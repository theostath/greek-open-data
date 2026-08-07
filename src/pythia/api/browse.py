"""Catalogue browse: deterministic SQL over publishers, themes and datasets (issue #18).

Why this exists at all: retrieval is the measured ceiling — R@1 is 0.46, so only 12 of 26
golden questions put the correct dataset first. A user who cannot phrase a question the way a
public body would is fighting the weakest component in the system. Browsing sidesteps it:
pick a dataset by hand, and ``Pipeline.answer(resource_id=...)`` skips retrieval entirely.

**Everything here is plain SQL.** No embeddings, no LLM. A browse result cannot be wrong in the
way a retrieval result can, which is the property that makes this a safe front door.

Two design decisions the catalogue forced, both measured on 2026-08-06:

1. **Geography comes from ``org_title``, not from ``spatial_text`` and not from free text.**
   ``spatial_text`` is populated on 41.7% of datasets but 7,600 of 9,101 populated rows just
   say ``Ελλάδα``. Free-text place matching is worse than useless — searching for Ιωάννινα
   returns **0** datasets while Δήμος Ιωαννιτών publishes plenty, so it would confidently
   report the opposite of the truth. Greek open data is published *by municipality*.
2. **Everything is filtered to datasets with a CSV or JSON resource.** Only 24.4% of the
   catalogue publishes anything tabular, so an unfiltered browse would be a machine for
   generating ``unsupported`` refusals.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum

from pythia.planning.select import select_resource

#: Datasets we are willing to offer: those with at least one resource we can actually read.
_TABULAR = (
    "SELECT DISTINCT dataset_id FROM resources WHERE upper(format) IN ('CSV', 'JSON')"
)

#: DCAT-AP theme codes → English labels. Chrome is English (PRODUCT.md); answers follow the
#: question's language. An unlisted code falls back to itself rather than breaking the page —
#: upstream can add one at any time.
_THEME_LABELS = {
    "AGRI": "Agriculture, fisheries and food",
    "ECON": "Economy and finance",
    "EDUC": "Education, culture and sport",
    "ENER": "Energy",
    "ENVI": "Environment",
    "GOVE": "Government and public sector",
    "HEAL": "Health",
    "INTR": "International issues",
    "JUST": "Justice and legal system",
    "REGI": "Regions and cities",
    "SOCI": "Population and society",
    "TECH": "Science and technology",
    "TRAN": "Transport",
}

# Anchored at the start: "Δήμος" opens a municipality's name. "Υπουργείο" is matched anywhere,
# because it usually follows the portfolio rather than leading.
_MUNICIPALITY = re.compile(r"^\s*Δήμ(ος|ου)\b")
_REGION = re.compile(r"^\s*Περιφερει|^\s*Περιφέρεια\b")
_MINISTRY = re.compile(r"Υπουργεί")


class PublisherKind(StrEnum):
    """How a publishing body maps onto a place, as far as its name will support.

    A deliberate heuristic over ``org_title``, not a claim about jurisdiction. Anything without
    a clear marker is ``NATIONAL`` rather than guessed at — CLAUDE.md §5 forbids inferring
    Greek geography, and an agency's name genuinely does not carry a place.
    """

    MUNICIPALITY = "municipality"
    REGION = "region"
    MINISTRY = "ministry"
    NATIONAL = "national"


@dataclass(frozen=True)
class Publisher:
    """A publishing body and how much readable data it actually offers."""

    name: str
    kind: PublisherKind
    dataset_count: int  # tabular datasets only — see the module docstring


@dataclass(frozen=True)
class Theme:
    """A DCAT-AP theme, with a label a reader can act on."""

    code: str
    label: str
    dataset_count: int


@dataclass(frozen=True)
class DatasetSummary:
    """One browsable dataset, carrying the resource a question would be pinned to."""

    id: str
    title: str
    publisher: str
    last_updated: str | None
    formats: list[str]
    #: The resource ``Pipeline.answer(resource_id=...)`` would fetch. ``None`` should not
    #: happen for a listed dataset, since listing requires a CSV/JSON resource, but the type
    #: stays honest rather than asserting.
    resource_id: str | None


def classify_publisher(org_title: str | None) -> PublisherKind:
    """Bucket a publishing body by its name. Unknown means national, never a guess."""
    name = org_title or ""
    if _MUNICIPALITY.search(name):
        return PublisherKind.MUNICIPALITY
    if _REGION.search(name):
        return PublisherKind.REGION
    if _MINISTRY.search(name):
        return PublisherKind.MINISTRY
    return PublisherKind.NATIONAL


def list_publishers(conn: sqlite3.Connection) -> list[Publisher]:
    """Every publisher with at least one CSV/JSON dataset, busiest first.

    Counts are over *tabular* datasets only. Showing a publisher's full catalogue count and
    then listing a tenth of it would be exactly the kind of quiet inaccuracy this product
    exists to avoid.
    """
    rows = conn.execute(
        f"SELECT org_title, COUNT(*) FROM datasets "  # noqa: S608 - _TABULAR is a constant
        f"WHERE id IN ({_TABULAR}) AND org_title IS NOT NULL AND trim(org_title) != '' "
        f"GROUP BY org_title ORDER BY COUNT(*) DESC, org_title"
    ).fetchall()
    return [
        Publisher(name=str(name), kind=classify_publisher(str(name)), dataset_count=int(count))
        for name, count in rows
    ]


def list_themes(conn: sqlite3.Connection) -> list[Theme]:
    """Every DCAT theme present on a tabular dataset, commonest first.

    ``theme`` is a JSON array of DCAT-AP URIs; the code is the last path segment. Harvested
    text is third-party content, so a malformed row is skipped rather than allowed to take out
    the page.
    """
    counts: dict[str, int] = {}
    for (raw,) in conn.execute(
        f"SELECT theme FROM datasets WHERE id IN ({_TABULAR}) AND theme IS NOT NULL"  # noqa: S608
    ):
        try:
            uris = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(uris, list):
            continue
        for uri in uris:
            code = str(uri).rstrip("/").rsplit("/", 1)[-1].strip()
            if code:
                counts[code] = counts.get(code, 0) + 1

    return [
        Theme(code=code, label=_THEME_LABELS.get(code, code), dataset_count=count)
        for code, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def list_datasets(
    conn: sqlite3.Connection,
    *,
    publisher: str | None = None,
    theme: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[DatasetSummary]:
    """Browsable datasets, newest first, filtered to those we can actually read."""
    where, params = _filters(publisher, theme)
    rows = conn.execute(
        f"SELECT id, title, org_title, last_updated FROM datasets "  # noqa: S608
        f"WHERE id IN ({_TABULAR}){where} "
        f"ORDER BY last_updated DESC, id LIMIT ? OFFSET ?",
        (*params, max(0, limit), max(0, offset)),
    ).fetchall()

    summaries: list[DatasetSummary] = []
    for dataset_id, title, org_title, last_updated in rows:
        formats = [
            str(fmt).strip().upper()
            for (fmt,) in conn.execute(
                "SELECT DISTINCT format FROM resources WHERE dataset_id = ? "
                "AND format IS NOT NULL ORDER BY format",
                (dataset_id,),
            )
            if str(fmt).strip()
        ]
        # Reuse planning's rule rather than inventing a second one: the question this hands off
        # to is fetched through exactly this resource, so the two must not disagree.
        chosen = select_resource(conn, str(dataset_id))
        summaries.append(
            DatasetSummary(
                id=str(dataset_id),
                title=str(title or dataset_id),
                publisher=str(org_title or ""),
                last_updated=last_updated,
                formats=formats,
                resource_id=chosen.id if chosen else None,
            )
        )
    return summaries


def count_datasets(
    conn: sqlite3.Connection, *, publisher: str | None = None, theme: str | None = None
) -> int:
    """How many datasets a listing with the same filters would return."""
    where, params = _filters(publisher, theme)
    row = conn.execute(
        f"SELECT COUNT(*) FROM datasets WHERE id IN ({_TABULAR}){where}",  # noqa: S608
        params,
    ).fetchone()
    return int(row[0]) if row else 0


def _filters(publisher: str | None, theme: str | None) -> tuple[str, tuple[str, ...]]:
    """Build the shared WHERE fragment. Every value is bound, never interpolated.

    ``theme`` reaches a LIKE clause and arrives from a URL path, so it is bound as a parameter
    and its wildcards are neutralised by matching the full DCAT URI rather than a bare code.
    """
    clauses: list[str] = []
    params: list[str] = []
    if publisher:
        clauses.append("org_title = ?")
        params.append(publisher)
    if theme:
        # Match the URI's final segment exactly: '.../data-theme/ENVI', optionally quoted by
        # the surrounding JSON. A smuggled fragment simply matches nothing.
        clauses.append("theme LIKE ?")
        params.append(f'%/{theme}"%')
    return (" AND " + " AND ".join(clauses) if clauses else "", tuple(params))
