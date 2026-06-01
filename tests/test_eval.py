"""Tests for retrieval eval scoring and golden-set loading."""

from __future__ import annotations

from pythia.eval.run_eval import GoldenQuestion, hit_at_k, load_golden, reciprocal_rank


def test_reciprocal_rank_found() -> None:
    """RR is 1/position (1-based) of the expected id."""
    assert reciprocal_rank("b", ["a", "b", "c"]) == 0.5
    assert reciprocal_rank("a", ["a", "b"]) == 1.0


def test_reciprocal_rank_absent_is_zero() -> None:
    """RR is 0 when the expected id is not retrieved."""
    assert reciprocal_rank("z", ["a", "b", "c"]) == 0.0


def test_hit_at_k() -> None:
    """hit_at_k only counts a match within the first k."""
    ranked = ["a", "b", "c", "d"]
    assert hit_at_k("c", ranked, 3) is True
    assert hit_at_k("c", ranked, 2) is False
    assert hit_at_k("z", ranked, 4) is False


def test_load_golden_parses_questions() -> None:
    """The bundled golden set loads into typed questions with expected ids."""
    questions = load_golden()
    assert len(questions) >= 20
    assert all(isinstance(q, GoldenQuestion) for q in questions)
    assert all(q.expected_id and q.question for q in questions)
    assert {q.lang for q in questions} <= {"el", "en", "greeklish"}
