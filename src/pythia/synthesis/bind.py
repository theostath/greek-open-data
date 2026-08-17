"""Decide what each column means, and which requested params a table can actually satisfy.

Every rule here defaults to the conservative side. A column is aggregable only when it is
*proven* to be a plain measure; anything contested, unprovable or order-dependent becomes
``UNKNOWN`` and is reported rather than summed. Absence of evidence is not evidence of
summability — an unsorted cumulative column tests non-monotonic, and treating that as "safe to
sum" is how a vaccination total lands four orders of magnitude out.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from config import Settings, get_settings

from pythia.access.models import TableData
from pythia.planning.models import QueryParams
from pythia.synthesis import coerce
from pythia.synthesis.lexicon import (
    CUMULATIVE_NAME,
    IDENTIFIER_NAMES,
    INDEX_NAME,
    YEAR_NAME,
    fold,
    group_key,
)
from pythia.synthesis.models import (
    Binding,
    CoercedColumn,
    CoercedKind,
    ColumnRole,
    Operation,
)


class TableTooLargeError(Exception):
    """The table is past the size we will attempt to synthesise from."""


def bind_columns(
    table: TableData, params: QueryParams | None = None, *, settings: Settings | None = None
) -> Binding:
    """Classify every column, resolve series identity, and bind requested params."""
    cfg = settings or get_settings()
    _preflight(table, cfg)

    cells = {
        column.name: [row.get(column.name) for row in table.rows] for column in table.columns
    }
    coerced = {
        name: coerce.coerce_column(name, values, max_digits=cfg.synthesis_decimal_max_digits)
        for name, values in cells.items()
    }

    constants = {
        name: str(_first_value(column))
        for name, column in coerced.items()
        if _distinct(column) == 1 and column.non_null
    }
    roles = _classify(table, coerced, constants, cfg)

    temporal = _pick_temporal(roles, coerced)
    dimension = _pick_dimension(roles, coerced, exclude=temporal, cfg=cfg)
    # Re-run the order-dependent tests now that a temporal column is known; before this point
    # monotonicity cannot be evaluated, so cumulative columns sit in UNKNOWN.
    if temporal is not None:
        roles = _resolve_cumulative(roles, coerced, temporal=temporal, dimension=dimension)

    measures = [name for name, role in roles.items() if role is ColumnRole.MEASURE]
    series_key = [
        name
        for name, role in roles.items()
        if role in {ColumnRole.CODE, ColumnRole.DIMENSION}
        and name not in constants
        and name != dimension
    ]
    forbidden = _forbidden_ops(roles, coerced, measures)
    merged = _merged_variants(coerced, dimension)
    observed = _observed_range(coerced, temporal)
    unbound, notes = _bind_params(params, roles, coerced, temporal, dimension, observed)

    reshaped = False
    if temporal is None and dimension is not None and len(measures) >= 2:
        if len(table.rows) * len(measures) <= cfg.synthesis_max_series_rows:
            reshaped = True
        else:
            notes.append("too many measure columns to reshape; reporting them separately")

    return Binding(
        roles=roles, coerced=coerced, dimension=dimension, temporal=temporal,
        measures=measures, series_key=series_key, forbidden_ops=forbidden,
        unbound=unbound, reshaped=reshaped, header_trusted=table.header_trusted,
        observed_range=observed, merged_variants=merged, constant_columns=constants,
        notes=notes,
    )


def _preflight(table: TableData, cfg: Settings) -> None:
    """Refuse a table too large to hold coerced in memory.

    ``access_max_bytes`` bounds the wire, not resident objects: a Decimal costs roughly twice
    a str, and ``sniff`` imposes no column limit at all.
    """
    if len(table.columns) > cfg.synthesis_max_columns:
        raise TableTooLargeError(
            f"{len(table.columns)} columns exceeds the {cfg.synthesis_max_columns} we will bind"
        )
    cells = len(table.rows) * max(len(table.columns), 1)
    if cells > cfg.synthesis_max_cells:
        raise TableTooLargeError(
            f"{cells} cells exceeds the {cfg.synthesis_max_cells} we will coerce"
        )


def _distinct(column: CoercedColumn) -> int:
    """Count distinct non-null values."""
    return len({_hashable(value) for value in column.values if value is not None})


def _hashable(value: Any) -> Any:
    """Return a hashable form of a coerced value."""
    return str(value) if isinstance(value, Decimal) else value


def _first_value(column: CoercedColumn) -> Any:
    """Return the first non-null value, or None."""
    return next((value for value in column.values if value is not None), None)


def _classify(
    table: TableData, coerced: dict[str, CoercedColumn], constants: dict[str, str], cfg: Settings
) -> dict[str, ColumnRole]:
    """Assign one role per column under a fixed precedence; first match wins."""
    title = fold(table.dataset_title or "")
    numeric_names = [
        name for name, col in coerced.items() if col.kind is CoercedKind.DECIMAL
    ]
    roles: dict[str, ColumnRole] = {}
    for name, column in coerced.items():
        roles[name] = _classify_one(name, column, coerced, numeric_names, title, constants, cfg)
    return roles


def _classify_one(
    name: str, column: CoercedColumn, coerced: dict[str, CoercedColumn],
    numeric_names: list[str], title: str, constants: dict[str, str], cfg: Settings,
) -> ColumnRole:
    """Classify a single column. Order is the contract; see ADR-0007."""
    folded = fold(name)
    rows = len(column.values)

    if _is_identifier(folded, column, rows):
        return ColumnRole.IDENTIFIER
    if column.kind is CoercedKind.TEMPORAL:
        return ColumnRole.TEMPORAL
    if _is_year_like(folded, column):
        # A base year or a reporting year is a label or an axis, never a quantity to sum.
        return ColumnRole.DIMENSION
    if _is_code(column, rows, cfg):
        return ColumnRole.CODE
    if column.kind is not CoercedKind.DECIMAL:
        return ColumnRole.DIMENSION
    if column.sentinel_count > rows * cfg.synthesis_sentinel_ratio_max:
        return ColumnRole.UNKNOWN
    if INDEX_NAME.search(folded) or (INDEX_NAME.search(title) and len(numeric_names) == 1):
        return ColumnRole.INDEX
    if _is_row_total(name, column, coerced, numeric_names):
        return ColumnRole.ROW_TOTAL
    if CUMULATIVE_NAME.search(folded):
        # Only the name fired. Whether it is a running total cannot be judged until a temporal
        # column is known, so hold it as UNKNOWN rather than assume it is summable.
        return ColumnRole.UNKNOWN
    return ColumnRole.MEASURE


def _is_identifier(folded: str, column: CoercedColumn, rows: int) -> bool:
    """Detect a row counter or entity key. Value shape leads; the name may only confirm.

    A name-first rule is what made ``ΑΡΙΘΜΟΣ ΑΙΤΗΜΑΤΩΝ`` an identifier and destroyed the
    measure it was meant to protect — "number of X" is the commonest measure name here.
    """
    if column.kind is not CoercedKind.DECIMAL or rows == 0:
        return folded in IDENTIFIER_NAMES and column.kind is CoercedKind.TEXT
    values = [value for value in column.values if value is not None]
    if not values:
        return False
    distinct = len({str(value) for value in values})
    dense_sequence = (
        len(values) == rows
        and distinct == rows
        and all(value == Decimal(index + 1) for index, value in enumerate(values))
    )
    if dense_sequence:
        return True
    integral = all(value == value.to_integral_value() for value in values)
    if not integral:
        return False
    # A key suffix ("areaid", "kodikos_id") confirms an entity code even when the values
    # repeat, which is the normal shape of a region id across a panel of dates.
    if folded in IDENTIFIER_NAMES or folded.endswith(("id", "κωδ", "_code")):
        return True
    return distinct == rows and folded in IDENTIFIER_NAMES


def _is_year_like(folded: str, column: CoercedColumn) -> bool:
    """Detect an integer column that is really a year."""
    if column.kind is not CoercedKind.DECIMAL or not YEAR_NAME.search(folded):
        return False
    values = [value for value in column.values if value is not None]
    return bool(values) and all(
        value == value.to_integral_value() and Decimal(1900) <= value <= Decimal(2100)
        for value in values
    )


def _is_code(column: CoercedColumn, rows: int, cfg: Settings) -> bool:
    """Detect a short, heavily repeated, uppercase token column (SDMX-style codes)."""
    if column.kind is not CoercedKind.TEXT or rows == 0:
        return False
    values = [str(value) for value in column.values if value is not None]
    if not values:
        return False
    if max(len(value) for value in values) > cfg.synthesis_code_max_len:
        return False
    if len(set(values)) / len(values) > cfg.synthesis_code_repeat_ratio:
        return False
    uppercase = sum(1 for value in values if value.replace("_", "").isupper() or value.isdigit())
    return uppercase >= 0.9 * len(values)


def _is_row_total(
    name: str, column: CoercedColumn, coerced: dict[str, CoercedColumn], numeric_names: list[str]
) -> bool:
    """Detect a column whose value equals the sum of its sibling measures on the same row."""
    siblings = [other for other in numeric_names if other != name]
    if len(siblings) < 2:
        return False
    checked = 0
    for index, value in enumerate(column.values):
        if value is None:
            continue
        parts = [coerced[other].values[index] for other in siblings]
        if any(part is None for part in parts):
            continue
        if value != sum(parts):
            return False
        checked += 1
        if checked >= 50:
            break
    return checked >= 3


def _resolve_cumulative(
    roles: dict[str, ColumnRole], coerced: dict[str, CoercedColumn], *,
    temporal: str, dimension: str | None,
) -> dict[str, ColumnRole]:
    """Promote name-flagged UNKNOWN columns to RUNNING_CUMULATIVE when the values agree.

    The test needs rows in time order within each group, which is only possible once the
    temporal column is known — running it before that is circular, and running it on fetch
    order makes the outcome depend on how the publisher happened to export the file.
    """
    resolved = dict(roles)
    times = coerced[temporal].values
    groups = coerced[dimension].values if dimension else [None] * len(times)
    for name, role in roles.items():
        if role is not ColumnRole.UNKNOWN:
            continue
        column = coerced[name]
        if column.kind is not CoercedKind.DECIMAL or not CUMULATIVE_NAME.search(fold(name)):
            continue
        if _is_monotonic_in_time(column.values, times, groups):
            resolved[name] = ColumnRole.RUNNING_CUMULATIVE
        else:
            # The name said "total" but the values are not a running total — a per-row count
            # like ΣΥΝΟΛΟ ΜΑΘΗΤΩΝ or a daily delta. Summable, with the disagreement noted.
            resolved[name] = ColumnRole.MEASURE
    return resolved


def _is_monotonic_in_time(
    values: list[Any], times: list[Any], groups: list[Any]
) -> bool:
    """Report whether values never decrease over time within each group."""
    buckets: dict[Any, list[tuple[Any, Any]]] = {}
    for value, moment, group in zip(values, times, groups, strict=True):
        if value is None or moment is None:
            continue
        buckets.setdefault(group, []).append((moment, value))
    if not buckets:
        return False
    for series in buckets.values():
        series.sort(key=lambda pair: pair[0])
        pairs = zip(series, series[1:], strict=False)
        if any(later < earlier for (_, earlier), (_, later) in pairs):
            return False
    return True


def _pick_temporal(roles: dict[str, ColumnRole], coerced: dict[str, CoercedColumn]) -> str | None:
    """Choose the temporal axis: the date-like column with the most distinct values."""
    candidates = [name for name, role in roles.items() if role is ColumnRole.TEMPORAL]
    if not candidates:
        return None
    return max(candidates, key=lambda name: _distinct(coerced[name]))


def _pick_dimension(
    roles: dict[str, ColumnRole], coerced: dict[str, CoercedColumn], *,
    exclude: str | None, cfg: Settings,
) -> str | None:
    """Choose the grouping column: a label with plausible category cardinality.

    One row per category is a perfectly normal published shape (q16 is 101 nationalities in
    101 rows), so near-uniqueness is not disqualifying on its own — high cardinality is
    handled downstream by the top-N cut and its explicit remainder. Constant columns are
    excluded because a single bucket is not a grouping.
    """
    candidates = [
        name for name, role in roles.items()
        if role in {ColumnRole.DIMENSION, ColumnRole.CODE} and name != exclude
    ]
    scored: list[tuple[int, str]] = []
    for name in candidates:
        column = coerced[name]
        if column.non_null == 0 or _distinct(column) <= 1:
            continue
        scored.append((_distinct(column), name))
    if not scored:
        return None
    # Prefer a cardinality that fits a chart; fall back to the smallest available.
    fitting = [pair for pair in scored if pair[0] <= cfg.synthesis_max_categories]
    return max(fitting)[1] if fitting else min(scored)[1]


def _forbidden_ops(
    roles: dict[str, ColumnRole], coerced: dict[str, CoercedColumn], measures: list[str]
) -> frozenset[Operation]:
    """Report which aggregations the bound measure cannot honestly support."""
    forbidden: set[Operation] = set()
    if not measures:
        forbidden |= {Operation.SUM, Operation.AVG}
    for name in measures:
        if coerced[name].unit == "%":
            # Percentages have no meaningful sum, and averaging them needs weights we do not
            # have. Reported as published instead.
            forbidden |= {Operation.SUM, Operation.AVG}
    if any(role is ColumnRole.INDEX for role in roles.values()) and not measures:
        forbidden |= {Operation.SUM, Operation.AVG}
    return frozenset(forbidden)


def _merged_variants(
    coerced: dict[str, CoercedColumn], dimension: str | None
) -> dict[str, list[str]]:
    """Report dimension values that fold together, so the merge can be disclosed."""
    if dimension is None:
        return {}
    groups: dict[str, set[str]] = {}
    for value in coerced[dimension].values:
        if value is None:
            continue
        groups.setdefault(group_key(str(value)), set()).add(str(value))
    return {key: sorted(spellings) for key, spellings in groups.items() if len(spellings) > 1}


def _observed_range(
    coerced: dict[str, CoercedColumn], temporal: str | None
) -> tuple[str, str] | None:
    """Report the first and last period actually present in the fetched rows."""
    if temporal is None:
        return None
    moments = [value for value in coerced[temporal].values if isinstance(value, date)]
    if not moments:
        return None
    return min(moments).isoformat(), max(moments).isoformat()


def _bind_params(
    params: QueryParams | None, roles: dict[str, ColumnRole], coerced: dict[str, CoercedColumn],
    temporal: str | None, dimension: str | None, observed: tuple[str, str] | None,
) -> tuple[list[str], list[str]]:
    """Bind requested filters to real columns, reporting whatever could not be applied."""
    unbound: list[str] = []
    notes: list[str] = []
    if params is None:
        return unbound, notes

    if params.date_from or params.date_to:
        if temporal is None:
            unbound.append("date")
            notes.append("this dataset is not broken down by time")
        elif observed is None:
            unbound.append("date")
    if params.region:
        target = group_key(params.region)
        holder = next(
            (
                name for name, column in coerced.items()
                if roles[name] in {ColumnRole.DIMENSION, ColumnRole.CODE}
                and any(
                    value is not None and group_key(str(value)) == target
                    for value in column.values
                )
            ),
            None,
        )
        if holder is None:
            unbound.append("region")
            if dimension is None:
                notes.append("this dataset is not broken down by region")
    if params.group_by and dimension is None:
        unbound.append("group_by")
    return unbound, notes


def range_overlap(
    observed: tuple[str, str] | None, date_from: str | None, date_to: str | None
) -> str:
    """Compare a requested period against the data's own range.

    Returns ``"none"`` (disjoint), ``"partial"``, or ``"full"``. A disjoint request is a
    refusal, not a caveat: answering "45.231" under a footnote that the year filter could not
    be applied is read by everyone as the figure for the year they asked about.
    """
    if observed is None or (not date_from and not date_to):
        return "full"
    start, end = observed
    if date_to and date_to < start:
        return "none"
    if date_from and date_from > end:
        return "none"
    if (date_from and date_from > start) or (date_to and date_to < end):
        return "partial"
    return "full"
