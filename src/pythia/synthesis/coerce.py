"""Explicit conversion of Phase 5's uncoerced text cells. Pure: no I/O, no config objects.

ADR-0006 deliberately stores every cell as ``str | None`` because Greek exports use ``,`` as a
decimal separator and eager parsing would corrupt figures. This module pays that debt, and it
pays it **per column**: a rule applied cell-by-cell would read ``1,234`` as 1.234 in one row
and 1234 in the next, inside the same total.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pythia.synthesis.lexicon import OBSERVATION_FLAGS, SCALE_HINTS, SENTINELS, fold
from pythia.synthesis.models import CoercedColumn, CoercedKind

_NBSP = "    "
_TRUE = {"true", "yes", "y", "ναι", "t", "1"}
_FALSE = {"false", "no", "n", "όχι", "οχι", "f", "0"}
# Anything a Decimal would happily construct but no publisher ever means.
_NON_FINITE = re.compile(r"^[+-]?(nan|snan|inf|infinity)$", re.IGNORECASE)
_CURRENCY = "€$£"
_TRAILING_FLAG = re.compile(r"^(?P<number>.*?\d)\s+(?P<flag>[a-zA-Z])$")

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_TS = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ]\d{2}:\d{2}")
_YEAR_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^(\d{4})$")
_QUARTER = re.compile(r"^(\d{4})[-/]?Q([1-4])$", re.IGNORECASE)
_SLASHED = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


class _CellKind(StrEnum):
    """How one cell reads on its own, before the column decides."""

    COMMA_DECIMAL = "comma_decimal"  # 86,6
    POINT_DECIMAL = "point_decimal"  # 86.6
    INTEGER = "integer"
    AMBIGUOUS = "ambiguous"  # 1,234 — a thousands group or three decimals
    UNPARSEABLE = "unparseable"


def is_null(value: str | None) -> bool:
    """Report whether a cell carries no value.

    The CSV path yields ``""`` for an empty field while the DataStore path yields ``None``, so
    without this the same table coerces differently depending on how it was fetched — exactly
    the path-dependence ADR-0006 exists to eliminate.
    """
    if value is None:
        return True
    return not value.strip().strip(_NBSP)


def is_sentinel(value: str | None) -> bool:
    """Report whether a cell is a 'no data' marker rather than a value."""
    if value is None:
        return False
    return value.strip().casefold() in SENTINELS


def strip_flag(value: str) -> tuple[str, str | None]:
    """Split a trailing Eurostat observation flag off a value.

    ``'86,6 p'`` is provisional. The flag is captured rather than stripped: silently dropping
    it would let a provisional figure carry a superlative claim beside definitive ones.
    """
    match = _TRAILING_FLAG.match(value.strip())
    if match and match.group("flag").lower() in OBSERVATION_FLAGS:
        return match.group("number"), match.group("flag").lower()
    return value, None


def _clean_numeric(value: str) -> tuple[str, str | None]:
    """Strip formatting noise, returning the bare numeral and any unit symbol found."""
    text = unicodedata.normalize("NFKC", value.strip())
    for char in _NBSP:
        text = text.replace(char, "")
    text = text.replace(" ", "")
    unit: str | None = None
    if "%" in text:
        unit = "%"
        text = text.replace("%", "")
    for symbol in _CURRENCY:
        if symbol in text:
            unit = "€" if symbol == "€" else symbol
            text = text.replace(symbol, "")
    if text.upper().endswith("EUR"):
        unit = "€"
        text = text[:-3]
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):  # accounting negative
        text = "-" + text[1:-1]
    return text, unit


def classify_cell(value: str) -> tuple[_CellKind, str | None]:
    """Classify one cell's numeric reading, and report any unit symbol it carried."""
    text, unit = _clean_numeric(value)
    if not text or _NON_FINITE.match(text):
        # Decimal('nan') parses happily; admitting it would promote the column to numeric and
        # make every downstream sum silently NaN under a confident provenance footer.
        return _CellKind.UNPARSEABLE, unit
    if not re.fullmatch(r"[+-]?[\d.,]+", text):
        return _CellKind.UNPARSEABLE, unit
    body = text.lstrip("+-")
    commas, points = body.count(","), body.count(".")
    if commas and points:
        # Whichever separator comes last is the decimal one: 1.234,56 (el) vs 1,234.56 (en).
        comma_last = body.rfind(",") > body.rfind(".")
        return (_CellKind.COMMA_DECIMAL if comma_last else _CellKind.POINT_DECIMAL), unit
    if not commas and not points:
        return (_CellKind.INTEGER if body.isdigit() else _CellKind.UNPARSEABLE), unit
    separator = "," if commas else "."
    if body.count(separator) > 1:  # 1.234.567 — only ever thousands groups
        return _CellKind.INTEGER, unit
    tail = body.split(separator)[1]
    if len(tail) == 3:
        return _CellKind.AMBIGUOUS, unit  # 1,234: a group or three decimals; the column decides
    kind = _CellKind.COMMA_DECIMAL if separator == "," else _CellKind.POINT_DECIMAL
    return kind, unit


def to_decimal(value: str, *, comma_decimal: bool, max_digits: int) -> Decimal | None:
    """Parse one cell under a reading the column already agreed on."""
    text, _ = _clean_numeric(value)
    if not text or _NON_FINITE.match(text):
        return None
    if comma_decimal:
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")
    if len(text) > max_digits + 8:
        # A multi-million-digit cell builds an exact Decimal and makes every later sum
        # superlinear bigint work: one hostile cell, one pegged CPU.
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or len(parsed.as_tuple().digits) > max_digits:
        return None
    if abs(parsed.adjusted()) > max_digits:
        # 1e999999999 has one digit and a ruinous exponent; every later multiply is bigint
        # work and no publisher means it.
        return None
    return parsed


def to_temporal(value: str, *, day_first: bool | None = None) -> date | None:
    """Parse one cell as a date or period start. Never converts timezones.

    A ``+03:00`` Greek timestamp converted to UTC lands on the previous day, so a daily count
    for the 15th would quietly absorb part of the 14th. The literal date portion is used.
    """
    text = value.strip()
    for pattern in (_ISO_TS, _ISO_DATE):
        if match := pattern.match(text):
            return _safe_date(int(match[1]), int(match[2]), int(match[3]))
    if match := _YEAR_MONTH.match(text):
        return _safe_date(int(match[1]), int(match[2]), 1)
    if match := _QUARTER.match(text):
        return _safe_date(int(match[1]), (int(match[2]) - 1) * 3 + 1, 1)
    if match := _YEAR.match(text):
        year = int(match[1])
        return _safe_date(year, 1, 1) if 1900 <= year <= 2100 else None
    if match := _SLASHED.match(text):
        first, second, year = int(match[1]), int(match[2]), int(match[3])
        if day_first is False:
            return _safe_date(year, first, second)
        if day_first is True or first > 12:
            return _safe_date(year, second, first)
        return None  # genuinely ambiguous and the column did not resolve it
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    """Build a date, or ``None`` if the components are not a real one."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


def scale_hint_for(name: str) -> str | None:
    """Read a declared scale ("σε χιλιάδες") out of a column name."""
    folded = fold(name)
    for needle, label in SCALE_HINTS:
        if needle in folded:
            return label
    return None


def coerce_column(
    name: str, cells: list[str | None], *, max_digits: int = 32
) -> CoercedColumn:
    """Convert one column, all-or-nothing.

    A single unparseable non-null cell leaves the whole column ``TEXT``. Coercing 97% of a
    column and dropping the rest yields a total that is wrong with no visible symptom, so the
    column either converts completely or not at all.
    """
    nulls = sum(1 for cell in cells if is_null(cell))
    sentinels = sum(1 for cell in cells if is_sentinel(cell))
    present: list[tuple[int, str]] = [
        (index, cell)
        for index, cell in enumerate(cells)
        if cell is not None and not is_null(cell) and not is_sentinel(cell)
    ]
    scale = scale_hint_for(name)
    if not present:
        return CoercedColumn(name, CoercedKind.TEXT, [None] * len(cells), nulls, sentinels,
                             scale_hint=scale)

    flagged: set[int] = set()
    stripped: list[tuple[int, str]] = []
    for index, cell in present:
        body, flag = strip_flag(cell)
        if flag is not None:
            flagged.add(index)
        stripped.append((index, body))

    numeric = _try_numeric(name, cells, stripped, nulls, sentinels, scale, flagged, max_digits)
    if numeric is not None:
        return numeric
    temporal = _try_temporal(name, cells, stripped, nulls, sentinels, scale, flagged)
    if temporal is not None:
        return temporal
    if all(body.strip().casefold() in _TRUE | _FALSE for _, body in stripped):
        values: list[object | None] = [None] * len(cells)
        for index, body in stripped:
            values[index] = body.strip().casefold() in _TRUE
        return CoercedColumn(name, CoercedKind.BOOLEAN, values, nulls, sentinels,
                             scale_hint=scale, flagged_rows=frozenset(flagged))
    text: list[object | None] = [
        None if is_null(cell) or is_sentinel(cell) else str(cell).strip() for cell in cells
    ]
    return CoercedColumn(name, CoercedKind.TEXT, text, nulls, sentinels, scale_hint=scale,
                         flagged_rows=frozenset(flagged))


def _try_numeric(
    name: str, cells: list[str | None], stripped: list[tuple[int, str]], nulls: int,
    sentinels: int, scale: str | None, flagged: set[int], max_digits: int,
) -> CoercedColumn | None:
    """Resolve the column's decimal separator, or decline to guess."""
    kinds: set[_CellKind] = set()
    unit: str | None = None
    for _, body in stripped:
        kind, cell_unit = classify_cell(body)
        kinds.add(kind)
        unit = unit or cell_unit
    if _CellKind.UNPARSEABLE in kinds:
        return None
    if _CellKind.COMMA_DECIMAL in kinds and _CellKind.POINT_DECIMAL in kinds:
        return None  # both readings present in one column: no honest resolution exists
    if _CellKind.COMMA_DECIMAL in kinds:
        comma_decimal = True
    elif _CellKind.POINT_DECIMAL in kinds:
        comma_decimal = False
    elif _CellKind.AMBIGUOUS in kinds:
        return None  # only 1,234-shaped cells; nothing in the column disambiguates them
    else:
        comma_decimal = False  # integers only
    values: list[object | None] = [None] * len(cells)
    for index, body in stripped:
        parsed = to_decimal(body, comma_decimal=comma_decimal, max_digits=max_digits)
        if parsed is None:
            return None
        values[index] = parsed
    return CoercedColumn(name, CoercedKind.DECIMAL, values, nulls, sentinels, unit=unit,
                         scale_hint=scale, flagged_rows=frozenset(flagged))


def _try_temporal(
    name: str, cells: list[str | None], stripped: list[tuple[int, str]], nulls: int,
    sentinels: int, scale: str | None, flagged: set[int],
) -> CoercedColumn | None:
    """Parse the column as dates, resolving DD/MM vs MM/DD once for the whole column."""
    slashed = [body for _, body in stripped if _SLASHED.match(body.strip())]
    day_first: bool | None = None
    if slashed:
        firsts = [int(_SLASHED.match(body.strip())[1]) for body in slashed]  # type: ignore[index]
        seconds = [int(_SLASHED.match(body.strip())[2]) for body in slashed]  # type: ignore[index]
        if any(value > 12 for value in firsts):
            day_first = True
        elif any(value > 12 for value in seconds):
            day_first = False
        else:
            return None  # every row reads both ways; Greek convention is not a proof
    values: list[object | None] = [None] * len(cells)
    for index, body in stripped:
        parsed = to_temporal(body, day_first=day_first)
        if parsed is None:
            return None
        values[index] = parsed
    return CoercedColumn(name, CoercedKind.TEMPORAL, values, nulls, sentinels, scale_hint=scale,
                         flagged_rows=frozenset(flagged))
