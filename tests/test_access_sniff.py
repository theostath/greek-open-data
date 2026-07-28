"""Tests for the pure decode/parse/infer layer (Phase 5).

Byte literals only — no network, no DB. This is where silent corruption would originate.
"""

from __future__ import annotations

import pytest

from pythia.access import sniff
from pythia.access.models import MalformedPayloadError

GREEK = "Νομός,Ατυχήματα\nΑττικής,1234\nΘεσσαλονίκης,567\n"


def test_decode_utf8_with_bom() -> None:
    """A UTF-8 BOM is stripped and reported."""
    text, encoding = sniff.decode_bytes(GREEK.encode("utf-8-sig"))
    assert text.startswith("Νομός")
    assert encoding == "utf-8-sig"


def test_decode_cp1253_greek() -> None:
    """A Windows-1253 Greek body decodes to real Greek, not mojibake."""
    text, encoding = sniff.decode_bytes(GREEK.encode("cp1253"))
    assert "Αττικής" in text
    assert encoding in {"cp1253", "iso8859_7"}


def test_decode_rejects_undecodable() -> None:
    """Binary that is not Greek text raises rather than being best-guessed into mojibake."""
    with pytest.raises(MalformedPayloadError):
        sniff.decode_bytes(b"\xff\xfe\x00\x01\x02\x03\xff\xfe\x00\x81\x8d\x8f\x90\x9d")


def test_trim_to_last_line_drops_partial_record() -> None:
    """A byte-capped payload loses its incomplete final line.

    Regression: without this, "1234" cut to "12" becomes a valid-looking integer row.
    """
    trimmed = sniff.trim_to_last_line(b"a,b\n1,2\n3,12")
    assert trimmed == b"a,b\n1,2\n"


def test_trim_to_last_line_no_newline() -> None:
    """A payload with no newline at all yields nothing rather than a partial row."""
    assert sniff.trim_to_last_line(b"a,b,c") == b""


def test_truncated_utf8_does_not_mojibake_whole_file() -> None:
    """Trimming before decode keeps a mid-codepoint cut from forcing a Latin-1 fallback."""
    raw = GREEK.encode("utf-8")
    chopped = raw[: len(raw) - 3]  # lands inside a multi-byte Greek codepoint
    text, encoding = sniff.decode_bytes(sniff.trim_to_last_line(chopped))
    assert encoding.startswith("utf")
    assert "Αττικής" in text


def test_sniff_semicolon_with_decimal_comma() -> None:
    """A Greek export using ';' with ',' decimals is read as two columns, not one."""
    delimiter, confident = sniff.sniff_dialect("a;b\n1,5;2,5\n3,5;4,5\n")
    assert (delimiter, confident) == (";", True)


def test_sniff_failure_falls_back_to_comma_not_semicolon() -> None:
    """An unsniffable sample falls back to ',' and reports low confidence."""
    delimiter, confident = sniff.sniff_dialect("single\n")
    assert delimiter == ","
    assert confident is False


def test_sep_preamble_is_honoured() -> None:
    """Excel's 'sep=;' preamble sets the delimiter and is not treated as the header."""
    table = sniff.parse_csv("sep=;\na;b\n1;2\n", ",", max_rows=10)
    assert table.header == ["a", "b"]
    assert table.rows == [{"a": "1", "b": "2"}]


def test_duplicate_and_empty_headers_are_renamed() -> None:
    """Columns stay 1:1 with row keys instead of collapsing."""
    table = sniff.parse_csv("a,a,,b\n1,2,3,4\n", ",", max_rows=10)
    assert table.header == ["a", "a_2", "col_3", "b"]
    assert table.rows[0] == {"a": "1", "a_2": "2", "col_3": "3", "b": "4"}


def test_ragged_rows_are_padded_and_trimmed() -> None:
    """Short rows pad with None; over-long rows drop the excess."""
    table = sniff.parse_csv("a,b,c\n1,2\n1,2,3,4\n", ",", max_rows=10)
    assert table.rows[0] == {"a": "1", "b": "2", "c": None}
    assert table.rows[1] == {"a": "1", "b": "2", "c": "3"}


def test_vertical_tab_does_not_split_a_row() -> None:
    """Only CR/LF split records — str.splitlines() would also split \\v and \\x85."""
    table = sniff.parse_csv("a,b\nx\x0by,2\n", ",", max_rows=10)
    assert len(table.rows) == 1
    assert table.rows[0]["a"] == "x\x0by"


def test_nul_bytes_raise_malformed() -> None:
    """A NUL-bearing CSV is a typed failure, not an escaping _csv.Error."""
    with pytest.raises(MalformedPayloadError):
        sniff.parse_csv("a,b\n1,\x002\n", ",", max_rows=10)


def test_row_cap_marks_truncated() -> None:
    """Hitting max_rows is reported, not silent."""
    table = sniff.parse_csv("a\n1\n2\n3\n", ",", max_rows=2)
    assert len(table.rows) == 2
    assert table.truncated is True


def test_infer_types() -> None:
    """Integers, numbers, dates and booleans are recognised; decimal commas stay text."""
    rows = [
        {"i": "1", "n": "1.5", "d": "2024-01-01", "b": "true", "gr": "1,5", "t": "x"},
        {"i": "2", "n": "2.5", "d": "2024-02-01", "b": "false", "gr": "2,5", "t": "y"},
    ]
    types = {c.name: c.type for c in sniff.infer_columns(list(rows[0]), rows)}
    assert types == {
        "i": "integer", "n": "number", "d": "date", "b": "boolean",
        "gr": "text",  # decimal comma is NOT silently coerced
        "t": "text",
    }


def test_all_empty_column_is_text_not_boolean() -> None:
    """Zero non-empty values means text; the vacuous-truth bug made this boolean."""
    rows: list[dict[str, str | None]] = [{"a": ""}, {"a": None}]
    assert sniff.infer_columns(["a"], rows)[0].type == "text"


def test_zero_one_column_is_integer() -> None:
    """0/1 are integers, not booleans."""
    rows: list[dict[str, str | None]] = [{"a": "0"}, {"a": "1"}]
    assert sniff.infer_columns(["a"], rows)[0].type == "integer"


def test_parse_json_list_of_objects() -> None:
    """A plain list of flat objects parses."""
    table = sniff.parse_json_records([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}], max_rows=10)
    assert table.header == ["a", "b"]
    assert table.rows[0] == {"a": "1", "b": "x"}


def test_parse_json_records_envelope() -> None:
    """An object with exactly one list value parses."""
    table = sniff.parse_json_records({"help": "x", "records": [{"a": 1}]}, max_rows=10)
    assert table.rows == [{"a": "1"}]


def test_parse_json_nested_rejected() -> None:
    """GeoJSON-ish nesting is refused rather than flattened into nonsense."""
    with pytest.raises(MalformedPayloadError):
        sniff.parse_json_records([{"geometry": {"type": "Point"}}, [1, 2]], max_rows=10)


def test_sanity_check_rejects_markup_header() -> None:
    """A header that looks like HTML is not a table."""
    from pythia.access.models import Column

    with pytest.raises(MalformedPayloadError):
        sniff.sanity_check([Column(name="<!DOCTYPE html>", type="text")], [{"x": "1"}],
                           confident=True)


def test_sanity_check_allows_a_genuine_single_column() -> None:
    """A real one-column file has no delimiter to sniff and must not be rejected for it."""
    from pythia.access.models import Column

    sniff.sanity_check([Column(name="a", type="integer")],
                       [{"a": "1"}, {"a": "2"}], confident=False)


def test_sanity_check_rejects_a_collapsed_table() -> None:
    """One column whose values still look delimited means the parse collapsed a real table."""
    from pythia.access.models import Column

    rows: list[dict[str, str | None]] = [{"a": "1;2;3"}, {"a": "4;5;6"}, {"a": "7;8;9"}]
    with pytest.raises(MalformedPayloadError):
        sniff.sanity_check([Column(name="a", type="text")], rows, confident=False)
