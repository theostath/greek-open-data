"""Shared table fixtures for the Phase 6 suite, shaped from the four live-probed resources.

Every trap these encode was measured against the real portal on 2026-07-29, not invented.
"""

from __future__ import annotations

from typing import Any

from pythia.access.models import Column, IncompleteReason, TableData
from pythia.planning.models import PlanStatus, QueryParams, QueryPlan
from pythia.retrieval.search import Candidate


def table(
    columns: list[tuple[str, str]],
    rows: list[dict[str, Any]],
    *,
    complete: bool = True,
    reason: IncompleteReason | None = None,
    title: str = "Σύνολο δεδομένων",
    publisher: str = "Ελληνική Κυβέρνηση",
    last_updated: str = "2026-05-11T17:25:52",
    header_trusted: bool = True,
) -> TableData:
    """Build a TableData with sensible provenance defaults."""
    return TableData(
        resource_id="res-1", dataset_id="ds-1",
        columns=[Column(name=name, type=kind) for name, kind in columns],
        rows=rows, row_count=len(rows), complete=complete, incomplete_reason=reason,
        header_trusted=header_trusted, access_path="datastore", source_url="https://data.gov.gr/x",
        fetched_at="2026-07-29T12:00:00+00:00", dataset_title=title, publisher=publisher,
        last_updated=last_updated,
    )


def plan(
    question: str = "ερώτηση", status: PlanStatus = PlanStatus.MATCHED,
    language: str = "el", params: QueryParams | None = None,
) -> QueryPlan:
    """Build a QueryPlan in the given terminal state."""
    candidate = Candidate(id="ds-1", name="dataset-slug", title="Σύνολο δεδομένων",
                          last_updated="2026-05-11T17:25:52", rank=1, score=0.5)
    return QueryPlan(
        question=question, normalized_question=question, language=language, status=status,
        dataset=candidate, resource_id="res-1", resource_format="CSV",
        resource_url="https://data.gov.gr/x", access_path="datastore",
        params=params or QueryParams(), confidence=0.7, reason="test", degraded=False,
        candidates=[candidate],
    )


#: q16 asylum-by-nationality. The ΣΥΝΟΛΟ row equals the sum of the others, so a naive total
#: is exactly 2x the truth — measured live: parts sum to 73,687 and the row reads 73,687.
ASYLUM_PARTS = [("ΑΙΓΥΠΤΟΣ", "7547"), ("ΣΥΡΙΑ", "40000"),
                ("ΑΦΓΑΝΙΣΤΑΝ", "26011"), ("ΑΙΘΙΟΠΙΑ", "129")]
ASYLUM_TOTAL = 73687


def asylum_table(**kwargs: Any) -> TableData:
    """q16: one label column, one measure column, plus an embedded ΣΥΝΟΛΟ row."""
    rows = [{"UPEKOOTETA": name, "ARIThMOS AITEMATON": value} for name, value in ASYLUM_PARTS]
    rows.append({"UPEKOOTETA": "ΣΥΝΟΛΟ", "ARIThMOS AITEMATON": str(ASYLUM_TOTAL)})
    kwargs.setdefault("title", "ΑΙΤΗΜΑΤΑ ΑΣΥΛΟΥ 2024 ΑΝΑ ΥΠΗΚΟΟΤΗΤΑ")
    return table([("UPEKOOTETA", "text"), ("ARIThMOS AITEMATON", "number")], rows, **kwargs)


def vaccination_table(**kwargs: Any) -> TableData:
    """q03: a daily regional panel with cumulative totals beside signed daily deltas."""
    rows = []
    for step, day in enumerate(["2023-12-01", "2023-12-02", "2023-12-03"]):
        for area, base in [("ΑΤΤΙΚΗΣ", 100), ("ΚΡΗΤΗΣ", 200)]:
            rows.append({
                "referencedate": f"{day} 00:00:00", "area": area, "areaid": str(700 + base),
                "daytotal": ["5", "2", "9"][step], "daydiff": ["-3", "7", "-1"][step],
                "totalvaccinations": str(base + 10 * step),
            })
    return table(
        [("referencedate", "timestamp"), ("area", "text"), ("areaid", "integer"),
         ("daytotal", "integer"), ("daydiff", "integer"), ("totalvaccinations", "integer")],
        rows, **{"title": "Στατιστικά εμβολιασμού για τον COVID-19", **kwargs},
    )


def index_table(*, complete: bool = False, **kwargs: Any) -> TableData:
    """q14: SDMX long format — many interleaved series, decimal-comma values, truncated."""
    rows = []
    for period in ["2010-01", "2010-02", "2010-03"]:
        for activity, value in [("BTE36", "86,6"), ("C141", "95,8")]:
            rows.append({"FREQ": "M", "ACTIVITY": activity, "BASE_PER": "2021",
                         "TIME_PERIOD": period, "OBS_VALUE": value})
    return table(
        [("FREQ", "text"), ("ACTIVITY", "text"), ("BASE_PER", "number"),
         ("TIME_PERIOD", "text"), ("OBS_VALUE", "text")],
        rows, complete=complete,
        reason=IncompleteReason.ROW_CAP if not complete else None,
        **{"title": "ΔΕΙΚΤΗΣ ΤΙΜΩΝ ΠΑΡΑΓΩΓΟΥ ΣΤΗ ΒΙΟΜΗΧΑΝΙΑ", **kwargs},
    )
