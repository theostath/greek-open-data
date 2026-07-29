"""The claim guard: decide whether generated prose may be shown.

A guard that only inspects digits is not a guard. "Ο δείκτης παρουσιάζει σταθερή πορεία
2010–2016" contains no numeral, is false by construction when the series was cut at 2016, and
would pass a numerals-only check unchallenged. So this module gates **claims**: numerals,
magnitudes written as words, trend and superlative language, and anything that looks like
markup or an exfiltration target.

Rejection is cheap. The deterministic template renders the same facts, so failing closed costs
a stylistic downgrade rather than an answer.
"""

from __future__ import annotations

import re
from decimal import Decimal

from pythia.synthesis.lexicon import (
    MARKUP_PATTERNS,
    NUMBER_WORDS,
    SUPERLATIVE_WORDS,
    TREND_WORDS,
    fold,
)
from pythia.synthesis.models import Fact, FactTable, Footer, Operation, VerificationResult

# A numeral, plus any alphanumeric context around it. The context is what lets an SDMX code
# like BTE36 through: the Notes require codes to be shown opaquely, and rejecting them would
# reject every faithful narration of the flagship dataset.
_TOKEN = re.compile(r"[A-Za-zΑ-Ωα-ω_]*\d[\w.,]*")
_PURE_NUMBER = re.compile(r"^[+-]?\d[\d.,]*$")
_MAX_CHARS = 1200


def normalise_number(token: str, language: str) -> str:
    """Reduce a numeral to a canonical form for comparison.

    Pinned to the answer's language: accepting both readings would let ``1.234`` match both
    1234 and 1.234, roughly doubling what the guard lets through.
    """
    text = token.strip().rstrip(".,").lstrip("+")
    if language == "el":
        text = text.replace(".", "").replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        value = Decimal(text)
    except Exception:  # noqa: BLE001 - any unparseable token is simply not a number
        return text
    return str(value.normalize())


def allowed_tokens(facts: FactTable | None, footer: Footer | None, language: str) -> set[str]:
    """Build the exact set of numerals a narration may contain.

    Exact membership, never substring: treating the coverage string "50.000 από 124.485
    γραμμές (40%)" as a *source* of allowed numerals by appearance would silently admit 50,
    000, 124, 485, 40, 1, 2, 4, 5 and 6 — enough for a hallucinated "αύξηση 40%" to pass.
    """
    allowed: set[str] = set()
    for fact in facts.facts if facts else []:
        allowed.add(normalise_number(str(fact.value), language))
        allowed.add(str(fact.n_used))
    if facts and facts.publisher_stated_total is not None:
        allowed.add(normalise_number(str(facts.publisher_stated_total.value), language))
    if facts:
        allowed.add(str(facts.row_basis))
        for point in facts.series:
            value = point.get("value")
            if isinstance(value, Decimal | int):
                allowed.add(normalise_number(str(value), language))
    if footer is not None:
        for number in re.findall(r"\d[\d.,]*", footer.row_coverage):
            allowed.add(normalise_number(number, language))
        # Years may come only from the data's own coverage. Allowing fetched_at or
        # last_updated is how an answer about the wrong period verifies clean.
        for bound in footer.observed_range or ():
            for part in re.findall(r"\d+", bound):
                allowed.add(normalise_number(part, language))
    return {token for token in allowed if token}


def check_claims(
    text: str, facts: FactTable | None, footer: Footer | None, *,
    language: str = "el", complete: bool = True, limitation: str | None = None,
) -> VerificationResult:
    """Decide whether ``text`` may be shown as the answer."""
    if not text.strip():
        return VerificationResult(ok=False, reason="empty narration")
    if len(text) > _MAX_CHARS:
        return VerificationResult(ok=False, reason="narration far longer than a summary")
    for pattern in MARKUP_PATTERNS:
        if pattern.search(text):
            return VerificationResult(ok=False, reason="narration contains markup or a link")

    folded = fold(text)
    for word in NUMBER_WORDS:
        if word in folded:
            # A magnitude in words is a quantity the numeral check cannot see, and Greek
            # reaches for that phrasing far more readily than English does.
            return VerificationResult(ok=False, rejected_tokens=[word],
                                      reason="magnitude written as a word")

    licensed = _licensed_claims(facts, complete)
    for word in SUPERLATIVE_WORDS:
        if word in folded and "superlative" not in licensed:
            return VerificationResult(ok=False, rejected_tokens=[word],
                                      reason="superlative not licensed by the facts")
    for word in TREND_WORDS:
        if word in folded and "trend" not in licensed:
            return VerificationResult(ok=False, rejected_tokens=[word],
                                      reason="trend claim not licensed by the facts")

    if limitation and fold(limitation)[:24] not in folded:
        return VerificationResult(ok=False, reason="stated limitation omitted from the answer")

    allowed = allowed_tokens(facts, footer, language)
    if limitation:
        # The limitation is our own sentence, built from the observed range, and the model is
        # required to repeat it. Its figures are licensed by construction.
        for number in re.findall(r"\d[\d.,]*", limitation):
            allowed.add(normalise_number(number, language))
    unmatched = [
        token for token in _TOKEN.findall(text)
        if _PURE_NUMBER.match(token) and normalise_number(token, language) not in allowed
    ]
    if unmatched:
        return VerificationResult(ok=False, rejected_tokens=unmatched[:5],
                                  reason="figure not present in the computed facts")
    return _check_label_binding(text, facts, language)


# Clause boundaries. The lookarounds keep Greek thousands separators intact: splitting on a
# bare "." or "," would cut 1.234,5 into pieces and destroy the very figures being checked.
_CLAUSE = re.compile(r"(?<!\d)[.;:·!?\n](?!\d)|(?<!\d),(?!\d)")


def _check_label_binding(
    text: str, facts: FactTable | None, language: str
) -> VerificationResult:
    """Reject a real figure attached to the wrong label.

    Membership alone is not enough: every value in the fact table is "present in the facts",
    so "Η Αίγυπτος κατέγραψε 40.000" passes a set check while naming Syria's figure. Within a
    clause that names exactly one category, any figure belonging to a different one is wrong.
    """
    if facts is None or not facts.facts:
        return VerificationResult(ok=True)
    by_value: dict[str, set[str]] = {}
    for fact in facts.facts:
        by_value.setdefault(normalise_number(str(fact.value), language), set()).add(
            fold(fact.label)
        )
    labels = {fold(fact.label) for fact in facts.facts}

    for clause in _CLAUSE.split(text):
        folded = fold(clause)
        named = {label for label in labels if label and label in folded}
        if len(named) != 1:
            continue  # no category named, or several — nothing unambiguous to bind to
        (mentioned,) = named
        for token in _TOKEN.findall(clause):
            if not _PURE_NUMBER.match(token):
                continue
            owners = by_value.get(normalise_number(token, language))
            if owners and mentioned not in owners:
                return VerificationResult(
                    ok=False, rejected_tokens=[token],
                    reason="figure attached to a category it does not belong to",
                )
    return VerificationResult(ok=True)


def _licensed_claims(facts: FactTable | None, complete: bool) -> set[str]:
    """Report which classes of claim the fact table supports."""
    licensed: set[str] = set()
    if facts is None or not complete or facts.truncated_range:
        return licensed
    if facts.truncation_is_categorical:
        return licensed
    if facts.operation in {Operation.SUM, Operation.COUNT} and facts.omitted_categories == 0:
        licensed.add("superlative")  # a complete ranking supports "the largest is X"
    if facts.operation is Operation.NONE and facts.series_field is None and len(facts.series) > 2:
        licensed.add("trend")  # one complete series supports describing its direction
    return licensed


def label_for(fact: Fact) -> str:
    """Return a fact's label — kept here so callers do not reach into the dataclass."""
    return fact.label
