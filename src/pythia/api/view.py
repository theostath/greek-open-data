"""The publish whitelist: ``(Answer, RecoveryContext)`` -> what a template may read.

``Answer.plan`` carries the ranked retrieval shortlist with RRF scores, and its own docstring
says it is kept server-side. Rather than omitting it and trusting every future template, this
module names every field that *is* published — so a field added to ``QueryPlan`` later is
invisible by default.

Figures are formatted here, once, with ``footer.format_number``. That function is the canonical
formatter precisely because the guard's normalisation and the rendered text must not disagree
about what "1.234,5" means, so a template must never format a number itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from config import Settings

from pythia.api.service import NearMiss, RecoveryContext
from pythia.planning.models import PlanStatus
from pythia.synthesis.footer import format_number, staleness_step
from pythia.synthesis.models import Answer, AnswerStatus, Footer


class RefusalShape(StrEnum):
    """Which refusal this is. Three, not two — they need three different screens."""

    #: Nothing in the catalogue covers the question. Offer what was looked at.
    NO_MATCH = "no_match"
    #: The dataset exists but publishes nothing tabular. Name what it does publish.
    UNSUPPORTED = "unsupported"
    #: Planning MATCHED and synthesis still refused — most often the requested period falls
    #: outside the data's observed range. Retrieval succeeded, so this dataset is NOT a near
    #: miss, and saying otherwise tells the user the opposite of what happened.
    MATCHED_BUT_REFUSED = "matched_but_refused"


@dataclass(frozen=True)
class FactView:
    """One figure, already rendered in the reader's locale."""

    label: str
    value: str
    basis: str
    unit: str | None = None


@dataclass(frozen=True)
class RefusalView:
    """Everything a refusal screen may show, and nothing else."""

    shape: RefusalShape
    reason: str
    near_misses: list[NearMiss] = field(default_factory=list)
    offered_formats: list[str] = field(default_factory=list)
    normalized_question: str = ""
    matched_title: str | None = None
    matched_last_updated: str | None = None


@dataclass(frozen=True)
class AnswerView:
    """The whole publishable surface of an answer. No ``plan`` field, by construction."""

    question: str
    language: str
    status: AnswerStatus
    text: str
    facts: list[FactView] = field(default_factory=list)
    #: The exact dict ``validate_spec`` approved — never rebuilt, or the guard is bypassed.
    chart: dict[str, Any] | None = None
    chart_caveat: str | None = None
    chart_title: str = ""
    footer: Footer | None = None
    #: 1–4 (fresh → possibly abandoned), 0 when unknown. Derived from ``footer.py``'s own
    #: thresholds so the indicator can never disagree with the sentence beside it.
    staleness_step: int = 0
    caveats: list[str] = field(default_factory=list)
    refusal: RefusalView | None = None
    #: Kept apart because ``Answer.degraded`` ORs them and the wording differs: one says the
    #: prose is templated, the other says the dataset was chosen on a score floor.
    narration_degraded: bool = False
    planning_degraded: bool = False
    narration_rejected: bool = False


def _shape(answer: Answer, recovery: RecoveryContext) -> RefusalShape:
    """Classify a refusal. ``matched_but_refused`` wins: it is the case that misleads."""
    if recovery.matched_but_refused:
        return RefusalShape.MATCHED_BUT_REFUSED
    if answer.plan.status is PlanStatus.UNSUPPORTED:
        return RefusalShape.UNSUPPORTED
    return RefusalShape.NO_MATCH


def to_view(answer: Answer, recovery: RecoveryContext, *, settings: Settings) -> AnswerView:
    """Map an answer and its recovery context onto the fields templates may read."""
    refusal = None
    if answer.status is AnswerStatus.REFUSED:
        shape = _shape(answer, recovery)
        refusal = RefusalView(
            shape=shape,
            reason=answer.refusal_reason or answer.text,
            # Near misses answer "what did you look at?", which is only honest when nothing
            # was chosen. Guarded here as well as in the recovery context so neither layer
            # alone can reintroduce the defect.
            near_misses=list(recovery.near_misses) if shape is RefusalShape.NO_MATCH else [],
            offered_formats=list(recovery.offered_formats),
            normalized_question=recovery.normalized_question,
            matched_title=recovery.matched_title,
            matched_last_updated=recovery.matched_last_updated,
        )

    facts = [
        FactView(
            label=fact.label,
            value=format_number(fact.value, answer.language),
            basis=fact.basis,
            unit=fact.unit,
        )
        for fact in (answer.facts.facts if answer.facts else [])
    ]

    return AnswerView(
        question=answer.question,
        language=answer.language,
        status=answer.status,
        text=answer.text,
        facts=facts,
        chart=answer.chart.vega_lite if answer.chart else None,
        chart_caveat=answer.chart.caveat if answer.chart else None,
        chart_title=answer.chart.title if answer.chart else "",
        footer=answer.footer,
        staleness_step=staleness_step(answer.footer.last_updated, cfg=settings)
        if answer.footer else 0,
        caveats=list(answer.caveats),
        refusal=refusal,
        # ``Answer.degraded`` is ``narration_degraded or plan.degraded``, so subtracting the
        # planning half is the only way back to what the narrator actually did.
        narration_degraded=answer.degraded and not recovery.planning_degraded,
        planning_degraded=recovery.planning_degraded,
        narration_rejected=answer.narration_rejected,
    )
