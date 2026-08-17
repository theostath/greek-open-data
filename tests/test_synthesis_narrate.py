"""Tests for the narration boundary: what the model is shown, and what it may not decide.

The limitation is the one sentence an answer may not lose. It used to be prose the model was
asked to reproduce, while `verify.check_claims` demanded the first 24 folded characters
verbatim — so the model paraphrased and every caveat-carrying answer degraded to the template.
Measured live afterwards, asking it to copy a token instead worked (the guard accepted) but the
model restated the sentence in its own words as well, twice out of two runs.

So the limitation is no longer the model's job at all. Python appends it to the accepted
narration, the model never learns it exists, and `check_claims` keeps asserting it on the
published string. What that removes is the guard's *accidental* protection: an answer denying
the truncation used to be caught for omitting the caveat, so `COMPLETENESS_WORDS` now catches
it for what it actually is.
"""

from __future__ import annotations

from tests.synthesis_fixtures import asylum_table, plan

from pythia.access.models import IncompleteReason
from pythia.llm import FakeLLM
from pythia.synthesis.answer import answer_question

NARRATION = "Η κατηγορία {LABEL_1} καταγράφει {FACT_1}."


def truncated_answer(answer_text: str):  # type: ignore[no-untyped-def]
    """Run a caveat-carrying question through the pipeline with a scripted narration."""
    llm = FakeLLM({"answer": answer_text})
    table = asylum_table(complete=False, reason=IncompleteReason.ROW_CAP)
    return answer_question("πόσα αιτήματα ασύλου;", plan(), table, llm=llm), llm


def test_the_model_is_never_shown_the_limitation() -> None:
    """It cannot restate, soften or contradict a sentence it was never given."""
    answer, llm = truncated_answer(NARRATION)
    sent = " ".join(message["content"] for call in llm.calls for message in call)
    assert answer.caveats
    assert answer.caveats[0] not in sent
    assert "LIMITATION" not in sent


def test_the_limitation_is_appended_to_an_accepted_narration() -> None:
    """Presence by construction: the model's compliance is no longer what carries it."""
    answer, _ = truncated_answer(NARRATION)
    assert answer.narration_rejected is False
    assert answer.caveats[0] in answer.text
    assert answer.text.index(answer.caveats[0]) > 0  # the facts come first, the caveat after


def test_a_narration_denying_the_truncation_is_rejected() -> None:
    """The property the injection suite protects, now enforced rather than incidental.

    Appending the caveat unconditionally means omission can no longer betray such an answer.
    """
    answer, _ = truncated_answer(f"Τα δεδομένα είναι πλήρη. {NARRATION}")
    assert answer.narration_rejected is True
    assert answer.caveats[0] in answer.text  # the template still states it


def test_a_rejected_narration_still_falls_back_to_the_template() -> None:
    """Failing closed must remain cheap rather than costing the answer."""
    answer, _ = truncated_answer("Καταγράφηκαν 999.999 αιτήματα.")
    assert answer.narration_rejected is True
    assert answer.text.strip()
