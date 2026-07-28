"""Query-side language detection + Greeklish→Greek transliteration (Phase 4, ADR-0005).

Pure and deterministic. The greeklish path is transliterated before retrieval to attack
the weak retrieval slice (greeklish MRR ~0.30); ``el``/``en`` questions pass through
unchanged (we do not accent-fold, because the dense index is built on raw accented text).
Detection defaults to ``en`` on ambiguity so English is never transliterated.
"""

from __future__ import annotations

import re

# Greek + Coptic (U+0370-03FF) and Greek Extended (U+1F00-1FFF).
_GREEK_RE = re.compile("[Ͱ-Ͽἀ-῿]")
_WORD_RE = re.compile("[a-zͰ-Ͽἀ-῿]+", re.IGNORECASE | re.UNICODE)

# Transliterated Greek function/interrogative words — unambiguous Greeklish signal.
_GREEKLISH_MARKERS = frozenset(
    {
        "posa", "poso", "posoi", "poses", "posous", "posi", "poia", "poies", "poioi",
        "poio", "pou", "poy", "pws", "pos", "giati", "pote", "einai", "ine", "yparxoun",
        "iparxoun", "yparxei", "iparxei", "stin", "sthn", "ston", "sto", "sti", "stis",
        "stous", "tou", "ton", "twn", "tvn", "tis", "kai", "ana", "gia", "apo", "tha",
        "vro", "vrw", "ftiaxnete", "kapou", "dimou", "dimos", "dimo", "xora", "xoras",
        "ellada", "ellados", "simeia", "kentra", "neoi", "mesa",
    }
)

# Common English function words — unambiguous English signal.
_ENGLISH_MARKERS = frozenset(
    {
        "the", "is", "are", "was", "were", "what", "where", "when", "which", "who", "how",
        "why", "find", "there", "published", "of", "for", "and", "with", "does", "do",
        "can", "am", "looking", "show", "about", "this", "that", "these", "those", "by",
        "on", "at", "an", "from", "main",
    }
)

_DIGRAPHS: tuple[tuple[str, str], ...] = (
    ("th", "θ"), ("ch", "χ"), ("ps", "ψ"), ("ks", "ξ"),
    ("ou", "ου"), ("ai", "αι"), ("ei", "ει"),
    ("oi", "οι"), ("gg", "γγ"), ("gk", "γκ"),
    ("mp", "μπ"), ("nt", "ντ"), ("tz", "τζ"),
    ("ts", "τσ"),
)

_SINGLES: dict[str, str] = {
    "a": "α", "b": "β", "c": "κ", "d": "δ", "e": "ε",
    "f": "φ", "g": "γ", "h": "η", "i": "ι", "j": "τζ",
    "k": "κ", "l": "λ", "m": "μ", "n": "ν", "o": "ο",
    "p": "π", "q": "κ", "r": "ρ", "s": "σ", "t": "τ",
    "u": "υ", "v": "β", "w": "ω", "x": "ξ", "y": "υ",
    "z": "ζ",
}

# Word-final sigma (σ -> ς).
_FINAL_SIGMA_RE = re.compile("σ(?=\\b|$)", re.UNICODE)


def detect_language(text: str) -> str:
    """Return ``"el" | "en" | "greeklish"`` for ``text`` (defaults to ``en`` on ambiguity).

    Greek script present → ``el``. Otherwise score transliterated-Greek markers against
    English markers over the tokens; ``greeklish`` only wins on a strictly higher count,
    so English (dense with cues like ``th``/``x``/``-is``) is never transliterated.
    """
    if _GREEK_RE.search(text):
        return "el"
    tokens = [t.lower() for t in _WORD_RE.findall(text)]
    greeklish_hits = sum(1 for t in tokens if t in _GREEKLISH_MARKERS)
    english_hits = sum(1 for t in tokens if t in _ENGLISH_MARKERS)
    return "greeklish" if greeklish_hits > english_hits and greeklish_hits > 0 else "en"


def transliterate_greeklish(text: str) -> str:
    """Transliterate Latin-script Greeklish to Greek (longest-match-first, lossy).

    Digraphs are matched before single characters; word-final ``σ`` becomes ``ς``. The
    mapping is best-effort — many Greeklish spellings collapse to one Greek form — and is
    meant to improve retrieval recall, not to reconstruct the original text exactly.
    """
    out: list[str] = []
    i = 0
    lowered = text.lower()
    while i < len(lowered):
        ch = lowered[i]
        pair = lowered[i : i + 2]
        digraph = next((g for latin, g in _DIGRAPHS if latin == pair), None)
        if digraph is not None:
            out.append(digraph)
            i += 2
        elif ch in _SINGLES:
            out.append(_SINGLES[ch])
            i += 1
        else:
            out.append(ch)
            i += 1
    return _FINAL_SIGMA_RE.sub("ς", "".join(out))


def normalize_question(text: str) -> tuple[str, str]:
    """Return ``(normalized_text, language)``; transliterate only the greeklish path."""
    language = detect_language(text)
    if language == "greeklish":
        return transliterate_greeklish(text), language
    return text, language
