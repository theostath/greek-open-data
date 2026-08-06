"""Provenance rendering. Principle #2: every answer names its source, publisher and freshness.

Catalog metadata is genuinely incomplete for some datasets, so a missing publisher degrades
the *wording* rather than raising on the answer path — ``get_provenance`` legitimately returns
all-``None`` when a dataset row is absent, and turning that into a crash would fail loudly in
production for a cosmetic gap.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from config import Settings, get_settings

from pythia.access.models import TableData
from pythia.logging_setup import redact_secrets
from pythia.synthesis.models import Footer

_MISSING = {
    "el": {
        "title": "χωρίς τίτλο στον κατάλογο",
        "publisher": "ο φορέας δεν καταγράφεται στον κατάλογο",
        "updated": "άγνωστη ημερομηνία ενημέρωσης",
    },
    "en": {
        "title": "untitled in the catalogue",
        "publisher": "publisher not recorded in the catalogue",
        "updated": "update date unknown",
    },
}


def build(
    table: TableData, *, dataset_name: str | None = None, language: str = "el",
    observed_range: tuple[str, str] | None = None, today: date | None = None,
    settings: Settings | None = None,
) -> Footer:
    """Render the provenance footer for a fetched table."""
    cfg = settings or get_settings()
    missing = _MISSING.get(language, _MISSING["en"])
    slug = dataset_name or table.dataset_id
    return Footer(
        dataset_title=(table.dataset_title or "").strip() or missing["title"],
        publisher=(table.publisher or "").strip() or missing["publisher"],
        last_updated=(table.last_updated or "").strip() or missing["updated"],
        # source_url alone is a weak citation: for 75% of resources it points at a municipal
        # server rather than the dataset page the answer is really citing.
        dataset_url=f"{cfg.data_gov_gr_base_url.rstrip('/')}/dataset/{slug}",
        # A publisher-supplied catalog URL may carry its own ?token=; redact what is shown,
        # not merely what is logged.
        source_url=redact_secrets(table.source_url),
        fetched_at=table.fetched_at,
        row_coverage=row_coverage(table, language),
        staleness=staleness(table.last_updated, language, cfg=cfg, today=today),
        complete=table.complete,
        resource_id=table.resource_id,
        resource_format=(table.delimiter and "CSV") or table.access_path,
        observed_range=observed_range,
    )


def row_coverage(table: TableData, language: str) -> str:
    """Describe how much of the resource the answer rests on.

    ``upstream_total`` is never set on a download, and downloads are the common case, so the
    "size unknown" wording is designed for rather than treated as an edge case.
    """
    rows = format_number(table.row_count, language)
    if table.complete:
        return f"όλες οι {rows} γραμμές" if language == "el" else f"all {rows} rows"
    if table.upstream_total:
        total = format_number(table.upstream_total, language)
        share = round(100 * table.row_count / table.upstream_total)
        return (
            f"{rows} από {total} γραμμές ({share}%)" if language == "el"
            else f"{rows} of {total} rows ({share}%)"
        )
    return (
        f"{rows} γραμμές· το πλήρες μέγεθος δεν είναι γνωστό" if language == "el"
        else f"{rows} rows; the full size is not known"
    )


def staleness(
    last_updated: str | None, language: str, *, cfg: Settings, today: date | None = None
) -> str:
    """Bucket how old the dataset is. Buckets do not overlap."""
    if not last_updated:
        return "άγνωστη ενημέρωση" if language == "el" else "update date unknown"
    try:
        moment = datetime.fromisoformat(last_updated.replace("Z", "+00:00")).date()
    except ValueError:
        return "άγνωστη ενημέρωση" if language == "el" else "update date unknown"
    now = today or datetime.now(UTC).date()
    days = (now - moment).days
    fresh, recent, ageing = cfg.synthesis_stale_days
    if days < fresh:
        return ("ενημερώθηκε τον τελευταίο μήνα" if language == "el"
                else "updated within the last month")
    if days < recent:
        return ("ενημερώθηκε μέσα στον τελευταίο χρόνο" if language == "el"
                else "updated within the last year")
    if days < ageing:
        return (
            f"δεν έχει ενημερωθεί εδώ και {days // 365} χρόνια" if language == "el"
            else f"not updated for {days // 365} years"
        )
    return (
        f"δεν έχει ενημερωθεί εδώ και {days // 365} χρόνια — πιθανώς ανενεργό"
        if language == "el"
        else f"not updated for {days // 365} years — possibly abandoned"
    )


def format_number(value: Decimal | int | str, language: str) -> str:
    """Render a figure in the reader's locale.

    The same formatter renders template text and feeds the guard's normalisation, so the two
    cannot disagree about what "1.234,5" means.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, Decimal):
        value = value.normalize()
        text = f"{value:,f}" if value == value.to_integral_value() else f"{value:,f}"
        text = text.rstrip("0").rstrip(".") if "." in text else text
    else:
        text = f"{value:,}"
    if language != "el":
        return text
    return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
