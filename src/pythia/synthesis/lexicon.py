"""Closed Greek/English word lists used by binding, computation and the claim guard.

Versioned data, deliberately in one place: the guard in ``verify`` is only as good as these
lists, so they must be reviewable rather than scattered through the modules that consume them.
Everything here is matched **after** ``fold`` — Greek uppercase drops accents and ``ΣΥΝΟΛΟ``
would otherwise never match a pattern written as ``σύνολο``.
"""

from __future__ import annotations

import re
import unicodedata

# Greek letters that render identically to a Latin letter. Mixed-keyboard typing puts both
# spellings of the same word in one column, and they must group as one category.
_HOMOGLYPHS = str.maketrans({
    "A": "Α", "B": "Β", "E": "Ε", "Z": "Ζ", "H": "Η", "I": "Ι", "K": "Κ", "M": "Μ",
    "N": "Ν", "O": "Ο", "P": "Ρ", "T": "Τ", "X": "Χ", "Y": "Υ",
})
_WHITESPACE = re.compile(r"[\s   ​-‍]+")


def _has_greek(value: str) -> bool:
    """Report whether the text contains any Greek letter."""
    return any("Ͱ" <= char <= "Ͽ" or "ἀ" <= char <= "῿" for char in value)


def fold(value: str) -> str:
    """Normalise text for matching and grouping.

    Strips diacritics, folds final sigma, maps Latin homoglyphs to Greek, collapses
    whitespace and casefolds. ``ΑΤΤΙΚΗ``, ``Αττική`` and Latin-typed ``ATTIKH`` all fold
    together — otherwise one category becomes three bars, each holding a third of the truth.

    Homoglyph mapping applies only to text that already contains Greek: it exists for
    mixed-keyboard spellings of Greek words. Applied blindly it would rewrite the ASCII
    ``BASE_PER`` into ``ΒΑSΕ_ΡΕR`` and no name pattern would ever match again. Use
    ``group_key`` instead when folding *values* for grouping, where a wholly Latin-typed
    Greek word must still land in the same bucket as its Greek spelling.
    """
    source = value.translate(_HOMOGLYPHS) if _has_greek(value) else value
    text = unicodedata.normalize("NFD", source)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = unicodedata.normalize("NFC", text).replace("ς", "σ").replace("Σ", "σ")
    return _WHITESPACE.sub(" ", text).strip().casefold()


# --- Row-level markers ---------------------------------------------------------------------

#: Dimension values that are a total over the other rows. Including one in a SUM double-counts.
TOTAL_ROW_LABELS: frozenset[str] = frozenset(
    fold(word) for word in (
        "ΣΥΝΟΛΟ", "ΣΥΝΟΛΑ", "ΓΕΝΙΚΟ ΣΥΝΟΛΟ", "ΣΥΝΟΛΙΚΑ", "ΑΘΡΟΙΣΜΑ", "ΟΛΕΣ", "ΟΛΑ",
        "ΣΥΝΟΛΟ ΧΩΡΑΣ", "TOTAL", "GRAND TOTAL", "ALL", "SUM",
    )
)

#: Residual buckets: legitimate in a total, but never the answer to "which is highest".
RESIDUAL_ROW_LABELS: frozenset[str] = frozenset(
    fold(word) for word in (
        "ΑΓΝΩΣΤΟ", "ΑΓΝΩΣΤΗ", "ΜΗ ΔΗΛΩΘΕΝ", "ΛΟΙΠΑ", "ΛΟΙΠΕΣ", "ΛΟΙΠΟΙ", "ΑΛΛΕΣ ΧΩΡΕΣ",
        "ΑΛΛΟ", "UNKNOWN", "OTHER", "NOT STATED",
    )
)

#: Cells that mean "no value", not a value. Left as text they would keep a whole measure
#: column unparseable, which silently demotes it to a dimension.
SENTINELS: frozenset[str] = frozenset(
    value.casefold() for value in (
        "-", "–", "—", ":", "..", ".", "n/a", "#n/a", "na", "#ΔΙΑΙΡ/0!", "#div/0!",
        "Δ/Υ", "ΜΗ ΔΙΑΘΕΣΙΜΟ", "ΔΕΝ ΥΠΑΡΧΟΥΝ ΣΤΟΙΧΕΙΑ", "x", "c", "w", "z", "null", "none",
    )
)

#: Eurostat-style observation flags appended to a value: provisional, estimated, break.
OBSERVATION_FLAGS: frozenset[str] = frozenset("pebefcdinrsu")


# --- Column-name signals -------------------------------------------------------------------

#: Whole-token names for a row counter or entity key. Deliberately *not* prefix matching:
#: `αρ`/`arith` would swallow ΑΡΙΘΜΟΣ ΑΤΥΧΗΜΑΤΩΝ, and "number of X" is the commonest measure
#: name in this catalogue. Value shape must agree before a column is demoted.
IDENTIFIER_NAMES: frozenset[str] = frozenset(
    fold(word) for word in ("id", "_id", "a/a", "α/α", "αα", "κωδικός", "κωδ", "code", "index_id")
)

CUMULATIVE_NAME = re.compile(fold("(σύνολο|synolo|total|cum|αθροιστ)"))
INDEX_NAME = re.compile(fold("(index|δείκτης|deiktis|obs_value|_idx)"))
YEAR_NAME = re.compile(fold("(year|έτος|etos|period|per|χρονια|τριμηνο|εξαμηνο)"))

#: Scale declared in the header. Never rescaled — carried into the rendered figure, so the
#: reader sees "4.500 χιλιάδες ευρώ" rather than a 1000x-wrong "4.500 ευρώ".
SCALE_HINTS: tuple[tuple[str, str], ...] = (
    (fold("σε χιλιάδες"), "χιλιάδες"),
    (fold("σε εκατ"), "εκατομμύρια"),
    (fold("σε δισ"), "δισεκατομμύρια"),
    (fold("χιλ."), "χιλιάδες"),
    (fold("εκατ."), "εκατομμύρια"),
    ("thousands", "thousands"),
    ("millions", "millions"),
)


# --- Claim vocabulary (the guard) ----------------------------------------------------------

#: Magnitudes expressed as words. Forbidden outright: they carry a quantity past a guard that
#: only inspects digits, and Greek reaches for them far more readily than English does.
NUMBER_WORDS: tuple[str, ...] = tuple(fold(word) for word in (
    "εκατομμύρι", "χιλιάδ", "χιλιάδες", "δισεκατομμύρι", "δεκάδ", "εκατοντάδ",
    "μισ", "μισό", "διπλάσι", "τριπλάσι", "τρίτο", "τέταρτο", "ένα τρίτο",
    "million", "billion", "thousand", "hundred", "half", "double", "twice", "triple",
    "third", "quarter", "dozens",
))

#: Trend, comparison and superlative language. Permitted only when a Fact licenses it, and
#: never over an incomplete table — "σταθερή πορεία 2010–2016" contains no numeral but is
#: false by construction when the series was cut at 2016.
TREND_WORDS: tuple[str, ...] = tuple(fold(word) for word in (
    "αυξήθηκ", "αύξηση", "μειώθηκ", "μείωση", "άνοδο", "ανοδικ", "πτώση", "πτωτικ", "τάση",
    "σταθερ", "εξέλιξη", "διπλασιάστηκ", "κορυφ", "ρεκόρ",
    "rose", "increase", "fell", "decrease", "decline", "growth", "trend", "stable", "surge",
))

SUPERLATIVE_WORDS: tuple[str, ...] = tuple(fold(word) for word in (
    "υψηλότερ", "χαμηλότερ", "μεγαλύτερ", "μικρότερ", "περισσότερ", "λιγότερ", "πρώτ",
    "τελευταί", "κορυφαί", "μέγιστ", "ελάχιστ",
    "highest", "lowest", "largest", "smallest", "most", "least", "top", "maximum", "minimum",
    "first", "leading",
))

#: Never legitimate in a narration built from a fact table.
MARKUP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(r"www\.", re.IGNORECASE),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    re.compile(r"</?[a-zA-Z][^>]*>"),
    re.compile(r"\]\([^)]*\)"),
    re.compile(r"```"),
    re.compile(r"<\|"),
)


def group_key(value: str) -> str:
    """Fold a dimension *value* for grouping.

    Unlike ``fold`` this always maps Latin homoglyphs to Greek, so a wholly Latin-typed
    ``ATTIKH`` groups with ``Αττική``. Applying it to column names would corrupt genuinely
    Latin identifiers, which is why the two are separate.
    """
    return fold(value.translate(_HOMOGLYPHS))


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def safe_label(value: str, *, max_chars: int = 120) -> str:
    """Make an untrusted cell value safe to render and to put in a chart title.

    Labels are published by whoever hosts the resource, and three quarters of them are not
    data.gov.gr. They reach the user through the answer text, the fact list and the chart
    title, so stripping control characters, newlines, markup and links has to happen once, at
    the point the label is created, rather than at each of those call sites.
    """
    # Truncate BEFORE the pattern sweep. A cell is bounded only by access_max_bytes (25 MB),
    # and the email pattern's `[\w.+-]+@` backtracks quadratically over a long run of word
    # characters — a single oversized cell would otherwise peg a CPU inside the sanitiser.
    text = _CONTROL.sub(" ", value[: max_chars * 4])
    for pattern in MARKUP_PATTERNS:
        text = pattern.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text or "—"
