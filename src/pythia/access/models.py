"""Typed contract for Phase 5 access output (ADR-0006).

``TableData`` is what Phase 6 synthesises from, so its honesty properties are load-bearing:
``complete`` has **no default** — every construction site must state whether the table is the
whole truth — and ``source_url`` is always the catalog's resource URL, never a resolved or
redirected one (those carry signed credentials).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Column types we are willing to assert. Deliberately source-level, not presentation-level:
# mapping these to Vega-Lite encodings is Phase 6's job, not this layer's.
COLUMN_TYPES = frozenset({"integer", "number", "boolean", "date", "timestamp", "text"})


class IncompleteReason(StrEnum):
    """Why a table is not the complete resource."""

    ROW_CAP = "row_cap"  # our configured row budget
    BYTE_CAP = "byte_cap"  # our configured byte budget
    PAGE_STOP = "page_stop"  # DataStore paging ended before total
    UPSTREAM_CAP = "upstream_cap"  # the server itself limited the result


class AccessError(Exception):
    """Base for every typed failure Phase 6 can turn into an honest message."""


class UnsupportedResourceError(AccessError):
    """The resource is something we will not handle (format, scheme, shape)."""


class MalformedPayloadError(AccessError):
    """The bytes are not the thing the catalog declared them to be."""


class ResourceUnavailableError(AccessError):
    """Upstream failed: status, network, deadline, or throughput floor."""


class NoMatchError(AccessError):
    """The plan did not identify a dataset — distinct from 'bad format'."""


@dataclass(frozen=True)
class Column:
    """One column with the type we are prepared to assert about it."""

    name: str
    type: str

    def __post_init__(self) -> None:
        """Reject a type outside the declared vocabulary."""
        if self.type not in COLUMN_TYPES:
            raise ValueError(f"unknown column type: {self.type!r}")


@dataclass(frozen=True)
class TableData:
    """Rows + columns + provenance for exactly one resource.

    Values are ``str | None`` and are **never silently coerced**: Greek exports use ``,`` as a
    decimal separator, so eager float parsing would corrupt figures. ``Column.type`` carries
    the inference; Phase 6 converts explicitly.
    """

    resource_id: str
    dataset_id: str
    columns: list[Column]
    rows: list[dict[str, str | None]]
    row_count: int
    complete: bool  # NO DEFAULT: overstating completeness is the failure this guards
    # Also no default, for the same reason one layer over: Greek exports put a title banner
    # above the real header, so column names are not always the publisher's column names.
    # Downstream renders these labels and feeds them to an LLM; silently asserting they are
    # trustworthy is the same class of overstatement as claiming completeness.
    header_trusted: bool
    access_path: str  # "datastore" | "download"
    source_url: str  # the CKAN resource URL, never a resolved/signed one
    fetched_at: str  # ISO-8601
    incomplete_reason: IncompleteReason | None = None
    upstream_total: int | None = None  # lets Phase 6 say "5,000 of 812,000"
    # Provenance for the mandatory Phase 6 footer (Principle #2).
    dataset_title: str | None = None
    publisher: str | None = None
    last_updated: str | None = None
    off_portal: bool = False
    transport_scheme: str = "https"
    encoding: str | None = None
    delimiter: str | None = None
    bytes_read: int = 0
    from_cache: bool = False
    # Intent params Phase 4 extracted that this layer deliberately did not apply, so Phase 6
    # filters uniformly across both access paths (see ADR-0006).
    deferred_params: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Enforce the honesty invariant between ``complete`` and ``incomplete_reason``."""
        if self.complete and self.incomplete_reason is not None:
            raise ValueError("complete=True cannot carry an incomplete_reason")
        if not self.complete and self.incomplete_reason is None:
            raise ValueError("complete=False requires an incomplete_reason")
