"""Pure decode -> parse -> infer for tabular payloads. No I/O, no config, no httpx.

Every function here is deterministic and unit-tested on byte literals, because this is where
silent corruption would originate: a mis-detected encoding turns Greek into plausible
Latin-1, and a mid-line byte cut turns "1234" into a perfectly valid "12".
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from typing import Any

from charset_normalizer import from_bytes

from pythia.access.models import Column, MalformedPayloadError

# Greek data means Greek codecs. An unrestricted charset_normalizer call cannot fail — it
# returns a best guess — so restricting the candidate set is what makes detection falsifiable.
_CANDIDATE_ENCODINGS = ("utf_8", "cp1253", "iso8859_7")
_DELIMITERS = ",;\t|"
_LINE_SPLIT = re.compile(r"\r\n|\r|\n")
_TRUE = {"true", "yes", "y", "ναι", "t"}
_FALSE = {"false", "no", "n", "όχι", "οχι", "f"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?")
_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER = re.compile(r"^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$")


@dataclass(frozen=True)
class ParsedTable:
    """Header + rows + the dialect facts worth surfacing as provenance."""

    header: list[str]
    rows: list[dict[str, str | None]]
    truncated: bool


def trim_to_last_line(raw: bytes) -> bytes:
    """Drop everything after the final newline.

    Called when a transfer hit the byte cap. Without it the partial trailing record becomes a
    real-looking row (padded with ``None``) and a cut mid-UTF-8-sequence forces the decoder
    onto a single-byte fallback that silently mojibakes the *entire* file.
    """
    index = raw.rfind(b"\n")
    return raw[: index + 1] if index != -1 else b""


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """Decode payload bytes to text, or raise. Never ``errors="replace"``."""
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for encoding in _CANDIDATE_ENCODINGS:
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    best = from_bytes(raw, cp_isolation=list(_CANDIDATE_ENCODINGS)).best()
    if best is None:
        raise MalformedPayloadError("could not decode payload as UTF-8, CP1253 or ISO-8859-7")
    return str(best), str(best.encoding)


def sniff_dialect(sample: str) -> tuple[str, bool]:
    """Return ``(delimiter, confident)`` for a CSV sample.

    Sniffs over whole lines only, so the sample never ends inside a quoted field. On
    ``csv.Sniffer`` failure we fall back to ``,`` and report ``confident=False`` rather than
    guessing ``;`` — the caller uses that to demand a stricter sanity check.
    """
    lines = [line for line in _LINE_SPLIT.split(sample) if line.strip()]
    if not lines:
        return ",", False
    joined = "\n".join(lines[:200])
    try:
        dialect = csv.Sniffer().sniff(joined, delimiters=_DELIMITERS)
    except csv.Error:
        return ",", False
    return dialect.delimiter, True


def strip_sep_preamble(text: str) -> tuple[str, str | None]:
    """Strip Excel's ``sep=;`` first line, returning the declared delimiter if present."""
    lines = _LINE_SPLIT.split(text, maxsplit=1)
    first = lines[0].strip().lower()
    if first.startswith("sep=") and len(first) == 5:
        return (lines[1] if len(lines) > 1 else ""), lines[0].strip()[4]
    return text, None


def dedupe_header(header: list[str]) -> list[str]:
    """Make header names unique and non-empty so columns and row keys stay 1:1.

    Dict-shaped rows cannot hold two identically named columns; silently collapsing them
    would drop a column's data while still reporting it in ``columns``.
    """
    seen: dict[str, int] = {}
    result: list[str] = []
    for index, raw_name in enumerate(header, start=1):
        name = (raw_name or "").strip().lstrip("﻿") or f"col_{index}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        result.append(name)
    return result


def parse_csv(text: str, delimiter: str, max_rows: int) -> ParsedTable:
    """Parse CSV text into header + dict rows, capped at ``max_rows``."""
    if "\x00" in text:
        # csv.reader no longer raises on NUL, so reject explicitly: a NUL means binary
        # content that detect.identify may have missed beyond its 4 KB sample.
        raise MalformedPayloadError("payload contains NUL bytes; not text/CSV")
    text, declared = strip_sep_preamble(text)
    if declared:
        delimiter = declared
    lines = [line for line in _LINE_SPLIT.split(text) if line != ""]
    if not lines:
        raise MalformedPayloadError("no CSV content after decoding")
    try:
        reader = csv.reader(lines, delimiter=delimiter)
        records: list[list[str]] = []
        truncated = False
        header_row: list[str] | None = None
        for row in reader:
            if header_row is None:
                header_row = row
                continue
            if len(records) >= max_rows:
                truncated = True
                break
            records.append(row)
    except csv.Error as exc:  # NUL bytes, field-size limit, bad quoting
        raise MalformedPayloadError(f"malformed CSV: {exc}") from exc
    if header_row is None:
        raise MalformedPayloadError("CSV had no header row")
    header = dedupe_header(header_row)
    width = len(header)
    rows: list[dict[str, str | None]] = []
    for record in records:
        values: list[str | None] = list(record[:width])
        values.extend([None] * (width - len(values)))
        rows.append(dict(zip(header, values, strict=True)))
    return ParsedTable(header=header, rows=rows, truncated=truncated)


def parse_json_records(payload: Any, max_rows: int) -> ParsedTable:
    """Parse a JSON payload of flat records into header + dict rows."""
    records = _extract_records(payload)
    truncated = len(records) > max_rows
    records = records[:max_rows]
    header: list[str] = []
    for record in records:
        for key in record:
            if key not in header:
                header.append(key)
    rows = [
        {name: _scalar(record.get(name)) for name in header}
        for record in records
    ]
    return ParsedTable(header=header, rows=rows, truncated=truncated)


def _extract_records(payload: Any) -> list[dict[str, Any]]:
    """Find the list of flat objects in a JSON payload, or raise."""
    if isinstance(payload, list):
        candidate = payload
    elif isinstance(payload, dict):
        lists = [v for v in payload.values() if isinstance(v, list)]
        if len(lists) != 1:
            raise MalformedPayloadError(
                f"JSON object has {len(lists)} list values; cannot pick a record set"
            )
        candidate = lists[0]
    else:
        raise MalformedPayloadError(f"JSON payload is a {type(payload).__name__}, not tabular")
    if not all(isinstance(item, dict) for item in candidate):
        raise MalformedPayloadError("JSON records are not all objects (nested or GeoJSON?)")
    return list(candidate)


def _scalar(value: Any) -> str | None:
    """Render a JSON value as text, uniformly with the CSV path."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def infer_columns(
    header: list[str], rows: list[dict[str, str | None]], sample: int = 200
) -> list[Column]:
    """Infer a source type per column; a column is a type only if every value parses."""
    columns: list[Column] = []
    for name in header:
        values = [
            value
            for row in rows[:sample]
            if (value := row.get(name)) is not None and value.strip() != ""
        ]
        columns.append(Column(name=name, type=_infer_one(values)))
    return columns


def _infer_one(values: list[str]) -> str:
    """Return the narrowest type every value satisfies; no values means ``text``."""
    if not values:
        return "text"  # vacuous truth would otherwise make an empty column "boolean"
    stripped = [value.strip() for value in values]
    if all(value.lower() in _TRUE | _FALSE for value in stripped):
        return "boolean"
    if all(_INTEGER.match(value) for value in stripped):
        return "integer"
    if all(_NUMBER.match(value) for value in stripped):
        return "number"
    if all(_DATE.match(value) for value in stripped):
        return "date"
    if all(_TIMESTAMP.match(value) for value in stripped):
        return "timestamp"
    return "text"


def sanity_check(
    columns: list[Column], rows: list[dict[str, str | None]], *, confident: bool
) -> None:
    """Reject a 'table' that is almost certainly not one."""
    if not columns:
        raise MalformedPayloadError("no columns parsed")
    if any("<" in column.name or ">" in column.name for column in columns):
        raise MalformedPayloadError("header looks like markup, not column names")
    if not rows:
        raise MalformedPayloadError("no data rows parsed")
    if not confident and len(columns) == 1:
        # A single column is only suspicious if its values still look delimited — that is
        # the signature of a collapsed table. A genuinely one-column file has nothing to
        # sniff and must not be rejected for it.
        name = columns[0].name
        sampled = [row.get(name) or "" for row in rows[:50]]
        delimited = sum(1 for value in sampled if any(d in value for d in _DELIMITERS))
        if sampled and delimited > len(sampled) // 2:
            raise MalformedPayloadError(
                "delimiter could not be sniffed and the single column still looks delimited"
            )
