"""Tests for the development entrypoint.

``uv run pythia-dev`` is the startup path on boxes without ``make`` — which is all of them
here — so it is worth the same bar as the rest. What matters: it must *report* a missing
catalogue rather than refuse to boot, and its output must survive a Windows console.
"""

from __future__ import annotations

from typing import Any

import pytest
from config import Settings

from pythia.api import dev


@pytest.fixture
def health(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Patch the health probe so preflight never touches Chroma or the network."""
    def install(**facts: Any) -> None:
        defaults = {"datasets": 21806, "dense_index": 21806, "lexical_index": 21806,
                    "llm_reachable": True, "llm_model": "qwen3.5:9b", "status": "ok"}
        monkeypatch.setattr("pythia.api.app.health", lambda settings: {**defaults, **facts})
    return install


def test_a_ready_stack_reports_ready(health: Any, capsys: pytest.CaptureFixture[str]) -> None:
    health()

    assert dev.preflight(Settings()) is True
    assert "MISS" not in capsys.readouterr().out


def test_an_empty_catalogue_is_reported_and_does_not_block_startup(
    health: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Serving with a gap beats refusing to boot: /healthz and the landing page both say so."""
    health(datasets=0)

    ready = dev.preflight(Settings())

    out = capsys.readouterr().out
    assert ready is False
    assert "MISS" in out and "harvest" in out, "the fix must be named, not just the fault"
    assert "Starting anyway" in out


def test_an_unreachable_llm_names_ollama(health: Any, capsys: pytest.CaptureFixture[str]) -> None:
    """The commonest failure by far, and the least self-evident from a refusal."""
    health(llm_reachable=False)

    dev.preflight(Settings())

    assert "ollama serve" in capsys.readouterr().out


def test_a_missing_index_names_the_index_build(
    health: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    health(dense_index=0)

    dev.preflight(Settings())

    assert "retrieval.index" in capsys.readouterr().out


def test_the_banner_is_ascii_so_a_cp1252_console_cannot_mangle_it(
    health: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Encoding is this project's oldest recurring bug class; the banner should not add to it."""
    health(datasets=0, llm_reachable=False)

    dev.preflight(Settings())

    out = capsys.readouterr().out
    out.encode("ascii")  # raises UnicodeEncodeError if a non-ASCII character crept back in
