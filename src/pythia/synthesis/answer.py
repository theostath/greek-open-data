"""Phase 6 orchestrator: (question, plan, table) -> a grounded ``Answer`` or an honest refusal.

Refusals are the common case, not the error path: on the golden set 12/26 questions find no
dataset and 6/26 find one that publishes nothing tabular. Each refusal names what *is* there,
so the answer is useful even when it is "no".
"""

from __future__ import annotations

import logging
from time import perf_counter

from config import Settings, get_settings

from pythia.access.models import (
    AccessError,
    MalformedPayloadError,
    ResourceUnavailableError,
    TableData,
    UnsupportedResourceError,
)
from pythia.llm import LLMClient
from pythia.logging_setup import get_logger, log_event
from pythia.planning.models import PlanStatus, QueryPlan
from pythia.synthesis import bind as bind_mod
from pythia.synthesis import chart as chart_mod
from pythia.synthesis import compute as compute_mod
from pythia.synthesis import footer as footer_mod
from pythia.synthesis import narrate, verify
from pythia.synthesis.models import (
    Answer,
    AnswerStatus,
    Binding,
    ColumnRole,
    FactTable,
    Footer,
    Operation,
    RefusalContext,
    output_language,
)

LOGGER_NAME = "pythia.synthesis.answer"


def answer_question(
    question: str,
    plan: QueryPlan,
    table: TableData | None = None,
    *,
    error: AccessError | None = None,
    refusal_ctx: RefusalContext | None = None,
    llm: LLMClient | None = None,
    settings: Settings | None = None,
) -> Answer:
    """Produce a grounded answer, a qualified partial answer, or a typed refusal."""
    cfg = settings or get_settings()
    logger = get_logger(LOGGER_NAME)
    started = perf_counter()
    language = output_language(plan)

    if error is not None:
        reason = _access_reason(error, language)
        return _log(logger, started, _refuse(question, plan, language, reason))
    if plan.status is not PlanStatus.MATCHED:
        return _log(logger, started, _refuse_plan(question, plan, language, refusal_ctx))
    if table is None:
        raise ValueError("a MATCHED plan needs either a table or an error to synthesise from")

    try:
        binding = bind_mod.bind_columns(table, plan.params, settings=cfg)
    except bind_mod.TableTooLargeError as exc:
        return _log(logger, started, _refuse(question, plan, language, str(exc)))

    overlap = bind_mod.range_overlap(
        binding.observed_range, plan.params.date_from, plan.params.date_to
    )
    if overlap == "none":
        start, end = binding.observed_range or ("", "")
        reason = (
            f"τα δεδομένα καλύπτουν {start} έως {end}· δεν καλύπτουν την περίοδο που ζητήθηκε"
            if language == "el" else
            f"the data covers {start} to {end}; it does not cover the period requested"
        )
        return _log(logger, started, _refuse(question, plan, language, reason))

    facts = compute_mod.summarise(table, binding, plan.params, settings=cfg, language=language)
    foot = footer_mod.build(
        table, dataset_name=plan.dataset.name if plan.dataset else None,
        language=language, observed_range=binding.observed_range, settings=cfg,
    )
    if facts is None:
        reason = (
            "δεν υπάρχουν γραμμές που να ταιριάζουν με το ερώτημα"
            if language == "el" else "no rows match the question"
        )
        # Keep the footer built just above: Answer's invariant forbids facts and charts on a
        # refusal, not provenance, and "the right dataset, but no rows match" is far more
        # useful with the dataset, publisher and freshness attached (Principle #2).
        return _log(logger, started, _refuse(question, plan, language, reason, footer=foot))

    caveats = _caveats(table, binding, facts, overlap, language)
    limitation = caveats[0] if caveats else None
    status = _status(table, binding, facts, caveats)

    chart = None
    if facts.operation is not Operation.LISTING:
        chart = chart_mod.build_spec(
            facts, title=foot.dataset_title, caveat=limitation,
            complete=table.complete, label=binding.dimension or "",
            # The authoritative temporal signal. Without it build_spec falls back to a
            # prefix heuristic on a dimension value, which mistook year-like categories
            # for a time series and real dates in other formats for categories.
            temporal_column=binding.temporal, settings=cfg,
        )

    text, degraded, rejected = _narrate(
        question, facts, foot, binding, language, limitation, llm, cfg, table.complete
    )
    return _log(logger, started, Answer(
        question=question, language=language, status=status, text=text, plan=plan,
        facts=facts, chart=chart, footer=foot, caveats=caveats,
        degraded=degraded or plan.degraded, narration_rejected=rejected,
    ))


def _narrate(
    question: str, facts: FactTable, foot: object, binding: object, language: str,
    limitation: str | None, llm: LLMClient | None, cfg: Settings, complete: bool,
) -> tuple[str, bool, bool]:
    """Write the prose, falling back to the template whenever the guard is not satisfied."""
    template = narrate.render_template(
        facts, foot, language=language, question=question, limitation=limitation  # type: ignore[arg-type]
    )
    if llm is None:
        return template, True, False
    drafted = narrate.write(
        question, facts, foot, language=language, llm=llm,  # type: ignore[arg-type]
        operation=facts.operation, limitation=limitation,
        max_tokens=cfg.synthesis_max_narration_tokens,
        max_prompt_bytes=cfg.synthesis_max_prompt_bytes,
    )
    if drafted is None:
        return template, True, False
    draft, mapping = drafted
    # Verify the placeholder text, then substitute. Checking after substitution would let a
    # label's own digits masquerade as a licensed figure.
    result = verify.check_claims(
        narrate.substitute(draft, mapping), facts, foot,  # type: ignore[arg-type]
        language=language, complete=complete, limitation=limitation,
    )
    if not result.ok:
        log_event(
            get_logger(LOGGER_NAME), logging.INFO, "synthesis.narration_rejected",
            reason=result.reason,
            # Truncated: a rejected token is row-derived and may be personal data.
            tokens=[token[:12] for token in result.rejected_tokens[:3]],
        )
        return template, True, True
    return narrate.substitute(draft, mapping), False, False


def _status(
    table: TableData, binding: Binding, facts: FactTable, caveats: list[str]
) -> AnswerStatus:
    """Decide between a clean answer and a qualified one."""
    if facts.operation is Operation.LISTING:
        return AnswerStatus.PARTIAL
    limited = (
        caveats
        or not table.complete
        or not binding.header_trusted
        or binding.unbound
        or facts.truncated_range
        or facts.truncation_is_categorical
    )
    return AnswerStatus.PARTIAL if limited else AnswerStatus.ANSWERED


def _caveats(
    table: TableData, binding: Binding, facts: FactTable, overlap: str, language: str
) -> list[str]:
    """State every limitation on the claim, most important first."""
    el = language == "el"
    out: list[str] = []
    if facts.truncation_is_categorical:
        out.append(
            "Τα δεδομένα κόπηκαν στη μέση: ολόκληρες κατηγορίες μπορεί να λείπουν."
            if el else
            "The data was cut short: whole categories may be missing entirely."
        )
    elif not table.complete and facts.observed_range:
        start, end = facts.observed_range
        out.append(
            f"Τα δεδομένα που ανακτήθηκαν καλύπτουν μόνο {start} έως {end}."
            if el else
            f"The retrieved data covers only {start} to {end}."
        )
    elif not table.complete:
        out.append(
            "Ανακτήθηκε μέρος μόνο των δεδομένων." if el else "Only part of the data was retrieved."
        )
    if facts.measure_role in {ColumnRole.RUNNING_CUMULATIVE, ColumnRole.INDEX}:
        out.append(
            "Η στήλη είναι αθροιστική ή δείκτης, οπότε δεν αθροίζεται."
            if el
            else "The column is cumulative or an index, so it is not summed."
        )
    if overlap == "partial" and facts.observed_range:
        start, end = facts.observed_range
        out.append(
            f"Η ζητούμενη περίοδος καλύπτεται μόνο εν μέρει ({start}–{end})."
            if el else
            f"The requested period is only partly covered ({start}–{end})."
        )
    if not binding.header_trusted:
        out.append(
            "Οι επικεφαλίδες του αρχείου δεν είναι αξιόπιστες· τα ονόματα στηλών δεν ερμηνεύονται."
            if el
            else "The file's headers are unreliable, so column names are not interpreted."
        )
    if binding.merged_variants:
        out.append(
            "Παραλλαγές γραφής της ίδιας κατηγορίας ενοποιήθηκαν."
            if el else "Spelling variants of the same category were merged."
        )
    if facts.omitted_categories:
        out.append(
            f"Εμφανίζονται οι μεγαλύτερες κατηγορίες· άλλες {facts.omitted_categories} "
            "ομαδοποιήθηκαν."
            if el
            else f"The largest categories are shown; {facts.omitted_categories} others "
            "were grouped."
        )
    for note in binding.notes:
        out.append(note)
    for unbound in binding.unbound:
        out.append(
            f"Το φίλτρο «{unbound}» δεν μπόρεσε να εφαρμοστεί σε αυτό το σύνολο δεδομένων."
            if el else
            f"The '{unbound}' filter could not be applied to this dataset."
        )
    return out


def _refuse_plan(
    question: str, plan: QueryPlan, language: str, ctx: RefusalContext | None
) -> Answer:
    """Render the two planning refusals, naming whatever the catalogue does offer."""
    el = language == "el"
    if plan.status is PlanStatus.UNSUPPORTED:
        fallback = plan.dataset.title if plan.dataset else ""
        title = (ctx.dataset_title if ctx else None) or fallback or "—"
        unknown = "άγνωστος φορέας" if el else "unknown publisher"
        publisher = (ctx.publisher if ctx else None) or unknown
        formats = ", ".join(ctx.offered_formats) if ctx and ctx.offered_formats else "—"
        reason = (
            f"Το σύνολο δεδομένων «{title}» ({publisher}) υπάρχει, αλλά ο κατάλογος δεν "
            f"δημοσιεύει CSV ή JSON γι' αυτό — διαθέσιμες μορφές: {formats}."
            if el else
            f"The dataset '{title}' ({publisher}) exists, but the catalogue lists no CSV or "
            f"JSON for it — formats listed: {formats}."
        )
    else:
        closest = ", ".join(
            candidate.title or candidate.name for candidate in plan.candidates[:3] if candidate
        )
        reason = (
            "Κανένα σύνολο δεδομένων του καταλόγου δεν καλύπτει αυτό το ερώτημα."
            if el else
            "No dataset in the catalogue covers this question."
        )
        if closest:
            reason += (
                f" Πλησιέστερα, χωρίς να ταιριάζουν: {closest}."
                if el else f" Closest, though not a match: {closest}."
            )
    return _refuse(question, plan, language, reason)


def _access_reason(error: AccessError, language: str) -> str:
    """Map a typed access failure to a message that blames the right thing."""
    el = language == "el"
    if isinstance(error, ResourceUnavailableError):
        return (
            "Ο διακομιστής του φορέα δεν απάντησε, οπότε τα δεδομένα δεν ανακτήθηκαν."
            if el else
            "The publisher's server did not respond, so the data could not be retrieved."
        )
    if isinstance(error, MalformedPayloadError):
        return (
            "Το αρχείο δεν είχε τη μορφή που δηλώνει ο κατάλογος, οπότε δεν διαβάστηκε."
            if el else
            "The file was not the format the catalogue declares, so it was not read."
        )
    if isinstance(error, UnsupportedResourceError):
        return (
            "Ο πόρος είναι σε μορφή που δεν υποστηρίζεται ακόμη."
            if el else "The resource is in a format that is not supported yet."
        )
    return "Τα δεδομένα δεν ανακτήθηκαν." if el else "The data could not be retrieved."


def _refuse(
    question: str, plan: QueryPlan, language: str, reason: str, *, footer: Footer | None = None
) -> Answer:
    """Build a refusal. It carries no facts and no chart, by construction.

    A footer is optional and passed only where one was actually built — most refusal paths
    never fetched anything, so they have no provenance to state.
    """
    return Answer(
        question=question, language=language, status=AnswerStatus.REFUSED, text=reason,
        plan=plan, refusal_reason=reason, degraded=plan.degraded, footer=footer,
    )


def _log(logger: logging.Logger, started: float, answer: Answer) -> Answer:
    """Emit one structured event per answer. Never row values, never the question."""
    log_event(
        logger, logging.INFO, "synthesis.done",
        status=answer.status.value,
        operation=answer.facts.operation.value if answer.facts else None,
        row_basis=answer.facts.row_basis if answer.facts else 0,
        complete=answer.footer.complete if answer.footer else None,
        chart_kind=answer.chart.kind.value if answer.chart else None,
        narration_rejected=answer.narration_rejected,
        degraded=answer.degraded,
        caveats=len(answer.caveats),
        language=answer.language,
        latency_ms=round((perf_counter() - started) * 1000),
    )
    return answer


def main(argv: list[str] | None = None) -> int:
    """Answer one question end to end (``make answer QUESTION="…"``)."""
    import argparse
    import json

    # Phase 7 owns the orchestration both entrypoints share (ADR-0008). Imported inside the
    # CLI function, not at module scope, so ``synthesis/`` keeps no import-time dependency on
    # the layer above it.
    from pythia.api.service import Pipeline

    parser = argparse.ArgumentParser(description="Answer one question from the catalogue.")
    parser.add_argument("--question", required=True)
    # Lets the synthesis path be exercised independently of retrieval, which otherwise
    # dominates the outcome (golden-set MRR is 0.544).
    parser.add_argument("--resource-id", default=None)
    parser.add_argument("--no-llm", action="store_true", help="force the template path")
    args = parser.parse_args(argv)

    if args.no_llm and not args.resource_id:
        raise SystemExit("planning needs an LLM; drop --no-llm or pass --resource-id")

    pipeline = Pipeline.create(get_settings(), with_llm=not args.no_llm)
    try:
        answer = pipeline.answer(args.question, resource_id=args.resource_id).answer
    except ValueError as exc:  # a bad --resource-id is user error, not a crash
        raise SystemExit(str(exc)) from exc
    finally:
        pipeline.close()

    print(f"\n[{answer.status.value}] {answer.text}\n")
    if answer.caveats:
        print("Caveats:")
        for caveat in answer.caveats:
            print(f"  - {caveat}")
    if answer.footer:
        print(f"\nSource: {answer.footer.dataset_url}  ({answer.footer.row_coverage})")
    if answer.chart:
        print("\nECharts option:")
        print(json.dumps(answer.chart.option, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
