"""Presentation helpers and markup the redesign depends on.

Two of these guard real defects rather than taste: the stats bars used an inline ``style``
that the CSP blocks (so every bar drew at full width), and htmx injected an indicator
``<style>`` element that the same CSP rejected on every page load.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pythia.api.app import _readable_date, _readable_time

TEMPLATES = Path("templates")


def _markup(path: Path) -> str:
    """A template's text with its {# comments #} removed, since those never render."""
    return re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)


@pytest.mark.parametrize(("raw", "expected"), [
    ("2026-05-11T17:25:52", "2026-05-11"),
    ("2026-05-11", "2026-05-11"),
    ("2026-07-29T12:00:00Z", "2026-07-29"),
    # footer.build's literal for a catalogue null must survive untouched, not be sliced.
    ("not recorded in the catalogue", "not recorded in the catalogue"),
    ("", ""),
])
def test_a_timestamp_is_shown_as_its_date(raw: str, expected: str) -> None:
    assert _readable_date(raw) == expected


def test_a_retrieval_time_is_shown_to_the_minute_in_utc() -> None:
    assert _readable_time("2026-07-29T12:00:00+00:00") == "2026-07-29 12:00 UTC"
    assert _readable_time("2026-07-29T15:30:00+03:00") == "2026-07-29 12:30 UTC"
    assert _readable_time("garbled") == "garbled"


def test_date_and_time_agree_on_the_day_across_an_offset() -> None:
    raw = "2024-03-05T01:00:00+02:00"
    assert _readable_time(raw) == "2024-03-04 23:00 UTC"
    assert _readable_date(raw) == "2024-03-04"


def test_no_template_uses_an_inline_style_the_csp_would_block() -> None:
    """style-src 'self' rejects style attributes, silently — the element just renders unstyled."""
    for template in TEMPLATES.rglob("*.html"):
        body = _markup(template)
        assert not re.search(r"\sstyle\s*=", body), f"{template} sets an inline style"
        assert "<style" not in body, f"{template} embeds a <style> element"


def test_htmx_is_told_not_to_inject_its_indicator_styles() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert '"includeIndicatorStyles": false' in base


def test_the_progress_fragment_still_polls_and_the_result_still_does_not() -> None:
    """The polling contract, restated against the redesigned markup."""
    assert 'hx-trigger="every 1s"' in _markup(TEMPLATES / "partials/_progress.html")
    assert "hx-trigger" not in _markup(TEMPLATES / "partials/_result.html")
