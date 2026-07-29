"""Phase 5 orchestrator: resource -> typed ``TableData`` (ADR-0006).

Chains guard -> cache -> transport -> detect -> sniff, enforcing one wall-clock deadline
across redirects, DataStore pages and retries. Every exit is either a ``TableData`` whose
``complete`` flag is the truth, or a typed ``AccessError``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from config import Settings, get_settings

from pythia.access import cache as cache_mod
from pythia.access import catalog, detect, sniff
from pythia.access.guard import check_url
from pythia.access.models import (
    AccessError,
    Column,
    IncompleteReason,
    MalformedPayloadError,
    NoMatchError,
    ResourceUnavailableError,
    TableData,
    UnsupportedResourceError,
)
from pythia.access.transport import Deadline, Transport
from pythia.ingest.models import ResourceRow
from pythia.logging_setup import get_logger, log_event
from pythia.planning.models import PlanStatus, QueryParams, QueryPlan
from pythia.planning.select import access_path

LOGGER_NAME = "pythia.access.data_client"
_SUPPORTED_FORMATS = ("CSV", "JSON")
_DATASTORE_PATH = "/api/3/action/datastore_search"

# CKAN DataStore reports Postgres types; map explicitly rather than leaking them onward.
_PG_TYPES = {
    "int": "integer", "int2": "integer", "int4": "integer", "int8": "integer",
    "bigint": "integer", "smallint": "integer", "serial": "integer",
    "numeric": "number", "float4": "number", "float8": "number", "real": "number",
    "double precision": "number", "money": "number",
    "bool": "boolean", "boolean": "boolean",
    "date": "date",
    "timestamp": "timestamp", "timestamptz": "timestamp",
    "timestamp without time zone": "timestamp", "timestamp with time zone": "timestamp",
}


def fetch_resource(
    resource: ResourceRow,
    *,
    transport: Transport,
    cache_conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    params: QueryParams | None = None,
    provenance: catalog.Provenance | None = None,
) -> TableData:
    """Fetch one resource into a typed table, or raise a typed ``AccessError``."""
    cfg = settings or get_settings()
    logger = get_logger(LOGGER_NAME)
    started = perf_counter()
    deadline = Deadline(cfg.access_deadline_s)

    declared = (resource.format or "").strip().upper()
    if declared not in _SUPPORTED_FORMATS:
        raise UnsupportedResourceError(f"unsupported resource format: {declared or 'unknown'}")

    target = check_url(
        resource.url,
        allow_http=cfg.access_allow_http,
        allow_off_portal=cfg.access_allow_off_portal,
    )
    max_rows = _row_budget(cfg, params)

    cached = (
        cache_mod.get(cache_conn, resource, ttl_s=cfg.access_cache_ttl_s)
        if cache_conn is not None
        else None
    )
    if cached is not None:
        table = _build_from_bytes(
            cached.body, complete=True, resource=resource, target_url=target.url,
            access_path_used=cached.access_path, declared=cached.resource_format or declared,
            max_rows=max_rows, provenance=provenance, params=params,
            off_portal=target.off_portal, scheme=target.scheme, from_cache=True,
        )
        _log(logger, started, table)
        return table

    path = access_path(resource)
    body: bytes
    complete: bool
    if path == "datastore":
        try:
            table = _fetch_datastore(
                resource, transport=transport, cfg=cfg, deadline=deadline, max_rows=max_rows,
                target_url=target.url, provenance=provenance, params=params,
                off_portal=target.off_portal, scheme=target.scheme,
            )
            _log(logger, started, table)
            return table
        except _DataStoreAbsent:
            # The catalog's datastore_active flag can be stale (our assumption, not a
            # documented upstream finding); fall back rather than fail.
            log_event(logger, logging.INFO, "access.datastore_fallback",
                      resource_id=resource.id)
            path = "download"

    raw = transport.get_bytes(target.url, max_bytes=cfg.access_max_bytes, deadline=deadline)
    body, complete = raw.body, raw.complete
    detect.ensure_matches_declared(detect.identify(body[:4096], raw.content_type), declared)
    if not complete:
        body = sniff.trim_to_last_line(body)

    table = _build_from_bytes(
        body, complete=complete, resource=resource, target_url=target.url,
        access_path_used="download", declared=declared, max_rows=max_rows,
        provenance=provenance, params=params, off_portal=target.off_portal,
        scheme=target.scheme, from_cache=False, bytes_read=len(raw.body),
    )
    if cache_conn is not None:
        cache_mod.put(cache_conn, resource, body=body, access_path="download",
                      complete=complete, max_total_bytes=cfg.access_cache_max_bytes)
    _log(logger, started, table)
    return table


def fetch_for_plan(
    plan: QueryPlan,
    *,
    conn: sqlite3.Connection,
    transport: Transport,
    cache_conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
) -> TableData:
    """Fetch the resource a ``MATCHED`` plan selected, carrying provenance through."""
    if plan.status is not PlanStatus.MATCHED:
        raise NoMatchError(f"plan status is {plan.status.value}; nothing to fetch")
    if plan.resource_id is None:
        raise NoMatchError("matched plan carries no resource id")
    resource = catalog.get_resource(conn, plan.resource_id)
    if resource is None:
        raise UnsupportedResourceError(f"resource {plan.resource_id} is not in the catalog")
    prov = catalog.get_provenance(conn, resource.dataset_id)
    return fetch_resource(
        resource, transport=transport, cache_conn=cache_conn, settings=settings,
        params=plan.params, provenance=prov,
    )


def _row_budget(cfg: Settings, params: QueryParams | None) -> int:
    """Bound rows by config and by the limit Phase 4 already clamped for us."""
    if params is not None and params.limit:
        return min(cfg.access_max_rows, params.limit)
    return cfg.access_max_rows


class _DataStoreAbsent(Exception):
    """Internal: this resource is not actually in the DataStore."""


def _fetch_datastore(
    resource: ResourceRow, *, transport: Transport, cfg: Settings, deadline: Deadline,
    max_rows: int, target_url: str, provenance: catalog.Provenance | None,
    params: QueryParams | None, off_portal: bool, scheme: str,
) -> TableData:
    """Page ``datastore_search`` into a table, with a stable sort."""
    base = cfg.data_gov_gr_base_url.rstrip("/") + _DATASTORE_PATH
    records: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    offset = 0
    total: int | None = None
    reason: IncompleteReason | None = None

    while True:
        deadline.check()
        remaining = max_rows - len(records)
        if remaining <= 0:
            reason = IncompleteReason.ROW_CAP
            break
        payload = transport.get_json(
            base,
            {
                "resource_id": resource.id,
                "limit": min(cfg.access_datastore_page_limit, remaining),
                "offset": offset,
                # Deep offset paging drifts without a stable sort (api_findings.md 4b);
                # the harvester learned this the hard way.
                "sort": "_id asc",
            },
            max_bytes=cfg.access_max_bytes,
            deadline=deadline,
        )
        if payload.get("success") is False:
            raise _DataStoreAbsent
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ResourceUnavailableError("DataStore response had no result object")
        if total is None:
            total = result.get("total")
            if not isinstance(total, int):
                raise ResourceUnavailableError("DataStore response had no integer total")
            fields = [f for f in result.get("fields") or [] if f.get("id") != "_id"]
        page = result.get("records") or []
        if not page:
            break
        records.extend(page)
        offset += len(page)
        if offset >= (total or 0):
            break

    if reason is None and total is not None and len(records) < total:
        reason = IncompleteReason.PAGE_STOP

    header = [str(f.get("id")) for f in fields]
    columns = [
        Column(name=str(f.get("id")), type=_PG_TYPES.get(str(f.get("type", "")).lower(), "text"))
        for f in fields
    ]
    rows = [
        {name: sniff.scalar(record.get(name)) for name in header}
        for record in records
    ]
    return TableData(
        resource_id=resource.id, dataset_id=resource.dataset_id, columns=columns, rows=rows,
        row_count=len(rows), complete=reason is None, incomplete_reason=reason,
        # DataStore reports real field ids; there is no banner row in a JSON envelope.
        header_trusted=True,
        upstream_total=total, access_path="datastore", source_url=target_url,
        fetched_at=datetime.now(UTC).isoformat(), off_portal=off_portal,
        transport_scheme=scheme, deferred_params=_deferred(params),
        dataset_title=provenance.dataset_title if provenance else None,
        publisher=provenance.publisher if provenance else None,
        last_updated=provenance.last_updated if provenance else None,
    )


def _build_from_bytes(
    body: bytes, *, complete: bool, resource: ResourceRow, target_url: str,
    access_path_used: str, declared: str, max_rows: int,
    provenance: catalog.Provenance | None, params: QueryParams | None, off_portal: bool,
    scheme: str, from_cache: bool, bytes_read: int | None = None,
) -> TableData:
    """Decode, parse, infer and sanity-check a payload into a table."""
    kind = detect.identify(body[:4096], None)
    detect.ensure_matches_declared(kind, declared)
    text, encoding = sniff.decode_bytes(body)
    delimiter: str | None = None
    if kind == "json" or declared == "JSON":
        try:
            parsed = sniff.parse_json_records(json.loads(text), max_rows)
        except ValueError as exc:
            raise MalformedPayloadError(f"resource declared JSON but did not parse: {exc}") from exc
        confident = True
    else:
        delimiter, confident = sniff.sniff_dialect(text[:65536])
        parsed = sniff.parse_csv(text, delimiter, max_rows)

    columns = sniff.infer_columns(parsed.header, parsed.rows)
    sniff.sanity_check(columns, parsed.rows, confident=confident)

    reason: IncompleteReason | None = None
    if parsed.truncated:
        reason = IncompleteReason.ROW_CAP
    elif not complete:
        reason = IncompleteReason.BYTE_CAP
    return TableData(
        resource_id=resource.id, dataset_id=resource.dataset_id, columns=columns,
        rows=parsed.rows, row_count=len(parsed.rows), complete=reason is None,
        incomplete_reason=reason, header_trusted=parsed.header_trusted,
        access_path=access_path_used, source_url=target_url,
        fetched_at=datetime.now(UTC).isoformat(), off_portal=off_portal,
        transport_scheme=scheme, encoding=encoding, delimiter=delimiter,
        bytes_read=bytes_read if bytes_read is not None else len(body),
        from_cache=from_cache, deferred_params=_deferred(params),
        dataset_title=provenance.dataset_title if provenance else None,
        publisher=provenance.publisher if provenance else None,
        last_updated=provenance.last_updated if provenance else None,
    )


def _deferred(params: QueryParams | None) -> dict[str, str]:
    """Record intent params this layer deliberately did not apply (see ADR-0006)."""
    if params is None:
        return {}
    deferred = {
        "date_from": params.date_from, "date_to": params.date_to, "region": params.region,
        "aggregation": params.aggregation, "group_by": params.group_by,
    }
    return {key: value for key, value in deferred.items() if value}


def _log(logger: logging.Logger, started: float, table: TableData) -> None:
    """Emit the structured access trace. Never logs a URL."""
    log_event(
        logger, logging.INFO, "access.done",
        resource_id=table.resource_id, access_path=table.access_path,
        from_cache=table.from_cache, off_portal=table.off_portal,
        bytes_read=table.bytes_read, row_count=table.row_count, complete=table.complete,
        incomplete_reason=table.incomplete_reason.value if table.incomplete_reason else None,
        latency_ms=round((perf_counter() - started) * 1000, 1),
    )


def main(argv: list[str] | None = None) -> int:
    """Fetch one resource by id and print a summary (``make fetch RESOURCE_ID=…``)."""
    import argparse

    import httpx

    from pythia.access.cache import connect_cache, init_cache_db
    from pythia.access.transport import HttpxTransport
    from pythia.ingest.db import connect
    from pythia.logging_setup import configure_logging
    from pythia.net import use_system_trust_store

    parser = argparse.ArgumentParser(description="Fetch one catalog resource.")
    parser.add_argument("--resource-id", required=True)
    args = parser.parse_args(argv)

    configure_logging()
    use_system_trust_store()  # entrypoint only: it mutates ssl process-wide
    cfg = get_settings()
    conn = connect(cfg.catalog_db_path)
    resource = catalog.get_resource(conn, args.resource_id)
    if resource is None:
        print(f"no such resource: {args.resource_id}")
        return 1
    cache_conn = connect_cache(cfg.cache_db_path)
    init_cache_db(cache_conn)

    with httpx.Client(
        timeout=httpx.Timeout(cfg.access_read_timeout_s, connect=cfg.access_connect_timeout_s),
        follow_redirects=False,
    ) as client:
        transport = HttpxTransport(
            client, max_redirects=cfg.access_max_redirects,
            attempts=cfg.access_retry_attempts,
            min_throughput_bps=cfg.access_min_throughput_bps,
            host_min_interval_s=cfg.access_host_min_interval_s,
        )
        try:
            table = fetch_resource(
                resource, transport=transport, cache_conn=cache_conn, settings=cfg,
                provenance=catalog.get_provenance(conn, resource.dataset_id),
            )
        except AccessError as exc:
            print(f"{type(exc).__name__}: {exc}")
            return 1
    cache_conn.commit()

    print(f"resource   : {table.resource_id} ({table.access_path}, from_cache={table.from_cache})")
    print(f"publisher  : {table.publisher}")
    print(f"updated    : {table.last_updated}")
    print(f"complete   : {table.complete} ({table.incomplete_reason or 'whole resource'})")
    print(f"rows       : {table.row_count} of {table.upstream_total or 'unknown'}")
    print(f"encoding   : {table.encoding}  delimiter={table.delimiter!r}")
    print(f"columns    : {', '.join(f'{c.name}:{c.type}' for c in table.columns)}")
    for row in table.rows[:3]:
        print(f"  {row}")
    return 0


__all__ = ["AccessError", "fetch_for_plan", "fetch_resource", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
