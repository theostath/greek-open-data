"""Typed contract for Phase 6 synthesis (ADR-0007).

The honesty properties here are load-bearing. ``Answer`` cannot be constructed carrying a
chart or facts alongside a refusal, and cannot be constructed without provenance unless it
*is* a refusal — Principle #2 is a structural property, not a convention. ``Fact`` is the only
thing downstream is allowed to state a quantity from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pythia.planning.models import AGGREGATIONS, QueryPlan


class AnswerStatus(StrEnum):
    """Terminal outcome of synthesis for one question."""

    ANSWERED = "answered"  # facts computed and nothing limits the claim
    PARTIAL = "partial"  # facts computed, but coverage or semantics limit what may be said
    REFUSED = "refused"  # no grounded claim is possible


class ColumnRole(StrEnum):
    """What a column *is*, as far as we are prepared to assert.

    Only ``MEASURE`` is aggregable. Everything else is either a label, an opaque code, or a
    number whose arithmetic is meaningless — summing a running total or a price index yields a
    figure that is confident, plausible and wrong.
    """

    MEASURE = "measure"
    DIMENSION = "dimension"
    TEMPORAL = "temporal"
    IDENTIFIER = "identifier"  # row counters and entity codes: 1..n, areaid, ΑΦΜ
    RUNNING_CUMULATIVE = "running_cumulative"  # totalvaccinations: monotonic within a group
    ROW_TOTAL = "row_total"  # equals the sum of its sibling measures on the same row
    INDEX = "index"  # OBS_VALUE: a price index; not summable across activities
    CODE = "code"  # FREQ=M, ACTIVITY=BTE36: groupable, never aggregable
    UNKNOWN = "unknown"  # contested or unprovable — conservatively non-aggregable


#: The single source of truth for what may be summed or averaged.
AGGREGABLE: frozenset[ColumnRole] = frozenset({ColumnRole.MEASURE})


class Operation(StrEnum):
    """How a ``FactTable``'s figures were derived."""

    NONE = "none"  # values reported as published, no arithmetic
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"
    MIN = "min"
    MAX = "max"
    # Not in AGGREGATIONS: the only correct reduction of a running cumulative column is its
    # value at the latest period, summed across the dimension. Summing it over every
    # region-day inflates the truth by orders of magnitude; a bare MAX returns one region.
    LATEST = "latest"
    LISTING = "listing"  # nothing bindable; a bounded row sample only


#: Operations the planner may request, kept in step with Phase 4 so the two cannot drift.
REQUESTABLE: frozenset[Operation] = frozenset(
    Operation(name) for name in AGGREGATIONS if name in set(Operation)
)


class ChartKind(StrEnum):
    """Chart shapes we are willing to emit. No pie (proportion misreads over partial data)
    and no map (CLAUDE.md §5 forbids joining on region names)."""

    LINE = "line"
    BAR = "bar"
    GROUPED_BAR = "grouped_bar"


class CoercedKind(StrEnum):
    """What a column's cells parsed as, after explicit Phase 6 coercion."""

    DECIMAL = "decimal"
    TEMPORAL = "temporal"
    BOOLEAN = "boolean"
    TEXT = "text"


@dataclass(frozen=True)
class CoercedColumn:
    """One column's cells, explicitly converted (or explicitly left as text).

    ``values`` is parallel to the source rows; ``None`` marks a null *or* a sentinel. The two
    counts are kept apart because a column that is 30% ``:``/``Δ/Υ`` is a different kind of
    unreliable from one that is 30% genuinely empty.
    """

    name: str
    kind: CoercedKind
    values: list[Any]
    null_count: int
    sentinel_count: int
    unit: str | None = None  # "%" / "€" — captured, never silently discarded
    scale_hint: str | None = None  # "σε χιλιάδες" — carried into the rendered figure
    flagged_rows: frozenset[int] = frozenset()  # Eurostat p/e/b: provisional, no superlatives

    @property
    def non_null(self) -> int:
        """Count values that survived coercion."""
        return sum(1 for value in self.values if value is not None)


@dataclass(frozen=True)
class Binding:
    """The result of deciding what each column means and which params could be applied."""

    roles: dict[str, ColumnRole]
    coerced: dict[str, CoercedColumn]
    dimension: str | None
    temporal: str | None
    measures: list[str]
    series_key: list[str]  # columns that make a row unique alongside `temporal`
    forbidden_ops: frozenset[Operation]
    unbound: list[str]  # requested params no column could satisfy — surfaced, never dropped
    reshaped: bool
    header_trusted: bool
    observed_range: tuple[str, str] | None = None
    merged_variants: dict[str, list[str]] = field(default_factory=dict)
    constant_columns: dict[str, str] = field(default_factory=dict)  # footer context, not dims
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Fact:
    """One computed figure, with the derivation that makes it inspectable.

    ``basis`` is rendered to the user, so it is written in the answer's language.
    """

    label: str
    value: Decimal | int | str
    basis: str
    n_used: int
    unit: str | None = None


@dataclass(frozen=True)
class FactTable:
    """Everything downstream is permitted to state a quantity from."""

    facts: list[Fact]
    series: list[dict[str, Any]]
    operation: Operation
    row_basis: int
    dimension: str | None = None
    series_field: str | None = None
    measure: str | None = None
    measure_role: ColumnRole | None = None
    truncated_range: bool = False
    truncation_is_categorical: bool = False
    observed_range: tuple[str, str] | None = None
    dimension_ordered: bool = False
    duplicate_key_count: int = 0
    # A total the publisher stated themselves (a ΣΥΝΟΛΟ row) is better evidence than our own
    # sum, and must never be added to it.
    publisher_stated_total: Fact | None = None
    omitted_categories: int = 0


@dataclass(frozen=True)
class ChartSpec:
    """A validated Vega-Lite v5 spec. ``None`` is a legitimate outcome; a fake chart is not."""

    vega_lite: dict[str, Any]
    kind: ChartKind
    title: str
    caveat: str | None = None


@dataclass(frozen=True)
class Footer:
    """Mandatory provenance. Principle #2: no answer without it."""

    dataset_title: str
    publisher: str
    last_updated: str
    dataset_url: str
    source_url: str
    fetched_at: str
    row_coverage: str
    staleness: str
    complete: bool
    resource_id: str = ""
    resource_format: str = ""
    observed_range: tuple[str, str] | None = None

    def __post_init__(self) -> None:
        """Reject a footer that omits provenance.

        ``footer.build`` substitutes an explicit literal ("publisher not recorded in the
        catalogue") for a catalog null, so a metadata gap degrades the *wording* rather than
        raising on the answer path.
        """
        for name in ("dataset_title", "publisher", "last_updated", "dataset_url", "source_url"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"footer field {name!r} must not be empty")


@dataclass(frozen=True)
class RefusalContext:
    """Catalog facts a refusal needs, resolved by the caller.

    ``Candidate`` carries no publisher and there is no ``TableData`` on a refusal path, so
    without this the ``UNSUPPORTED`` refusal cannot name what it promises to name. Resolved by
    the caller for the same reason ``fetch_for_plan`` resolves provenance: ``synthesis/`` does
    no I/O.
    """

    dataset_title: str | None = None
    publisher: str | None = None
    last_updated: str | None = None
    offered_formats: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VerificationResult:
    """Whether a generated narration may be shown."""

    ok: bool
    rejected_tokens: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class Answer:
    """The output of Phase 6."""

    question: str
    language: str  # "el" | "en" — the OUTPUT language, not the detection label
    status: AnswerStatus
    text: str
    plan: QueryPlan  # kept server-side; the JSON encoder does not publish it
    facts: FactTable | None = None
    chart: ChartSpec | None = None
    footer: Footer | None = None
    caveats: list[str] = field(default_factory=list)
    refusal_reason: str | None = None
    degraded: bool = False
    narration_rejected: bool = False

    def __post_init__(self) -> None:
        """Enforce the honesty invariants between status, facts, chart and provenance."""
        if self.status is AnswerStatus.REFUSED:
            if self.facts is not None or self.chart is not None:
                raise ValueError("a refusal cannot carry facts or a chart")
            if not self.refusal_reason:
                raise ValueError("a refusal must state why")
        elif self.footer is None:
            raise ValueError("every non-refused answer must carry provenance (Principle #2)")


def output_language(plan: QueryPlan) -> str:
    """Map a detection label to the language the answer is written in.

    Greeklish is Greek typed on a Latin keyboard, so the asker reads Greek — ADR-0005 already
    transliterates it for retrieval and this is the same call one layer on.
    """
    return "el" if plan.language in {"el", "greeklish"} else "en"
