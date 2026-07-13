"""Tests for query-side language detection + Greeklish transliteration (Phase 4)."""

from __future__ import annotations

import pytest

from pythia.planning.normalize import (
    detect_language,
    normalize_question,
    transliterate_greeklish,
)

# Real golden-set questions, one per language slice.
_GREEK = "Πόσα τροχαία ατυχήματα έχουν καταγραφεί στην Ελλάδα;"
_ENGLISH = [
    "What is the index tracking apartment prices published by the central bank?",
    "Where can I find the producer price index for Greek industry?",
    "I'm looking for the main bus and trolley route network of Athens.",
    "Are there meteorological observations from the weather station in Heraklion?",
]
_GREEKLISH = [
    "posa kroysmata oikonomikon egklimaton entopise i oikonomiki astynomia?",
    "poso einai i kinisi tou internet stin Ellada apo to GR-IX?",
    "pou tha vro tin elegxomeni stathmeusi (smart parking) tou Dimou Peiraia?",
    "ana perifereia, posa epaggelmatika lykeia EPAL yparxoun;",
]


def test_detect_greek_script() -> None:
    """Any Greek script means the el path."""
    assert detect_language(_GREEK) == "el"


@pytest.mark.parametrize("question", _ENGLISH)
def test_english_is_not_greeklish(question: str) -> None:
    """English questions must be detected as en (never transliterated)."""
    assert detect_language(question) == "en"


@pytest.mark.parametrize("question", _GREEKLISH)
def test_greeklish_detected(question: str) -> None:
    """Greeklish questions are detected via transliterated-Greek markers."""
    assert detect_language(question) == "greeklish"


@pytest.mark.parametrize("question", _ENGLISH)
def test_english_passes_through_unchanged(question: str) -> None:
    """normalize_question leaves English text byte-for-byte unchanged."""
    normalized, language = normalize_question(question)
    assert language == "en"
    assert normalized == question


def test_greeklish_is_transliterated_to_greek_script() -> None:
    """A greeklish question is rewritten into Greek script by normalize_question."""
    normalized, language = normalize_question(_GREEKLISH[0])
    assert language == "greeklish"
    assert any("Ͱ" <= ch <= "Ͽ" for ch in normalized)


def test_transliteration_maps_digraphs() -> None:
    """Digraphs are applied longest-match-first before single characters."""
    assert transliterate_greeklish("psari") == "ψαρι"
    assert transliterate_greeklish("theos") == "θεος"


def test_transliteration_applies_final_sigma() -> None:
    """A word-final s becomes ς, not σ."""
    result = transliterate_greeklish("kentros")
    assert result.endswith("ς")


def test_empty_question_defaults_to_english() -> None:
    """Blank input is detected as en and passes through unchanged (planner handles length)."""
    normalized, language = normalize_question("")
    assert language == "en"
    assert normalized == ""
