"""Tests for the `main`-branch security-review gate hook.

The gate is only worth having if it stays correct: a regression here fails *open* and
is invisible, because nothing else exercises it. The hook lives outside ``src/`` (it is
Claude Code configuration, not application code), so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

HOOK_PATH = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "security_review_gate.py"


def _load_gate() -> Any:
    """Import the hook module from its path, since .claude/ is not a package."""
    spec = importlib.util.spec_from_file_location("security_review_gate", HOOK_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load_gate()

HEAD_SHA = "a" * 40
MAIN_SHA = "b" * 40
PR_HEAD_SHA = "c" * 40


@pytest.fixture
def on_develop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every subprocess call the gate makes, so tests never touch git or gh."""
    def fake_run(*args: str) -> str | None:
        if args[:2] == ("git", "rev-parse"):
            return {"HEAD": HEAD_SHA, "main": MAIN_SHA, "develop": HEAD_SHA}.get(args[2])
        if args[:3] == ("git", "branch", "--show-current"):
            return "develop"
        return None

    monkeypatch.setattr(gate, "run", fake_run)


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --base main --title x",
        "gh pr create --base=main --title x",
        "gh pr create -B main --title x",
        "git push origin HEAD:main",
        "git push origin develop:main",
        "git push --force origin main",
        "git push origin +main",
        "echo hi && gh pr create --base main",
    ],
)
def test_commands_touching_main_are_gated(command: str, on_develop: None) -> None:
    """Every route that can move main must yield a review subject."""
    assert gate.gate_subject(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --base develop --title x",
        "git push origin develop",
        # "main" inside a branch name is not the main branch.
        "git push origin feat/main-page",
        "git push -u origin feat/x",
        # Merging while checked out on develop does not move main.
        "git merge --no-ff release/1.0",
        # Prose in a commit message must not trip the gate — the failure that broke
        # the first version of this hook.
        "git commit -m 'docs: explain the gh pr create --base main gate'",
    ],
)
def test_ordinary_commands_pass(command: str, on_develop: None) -> None:
    """Day-to-day Gitflow work must not be blocked, or the gate gets disabled."""
    assert gate.gate_subject(command) is None


def test_push_subject_is_the_source_not_the_destination(on_develop: None) -> None:
    """The review must cover the code being published, not main's current tip."""
    assert gate.gate_subject("git push origin develop:main") == HEAD_SHA


def test_bare_push_while_on_main_is_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git push` with no refspec publishes the current branch: on main, that is main."""
    monkeypatch.setattr(
        gate, "run",
        lambda *a: "main" if a[:3] == ("git", "branch", "--show-current") else MAIN_SHA,
    )
    assert gate.gate_subject("git push") == MAIN_SHA


def test_pr_merge_uses_the_pr_head_not_local_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """A local checkout is unrelated to the PR being merged; review the PR's code."""
    def fake_run(*args: str) -> str | None:
        if "baseRefName" in args:
            return "main"
        if "headRefOid" in args:
            return PR_HEAD_SHA
        return HEAD_SHA

    monkeypatch.setattr(gate, "run", fake_run)
    assert gate.gate_subject("gh pr merge 42 --merge") == PR_HEAD_SHA


def test_pr_merge_selector_survives_leading_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """`gh pr merge --merge 42` must resolve PR 42, not the current branch's PR."""
    seen: list[tuple[str, ...]] = []

    def fake_run(*args: str) -> str | None:
        seen.append(args)
        return "develop"

    monkeypatch.setattr(gate, "run", fake_run)
    gate.gate_subject("gh pr merge --merge 42")
    assert any("42" in call for call in seen)


def test_unresolvable_pr_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the base cannot be read, assume main rather than waving the merge through."""
    monkeypatch.setattr(gate, "run", lambda *a: None)
    assert gate.gate_subject("gh pr merge 42") == gate.UNRESOLVED


def test_unresolved_subject_is_never_covered() -> None:
    """The fail-closed sentinel must not satisfy the freshness check."""
    assert gate.review_covers(gate.UNRESOLVED) is False


def test_review_covers_only_an_exact_sha_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stamp for one commit must not authorise merging a different one."""
    marker = tmp_path / ".claude" / ".last-security-review"
    marker.parent.mkdir(parents=True)
    marker.write_text(HEAD_SHA, encoding="utf-8")
    monkeypatch.setattr(gate, "run", lambda *a: str(tmp_path))

    assert gate.review_covers(HEAD_SHA) is True
    assert gate.review_covers(MAIN_SHA) is False


def test_missing_marker_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No stamp at all is the common case, and must deny."""
    monkeypatch.setattr(gate, "run", lambda *a: str(tmp_path))
    assert gate.review_covers(HEAD_SHA) is False
