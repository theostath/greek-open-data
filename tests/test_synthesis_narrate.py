"""Tests for the narration boundary: what the model is shown, and what it must copy back.

The limitation is the one sentence an answer may not lose. ADR-0007 keeps figures honest by
handing the model opaque tokens and substituting the real strings back only after the guard
accepts — but the limitation was the exception. It was prose the model was asked to reproduce
from memory, while `verify.check_claims` demanded the first 24 folded characters verbatim, so
the model paraphrased and every caveat-carrying answer degraded to the template.
"""

from __future__ import annotations

from decimal import Decimal

from tests.synthesis_fixtures import asylum_table, plan

from pythia.access.models import IncompleteReason
from pythia.llm import FakeLLM
from pythia.synthesis import narrate
from pythia.synthesis.answer import answer_question
from pythia.synthesis.models import Fact, FactTable, Operation

LIMITATION = "Ανακτήθηκε μέρος μόνο των δεδομένων."


def facts() -> FactTable:
    """A minimal ranked fact table."""
    return FactTable(
        facts=[Fact(label="ΑΙΓΥΠΤΟΣ", value=Decimal(7547), basis="b", n_used=4)],
        series=[{"dim": "ΑΙΓΥΠΤΟΣ", "value": Decimal(7547)}],
        operation=Operation.SUM, row_basis=4, dimension="Χώρα", measure="Πλήθος",
    )


def truncated_answer(answer_text: str):  # type: ignore[no-untyped-def]
    """Run a caveat-carrying question through the pipeline with a scripted narration."""
    llm = FakeLLM({"answer": answer_text})
    table = asylum_table(complete=False, reason=IncompleteReason.ROW_CAP)
    return answer_question("πόσα αιτήματα ασύλου;", plan(), table, llm=llm), llm


def test_limitation_is_mapped_to_a_token() -> None:
    """The substitution table owns the token vocabulary, so the limitation belongs in it."""
    _, mapping = narrate.build_placeholders(facts(), "el", limitation=LIMITATION)
    assert mapping["{LIMITATION}"] == LIMITATION


def test_no_limitation_leaves_the_token_unmapped() -> None:
    """An unconstrained answer must not carry a token that stands for nothing."""
    _, mapping = narrate.build_placeholders(facts(), "el")
    assert "{LIMITATION}" not in mapping


def test_the_model_is_shown_both_the_token_and_the_sentence() -> None:
    """The token is what it must copy; the text is what stops it contradicting the sentence.

    Showing the text is safe because every caveat is one of our own hard-coded sentences,
    interpolating only dates and counts — no publisher-controlled cell reaches it.
    """
    answer, llm = truncated_answer("{LIMITATION} Η κατηγορία {LABEL_1} έχει {FACT_1}.")
    sent = " ".join(message["content"] for call in llm.calls for message in call)
    assert "{LIMITATION}" in sent
    assert answer.caveats[0] in sent


def test_a_narration_carrying_the_token_survives_the_guard() -> None:
    """The defect this fixes: a caveat no longer costs the model's prose."""
    answer, _ = truncated_answer("{LIMITATION} Η κατηγορία {LABEL_1} έχει {FACT_1}.")
    assert answer.caveats
    assert answer.narration_rejected is False
    assert answer.caveats[0] in answer.text


def test_a_paraphrased_limitation_is_still_rejected() -> None:
    """The guarantee, unchanged: only our exact sentence counts as stating the limitation.

    A regression guard rather than a driver — this is the behaviour that must survive the fix.
    """
    answer, _ = truncated_answer("Μέρος των δεδομένων λείπει. {LABEL_1}: {FACT_1}.")
    assert answer.narration_rejected is True
