"""The only module in Phase 6 that produces a number.

Everything downstream — narration, chart, footer — reads a ``FactTable`` built here. Keeping
arithmetic in one pure, testable place is what makes "the LLM never emits a quantity"
enforceable rather than aspirational.
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from config import Settings, get_settings

from pythia.access.models import TableData
from pythia.planning.models import QueryParams
from pythia.synthesis.lexicon import TOTAL_ROW_LABELS, fold, group_key, safe_label
from pythia.synthesis.models import (
    AGGREGABLE,
    Binding,
    ColumnRole,
    Fact,
    FactTable,
    Operation,
)

#: Relative tolerance when testing whether a row equals the sum of the others.
_TOTAL_TOLERANCE = Decimal("0.01")


def summarise(
    table: TableData, binding: Binding, params: QueryParams | None = None,
    *, settings: Settings | None = None, language: str = "el",
) -> FactTable | None:
    """Compute the facts an answer may be built from, or ``None`` if none can be.

    ``None`` means refuse. It is returned for an empty table and — importantly — for a filter
    that matches nothing, because ``sum([]) == 0`` narrated as "0 αιτήματα" is a confident
    false zero rather than an absence of data.
    """
    cfg = settings or get_settings()
    if not table.rows:
        return None
    with localcontext() as ctx:
        ctx.prec = 34
        return _summarise(table, binding, params, cfg, language)


def _summarise(
    table: TableData, binding: Binding, params: QueryParams | None,
    cfg: Settings, language: str,
) -> FactTable | None:
    """Pick an operation from the binding and carry it out."""
    measure = _pick_measure(binding)
    role = binding.roles.get(measure) if measure else None

    if binding.temporal and measure and role is ColumnRole.RUNNING_CUMULATIVE:
        return _latest(table, binding, measure, cfg, language)
    if binding.temporal and measure and role in {ColumnRole.INDEX, ColumnRole.MEASURE}:
        return _series(table, binding, measure, cfg, language)
    if binding.dimension and measure and role in AGGREGABLE:
        return _grouped(table, binding, measure, cfg, language)
    if binding.dimension:
        return _counted(table, binding, cfg, language)
    return _listing(table, binding)


def _pick_measure(binding: Binding) -> str | None:
    """Choose the measure to report: a plain one if any, else the most informative fallback."""
    if binding.measures:
        return max(binding.measures, key=lambda name: binding.coerced[name].non_null)
    for role in (ColumnRole.INDEX, ColumnRole.RUNNING_CUMULATIVE, ColumnRole.ROW_TOTAL):
        named = [name for name, value in binding.roles.items() if value is role]
        if named:
            return max(named, key=lambda name: binding.coerced[name].non_null)
    return None


def _subtotal_rows(
    binding: Binding, dimension: str, measure: str | None
) -> tuple[set[int], int | None]:
    """Find rows that total the others, returning their indices and the total row's index.

    Two independent signals: the label, and the arithmetic. Both agreeing is a publisher-stated
    total — better evidence than our own sum, and reported separately rather than added to it.
    """
    labels = binding.coerced[dimension].values
    flagged = {
        index for index, value in enumerate(labels)
        if value is not None and fold(str(value)) in TOTAL_ROW_LABELS
    }
    if not flagged or measure is None:
        return flagged, None
    values = binding.coerced[measure].values
    confirmed: int | None = None
    for index in flagged:
        candidate = values[index]
        if not isinstance(candidate, Decimal):
            continue
        others = sum(
            value for position, value in enumerate(values)
            if position not in flagged and isinstance(value, Decimal)
        )
        if others and abs(candidate - others) <= abs(others) * _TOTAL_TOLERANCE:
            confirmed = index
            break
    return flagged, confirmed


def _grouped(
    table: TableData, binding: Binding, measure: str, cfg: Settings, language: str
) -> FactTable | None:
    """Group by the dimension and aggregate the measure."""
    dimension = binding.dimension
    assert dimension is not None
    subtotals, stated_index = _subtotal_rows(binding, dimension, measure)
    labels, values = binding.coerced[dimension].values, binding.coerced[measure].values

    buckets: dict[str, Decimal] = {}
    spellings: dict[str, Counter[str]] = {}
    used = 0
    for index, (label, value) in enumerate(zip(labels, values, strict=True)):
        if index in subtotals or label is None or not isinstance(value, Decimal):
            continue
        key = group_key(str(label))
        buckets[key] = buckets.get(key, Decimal(0)) + value
        spellings.setdefault(key, Counter())[str(label)] += 1
        used += 1
    if not used:
        return None

    display = {
        key: safe_label(counter.most_common(1)[0][0], max_chars=cfg.synthesis_max_label_chars)
        for key, counter in spellings.items()
    }
    ordered = sorted(buckets.items(), key=lambda item: item[1], reverse=True)
    kept, omitted = ordered[: cfg.synthesis_max_categories], ordered[cfg.synthesis_max_categories:]
    unit = binding.coerced[measure].unit

    facts = [
        Fact(
            label=display[key], value=total, n_used=used, unit=unit,
            basis=_basis(language, Operation.SUM, measure, used),
        )
        for key, total in kept
    ]
    if omitted:
        # The count of hidden categories is not enough: a reader sums the visible bars and
        # infers the total, so the tail's magnitude has to be stated too.
        facts.append(Fact(
            label="Λοιπά" if language == "el" else "Other",
            value=sum((total for _, total in omitted), Decimal(0)),
            n_used=len(omitted), unit=unit,
            basis=(
                f"άθροισμα των υπόλοιπων {len(omitted)} κατηγοριών" if language == "el"
                else f"sum of the remaining {len(omitted)} categories"
            ),
        ))
    stated = None
    if stated_index is not None:
        stated_value = values[stated_index]
        if isinstance(stated_value, Decimal):
            stated = Fact(
                label=safe_label(str(labels[stated_index]),
                                 max_chars=cfg.synthesis_max_label_chars),
                value=stated_value, n_used=1, unit=unit,
                basis=("σύνολο όπως το δημοσιεύει ο φορέας" if language == "el"
                       else "total as published by the source"),
            )
    series = [
        {"dim": display[key], "value": total}
        for key, total in (kept + ([("__other__", facts[-1].value)] if omitted else []))
        if key != "__other__"
    ]
    if omitted:
        series.append({"dim": facts[-1].label, "value": facts[-1].value})
    return FactTable(
        facts=facts, series=series, operation=Operation.SUM, row_basis=used,
        dimension=dimension, measure=measure, measure_role=binding.roles[measure],
        observed_range=binding.observed_range, publisher_stated_total=stated,
        omitted_categories=len(omitted),
        duplicate_key_count=used - len(buckets),
        truncation_is_categorical=_categorical_truncation(table, binding),
    )


def _counted(
    table: TableData, binding: Binding, cfg: Settings, language: str
) -> FactTable | None:
    """Count rows per dimension value — the honest operation when nothing is summable."""
    dimension = binding.dimension
    assert dimension is not None
    subtotals, _ = _subtotal_rows(binding, dimension, None)
    counts: Counter[str] = Counter()
    spellings: dict[str, Counter[str]] = {}
    for index, label in enumerate(binding.coerced[dimension].values):
        if index in subtotals or label is None:
            continue
        key = group_key(str(label))
        counts[key] += 1
        spellings.setdefault(key, Counter())[str(label)] += 1
    if not counts:
        return None
    display = {
        key: safe_label(counter.most_common(1)[0][0], max_chars=cfg.synthesis_max_label_chars)
        for key, counter in spellings.items()
    }
    ordered = counts.most_common()
    kept, omitted = ordered[: cfg.synthesis_max_categories], ordered[cfg.synthesis_max_categories:]
    used = sum(counts.values())
    facts = [
        Fact(label=display[key], value=count, n_used=used,
             basis=_basis(language, Operation.COUNT, dimension, used))
        for key, count in kept
    ]
    if omitted:
        facts.append(Fact(
            label="Λοιπά" if language == "el" else "Other",
            value=sum(count for _, count in omitted), n_used=len(omitted),
            basis=(f"πλήθος στις υπόλοιπες {len(omitted)} κατηγορίες" if language == "el"
                   else f"count across the remaining {len(omitted)} categories"),
        ))
    return FactTable(
        facts=facts, series=[{"dim": fact.label, "value": fact.value} for fact in facts],
        operation=Operation.COUNT, row_basis=used, dimension=dimension,
        observed_range=binding.observed_range, omitted_categories=len(omitted),
        truncation_is_categorical=_categorical_truncation(table, binding),
    )


def _series(
    table: TableData, binding: Binding, measure: str, cfg: Settings, language: str
) -> FactTable | None:
    """Build an ordered time series, refusing when the table holds many interleaved ones."""
    temporal = binding.temporal
    assert temporal is not None
    moments, values = binding.coerced[temporal].values, binding.coerced[measure].values
    keys = binding.series_key + ([binding.dimension] if binding.dimension else [])

    points: dict[tuple[Any, ...], list[tuple[date, Decimal]]] = {}
    used = 0
    for index, (moment, value) in enumerate(zip(moments, values, strict=True)):
        if not isinstance(moment, date) or not isinstance(value, Decimal):
            continue
        identity = tuple(str(binding.coerced[key].values[index]) for key in keys)
        points.setdefault(identity, []).append((moment, value))
        used += 1
    if not used:
        return None

    duplicates = sum(
        len(series) - len({moment for moment, _ in series}) for series in points.values()
    )
    if duplicates:
        # More than one observation per period inside a single series means the series key is
        # incomplete: the table holds dimensions we have not identified. Charting it would
        # draw several incommensurable series as one line.
        return FactTable(
            facts=[], series=[], operation=Operation.LISTING, row_basis=used,
            dimension=binding.dimension, measure=measure,
            measure_role=binding.roles[measure], observed_range=binding.observed_range,
            duplicate_key_count=duplicates,
        )

    series: list[dict[str, Any]] = []
    for identity, observations in sorted(points.items()):
        for moment, value in sorted(observations):
            series.append({
                "dim": moment.isoformat(),
                "value": value,
                "series": safe_label(" · ".join(identity), max_chars=64)
                if identity else measure,
            })
    role = binding.roles[measure]
    unit = binding.coerced[measure].unit
    facts: list[Fact] = []
    if len(points) == 1:
        observations = sorted(next(iter(points.values())))
        facts = [
            Fact(label=observations[0][0].isoformat(), value=observations[0][1], n_used=used,
                 unit=unit, basis=_basis(language, Operation.NONE, measure, used)),
            Fact(label=observations[-1][0].isoformat(), value=observations[-1][1], n_used=used,
                 unit=unit, basis=_basis(language, Operation.NONE, measure, used)),
        ]
    return FactTable(
        facts=facts, series=series, operation=Operation.NONE, row_basis=used,
        dimension=temporal, series_field="series" if len(points) > 1 else None,
        measure=measure, measure_role=role, observed_range=binding.observed_range,
        truncated_range=not table.complete,
    )


def _latest(
    table: TableData, binding: Binding, measure: str, cfg: Settings, language: str
) -> FactTable | None:
    """Reduce a running cumulative column to its value at the latest period.

    Summing such a column across every region-day is the single largest error available in
    this data; a bare MAX would return one region's total rather than the country's.
    """
    temporal, dimension = binding.temporal, binding.dimension
    assert temporal is not None
    moments, values = binding.coerced[temporal].values, binding.coerced[measure].values
    groups = binding.coerced[dimension].values if dimension else [None] * len(moments)

    latest: dict[Any, tuple[date, Decimal]] = {}
    for moment, value, group in zip(moments, values, groups, strict=True):
        if not isinstance(moment, date) or not isinstance(value, Decimal):
            continue
        key = str(group)
        if key not in latest or moment > latest[key][0]:
            latest[key] = (moment, value)
    if not latest:
        return None
    as_of = max(moment for moment, _ in latest.values())
    total = sum((value for _, value in latest.values()), Decimal(0))
    basis = (
        f"τιμή του «{measure}» στις {as_of.isoformat()}, αθροισμένη σε {len(latest)} περιοχές"
        if language == "el" else
        f"value of '{measure}' at {as_of.isoformat()}, summed over {len(latest)} areas"
    )
    return FactTable(
        facts=[Fact(label=measure, value=total, basis=basis, n_used=len(latest),
                    unit=binding.coerced[measure].unit)],
        series=[{"dim": safe_label(key, max_chars=cfg.synthesis_max_label_chars),
                 "value": value} for key, (_, value) in sorted(latest.items())],
        operation=Operation.LATEST, row_basis=len(latest), dimension=dimension,
        measure=measure, measure_role=ColumnRole.RUNNING_CUMULATIVE,
        observed_range=binding.observed_range, truncated_range=not table.complete,
    )


def _listing(table: TableData, binding: Binding) -> FactTable:
    """Report a bounded sample when nothing in the table can be bound."""
    return FactTable(
        facts=[], series=[], operation=Operation.LISTING, row_basis=len(table.rows),
        observed_range=binding.observed_range,
    )


def _categorical_truncation(table: TableData, binding: Binding) -> bool:
    """Detect truncation that removed whole categories rather than a clean time slice.

    When the fetch stopped mid-file, whether the missing rows are a tail of the time axis or
    entire unseen categories depends on the publisher's export order. If the categories in the
    first tenth of the rows differ materially from those in the last, the rows we never saw
    hold categories we never saw — and a ranked chart would show them as simply absent.
    """
    if table.complete or binding.dimension is None:
        return False
    values = [value for value in binding.coerced[binding.dimension].values if value is not None]
    if len(values) < 20:
        return False
    window = max(len(values) // 10, 1)
    head = {group_key(str(value)) for value in values[:window]}
    tail = {group_key(str(value)) for value in values[-window:]}
    return not (head & tail)


def non_negative(binding: Binding, measure: str | None) -> bool:
    """Report whether every fetched value of the measure is non-negative.

    A partial sum bounds the true total only for a non-negative measure. ``daydiff`` in the
    vaccination resource is negative in 5,550 of 35,076 rows, so this cannot be assumed.
    """
    if measure is None:
        return False
    return all(
        value >= 0 for value in binding.coerced[measure].values if isinstance(value, Decimal)
    )


def _basis(language: str, operation: Operation, column: str, used: int) -> str:
    """Render the derivation of a fact in the answer's language."""
    if language == "el":
        verb = {
            Operation.SUM: "άθροισμα", Operation.COUNT: "πλήθος γραμμών",
            Operation.AVG: "μέσος όρος", Operation.NONE: "τιμή όπως δημοσιεύτηκε",
        }.get(operation, operation.value)
        return f"{verb} του «{column}» σε {used} γραμμές"
    verb = {
        Operation.SUM: "sum", Operation.COUNT: "row count",
        Operation.AVG: "mean", Operation.NONE: "value as published",
    }.get(operation, operation.value)
    return f"{verb} of '{column}' over {used} rows"
