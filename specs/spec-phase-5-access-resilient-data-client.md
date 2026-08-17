# Plan: Phase 5 — Access (resilient data client + cache + schema sniffing)

- **Task type:** feature · **Complexity:** complex
- **Roadmap:** `CLAUDE.md §8`, Phase 5
- **Upstream truth:** `docs/api_findings.md §3` — **plus the measured corrections in "Fetch
  surface" below, which supersede its implicit model.**
- **Revision:** v2, after a four-judge panel review. See "Panel review outcomes".

## Task Description

Phase 4 ends with a typed `QueryPlan` naming one resource: `resource_id`, `resource_url`,
`resource_format` (CSV/JSON), `access_path`, and validated intent `params`. Phase 5 turns
that into actual **rows and columns** — decoded, parsed, typed, provenance-carrying — from
an upstream with no SLA, where **three quarters of the fetchable resources are not even
hosted by the portal**.

## Objective

`fetch_resource()` returns a typed `TableData`, or a typed failure, such that **no code path
can present incomplete or misidentified data as complete and correct**. Bounded in memory,
bytes, rows, and wall-clock; cached without going permanently stale; parsed deterministically
offline-testable; and incapable of leaking the upstream's signed-URL credential.

## Fetch surface (measured 2026-07-29 from `data/catalog.sqlite`)

The v1 spec assumed `data.gov.gr → 302 → Azure Blob`. Measured over the **6,154 active
CSV/JSON resources** Phase 5 can actually be asked to fetch:

| Property | Value | Consequence |
|---|---|---|
| Host **not** `data.gov.gr` | **4,671 (75%)** across ~51 hosts | The Azure-Blob model covers a minority |
| Top off-portal hosts | `gis.crete.gov.gr` (624), `gisservices.chania.gr` (407), `naxos.getmap.gr` (303) | Small municipal GeoServers, not a CDN |
| Plain `http://` | 109 | MITM-able data cited as official |
| No file extension in path | 4,333 (70%) | Live query APIs (WFS `GetFeature`), not files |
| `last_modified` NULL | 4,580 (74%) | Cache key degrades to `metadata_modified` |
| Both timestamps NULL | **0** | v1's `"ttl"` cache branch was **dead code** |
| `size` NULL or ≤ 1 | 4,822 (78%) | Declared size is unusable for pre-flight |
| Largest declared size | **182,278,591 B (~174 MiB)** | v1's "largest is ~9.4 MB" was wrong |
| `datastore_active` | 412 | DataStore is **4.7% of tabular** (1,029/21,681), not 1% |

**Design consequence:** treat every fetch as *untrusted third-party content over an
untrusted URL*, not as "our portal's CSV".

## Problem Statement

6/26 golden questions yield a `MATCHED` plan and nothing fetches data, so Phase 6 cannot be
grounded. The hard part is not HTTP; it is that **each layer can silently produce plausible
wrong data**: an HTML error page parses as a table, a byte-capped file yields a fabricated
final row, a cache entry never expires, an unsorted DataStore page skips rows. Every one of
those ends as a confident figure in a cited answer — the exact failure Principle #1 forbids.

## Solution Approach

Two rules drive the design:

1. **Every boundary must be able to say "this is not what I asked for."** v1's boundaries
   could only succeed or crash. Each layer now has an explicit rejection path:
   scheme/host → parse-shape → magic-bytes → decode-plausibility → table-sanity →
   completeness. Anything unrecognised becomes a typed failure, never a table.
2. **Completeness is a first-class, non-defaultable fact.** `TableData.complete` plus
   `incomplete_reason` replaces v1's single `truncated` flag, which five separate paths
   could leave `False` over partial data.

Established repo patterns are kept: Protocol + fake at the network edge (as
`llm.py:LLMClient`, `retrieval/rerank.py:Scorer`), pure transforms isolated from I/O (as
`ingest/normalize.py`), committed SQL schema + caller-owned transactions (as `ingest/db.py`).

```
QueryPlan ─> data_client.fetch_resource()      orchestration, deadline, retries, logging
               ├─ guard.check_url()            scheme/host/IP policy   (pure)
               ├─ cache.get/put                SQLite, bounded, TTL-ceilinged
               ├─ Transport                    the ONLY I/O; returns no resolved URL
               ├─ detect.identify()            magic bytes / content-type  (pure)
               └─ sniff.*                      decode → parse → infer      (pure)
                                                     └─> TableData (complete | typed failure)
```

## Relevant Files

- `docs/api_findings.md` §3–§5 — endpoints, pagination, redirect, encoding quirks. **§4b's
  "deep offset pagination requires an explicit `sort`" applies to DataStore paging too.**
- `src/pythia/planning/models.py` — `QueryPlan`, `QueryParams` (now consumed, see task 7).
- `src/pythia/planning/select.py` — `access_path()`; `_SUPPORTED_FORMATS` to import, not retype.
- `src/pythia/ingest/models.py` — `ResourceRow`; note `url` is `str | None`.
- `src/pythia/ingest/db.py` — `connect()` is generic and reusable; only `init_db` is schema-bound.
- `src/pythia/llm.py` — retry idiom. **Note it deliberately does *not* retry `ReadTimeout`,
  and it does `raise LLMError(str(exc))` — which we must NOT copy (see task 4).**
- `src/pythia/logging_setup.py` — `configure_logging()` sets the **root** logger to INFO,
  which is why httpx's own request logging leaks (task 3).
- `src/pythia/ingest/harvest.py` — live client construction, `User-Agent`, `ssl_context`.
- `config.py`, `Makefile`, `docs/adr/`.

### New Files
`src/pythia/access/{__init__,models,guard,transport,detect,sniff,cache,catalog,data_client}.py`,
`src/pythia/access/cache_schema.sql`, `docs/adr/0006-access-layer-contract.md`,
`tests/test_access_{guard,detect,sniff,cache,client}.py`

## Implementation Phases

**Phase 1 — Foundation:** `models.py`, `guard.py`, `detect.py`, `sniff.py` + tests. All pure,
no network, no DB. This is where the correctness risk lives, so it lands first and cheapest.
**Phase 2 — Core:** `transport.py`, `cache.py`, `catalog.py`, `data_client.py`.
**Phase 3 — Integration:** `fetch_for_plan`, ADR-0006, `make fetch`, live validation, docs.

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom.

### 1. Config (`config.py`)
- `cache_db_path = "data/cache.sqlite"`; `access_read_timeout_s = 30.0` (per-chunk, not total);
  **`access_deadline_s = 90.0`** (total wall-clock per `fetch_resource`, incl. redirects,
  pages and retries); `access_connect_timeout_s = 8.0`; `access_max_rows = 50_000`;
  `access_max_bytes = 25_000_000`; `access_datastore_page_limit = 32_000`;
  `access_retry_attempts = 4`; `access_cache_ttl_s = 2_592_000` (30-day **ceiling on every
  row**); `access_cache_max_bytes = 2_000_000_000`; `access_min_throughput_bps = 10_000`;
  `access_host_min_interval_s = 1.0`; `access_max_redirects = 3`;
  `access_allow_http = False`; `access_allow_off_portal = True`.
- Document *why* on each, per existing style.

### 2. Typed contract (`access/models.py`)
- `Column(name, type)`, `type ∈ {integer, number, boolean, date, timestamp, text}`.
- `TableData` (frozen): `resource_id`, `dataset_id`, `dataset_title`, **`publisher`**
  (`datasets.org_title` — Principle #2 requires it in every answer and nothing else in the
  pipeline carries it), `last_updated`, `columns`, `rows: list[dict[str, str | None]]`,
  `row_count`, `upstream_total: int | None` (DataStore `total`, so "5,000 of 812,000" is
  sayable), **`complete: bool`**, **`incomplete_reason: str | None`**
  (`row_cap|byte_cap|page_stop|upstream_cap`), `access_path`, `source_url` (**always the
  CKAN resource URL — never a resolved/redirected URL**), `off_portal: bool`,
  `transport_scheme`, `encoding`, `delimiter`, `bytes_read`, `fetched_at`, `from_cache`.
- Errors: `AccessError` → `UnsupportedResourceError` (format/shape/scheme we will not
  handle), `MalformedPayloadError` (bytes are not the declared thing), `ResourceUnavailableError`
  (network/status/deadline), `NoMatchError` (plan is not `MATCHED` — distinct from
  "bad format", which v1 conflated).
- **`complete` has no default.** Every construction site must state it.

### 3. Credential containment (`logging_setup.py` + `access/transport.py`)
**Verified 2026-07-29:** httpx emits `logger.info("HTTP Request: GET <full-url> …")` on the
`httpx` logger, and `configure_logging()` sets root to INFO — the SAS `sig=` lands in our
JSON logs unaided. `raise_for_status()` also embeds the URL in its message.
- In `configure_logging()`: `logging.getLogger("httpx").setLevel(logging.WARNING)` and
  `logging.getLogger("httpcore").setLevel(logging.WARNING)`.
- Add a `RedactingFilter` on the root handler stripping `[?&](sig|se|sp|sv|st|skoid|sktid|fdl|token|key)=[^&\s'\"]*`
  from `msg` and `exc` (belt-and-braces: a future dependency will log a URL again).
- **Never** `raise_for_status()` on a fetched response; branch on `status_code`.
- **Never** put `str(httpx_exc)` into an `AccessError`. Map to a fixed message +
  `status_code`. This is an explicit divergence from `llm.py:107`.
- Transport returns **no resolved URL** — only `redirected: bool` and `final_host: str`.

### 4. URL policy (`access/guard.py`, pure)
- `check_url(url: str | None) -> ParsedTarget` raising `UnsupportedResourceError` for: null/
  empty (6 CSV/JSON rows), non-`http(s)` scheme (2 `ftp://`), and `http://` when
  `access_allow_http` is false (try an `https` upgrade first, then reject).
- `check_hop(host: str) -> None` rejecting loopback, RFC1918, link-local `169.254/16`,
  CGNAT `100.64/10`, ULA, `::1`. **Ollama listens on `localhost:11434` on this machine**, so
  a publisher-controlled URL is a live SSRF primitive today, and `169.254.169.254` matters
  if this is ever hosted. Document the DNS-rebinding residual risk explicitly.
- **Never** `verify=False` — a municipal host with an expired cert must become
  `ResourceUnavailableError`, not a silent downgrade.

### 5. Content identification (`access/detect.py`, pure)
- `identify(head: bytes, content_type: str | None) -> str` returning `csv|json|html|zip|pdf|ole|xml|binary`
  from magic bytes (`PK\x03\x04`, `%PDF`, `\xd0\xcf\x11\xe0`, leading `<!DOCTYPE`/`<html`/`<?xml`,
  leading `{`/`[`) plus `Content-Type` as a **negative** signal only (blobs send
  `application/octet-stream`).
- Anything not matching the declared `resource_format` → `MalformedPayloadError` naming what
  was found. This is the gate that stops an HTML 404 page becoming a one-column table —
  measured as a real case (`opendata.attica.gov.gr/...?fdl=<base64>` declared CSV, serves HTML).

### 6. Pure decode/parse/infer (`access/sniff.py`)
- `decode_bytes(raw) -> (text, encoding)`: `utf-8-sig` → `charset_normalizer` **restricted to
  `utf-8, cp1253, iso-8859-7`** (Greek codecs; the unrestricted call cannot fail — it returns
  a best guess, so v1's "never mojibake" guarantee did not hold). `.best()` returning `None`,
  or a decode with implausibly few Greek codepoints for a Greek-tagged dataset →
  `MalformedPayloadError`. Never `errors="replace"`.
- **On byte truncation, drop everything after the last `\n` before decoding.** Otherwise a
  mid-codepoint cut forces the Latin-1 fallback (whole-file mojibake) and a mid-line cut
  yields a fabricated final row — e.g. `"1234"` cut to `"12"` parses as a valid integer.
- `sniff_dialect(sample) -> csv.Dialect` — full dialect (delimiter from `,;\t|`, `quotechar`,
  `doublequote`), sampling **whole lines** so the sample never ends inside a quoted field.
  `csv.Sniffer` signals failure by raising `_csv.Error`; on that, fall back to `,` **and mark
  the table for the sanity gate** (v1's "default to `;` on ambiguity" was an unsupported claim
  and would turn a single-column comma file into one joined column). Handle the Excel `sep=;`
  preamble line. Use `csv.Sniffer().has_header()`; a headerless file gets synthetic
  `col_1..col_n` rather than eating its first data row.
- `parse_csv(text, dialect, max_rows)` — feed `csv.reader` an iterator that splits **only on
  `\r\n|\r|\n`** (never `str.splitlines()`, which also splits `\v`, `\f`, `\x85`, ` `).
  Pad short rows with `None`; drop and count over-long rows. **Duplicate/empty headers are
  renamed `name`, `name_2`, `col_3`** so `columns` and row keys stay 1:1 — v1's dict rows
  silently collapsed them. Catch `_csv.Error` (NUL bytes, field-size limit) →
  `MalformedPayloadError`; do not raise the field-size limit.
- `parse_json_records` — list-of-flat-objects, or an object with exactly one
  list-of-objects value; reject nested/GeoJSON.
- `infer_columns(header, rows, sample=200)` — order boolean → integer → number → date →
  timestamp → text; a type only if **every** non-empty sampled value parses. **Zero non-empty
  values ⇒ `text`** (v1's rule made an all-empty column vacuously `boolean`). Enumerate
  accepted booleans; `0`/`1` are `integer`, not boolean. Decimal-comma values stay `text` —
  deliberately, no silent coercion.
- `sanity_check(columns, rows)` → `MalformedPayloadError` on zero data rows from a non-empty
  body, header cells containing `<`/`>`, or a single column when the body has many delimiters.

### 7. Consume `QueryParams` (`data_client` + docs)
Phase 4 validates `date_from/date_to/region/aggregation/group_by/limit` and clamps `limit` to
`planning_limit_max = 32000` with the comment *"DataStore hard cap; clamp any extracted limit
to it"* — i.e. it clamps **for Phase 5**. v1 ignored all of it.
- `fetch_resource(..., params: QueryParams | None = None)`.
- **`params.limit`** bounds the row budget: `effective_max_rows = min(access_max_rows, params.limit or ∞)`.
- **Date/region/aggregation are NOT applied here** and that is deliberate: `datastore_search`
  supports only equality `filters`, not ranges, and the download path has no server-side
  filtering at all. Applying them for 4.7% of resources and not the rest would make Phase 6's
  input silently path-dependent. Record them on `TableData.requested_params` so Phase 6
  filters uniformly and can say so. **Write this reasoning into ADR-0006** — otherwise the
  next reader re-litigates it.

### 8. Transport (`access/transport.py`)
- Protocol: `get_json(url, params, max_bytes, deadline) -> dict` and
  `get_bytes(url, max_bytes, deadline) -> RawResponse(body, complete, redirected, final_host, status, content_type)`.
- **`follow_redirects=False`**; follow manually, `≤ access_max_redirects`, calling
  `guard.check_hop()` on each. Per-hop validation is architecturally impossible with
  `follow_redirects=True`.
- **Stream both paths**: `with client.stream(...) as r: for chunk in r.iter_bytes(): …`,
  breaking at `max_bytes`. **Forbid `.content`/`.read()`/`.json()` on unbounded responses** —
  httpx's `GZipDecoder` has no `max_length`, so a 1 MB gzip body expands to ~1 GB before any
  cap applies. `iter_bytes` counts decoded bytes (the right meter). Send
  `Accept-Encoding: identity` on the file path; these payloads are already compressed.
- **`get_json` takes `max_bytes` too** — v1 left Path A completely uncapped. A JSON page cannot
  be honestly truncated, so hitting the cap is `ResourceUnavailableError`.
- Enforce `access_deadline_s` (monotonic) inside the loop and across hops/pages/retries, plus
  the `access_min_throughput_bps` floor — httpx's read timeout is **per-chunk**, so a server
  dribbling 1 byte per 29 s never trips it and v1's "bounded wall-clock" was false.
- Retries: `ConnectError`, `ReadTimeout`, `RemoteProtocolError`, 5xx, and 429 (at most **one**
  retry for 429). **Never retry other 4xx.** Jittered backoff. Clamp `Retry-After` to
  `min(30 s, remaining deadline)` and accept both delta-seconds and HTTP-date. Retrying a
  partially streamed body restarts it — use `Range`/206 (confirmed supported) to resume, or
  state why not.
- Per-host token bucket (`access_host_min_interval_s`, concurrency 1). 76% of traffic hits
  small municipal GeoServers, many via live WFS `GetFeature` queries; the portal is documented
  as blocking crawlers. `User-Agent` with a contact URL, following the harvest precedent.
- `FakeTransport` with a call log, canned responses, and injectable failures.
- `use_system_trust_store()` is called from **entrypoints**, never at import (it mutates `ssl`
  process-wide).

### 9. Catalog lookup (`access/catalog.py`)
- **`get_resource(conn, resource_id) -> ResourceRow | None`** — does not exist anywhere in the
  repo today (the only `FROM resources` query is `select.py:22`, keyed on *dataset*), yet both
  `fetch_for_plan` and `make fetch` need it.
- `get_dataset_provenance(conn, dataset_id) -> (title, publisher, last_updated)` from
  `datasets.org_title`/`title`/`last_updated`.

### 10. Cache (`access/cache.py` + `cache_schema.sql`)
- Key: **`(resource_id, key_field, key_value)`** where `key_field ∈ {last_modified,
  metadata_modified}` — v1 collapsed these, so a `last_modified` that disappears resurrects an
  older body. The `"ttl"` branch is deleted: **0 resources have both timestamps NULL**, so it
  was unreachable and `purge_expired()` could never fire.
- Columns additionally: `access_path`, `resource_format`, `complete`, `body` (zlib),
  `bytes_read`, `cached_at`, `last_accessed`, `parser_version`.
- **Never cache an incomplete body**, and treat a cached row as a miss if `parser_version`
  has moved. v1 would cache a 25 MB slice of a 174 MB file under a never-expiring key and
  serve it as complete forever.
- **Store `access_path`/`format` and reconstruct from them**, not from the live
  `datastore_active` flag: after a DataStore 404 falls back to download, `access_path(resource)`
  still says `datastore`, so v1's cache hit would feed CSV bytes to the JSON parser forever.
- **`access_cache_ttl_s` is a ceiling on every row**, because `metadata_modified` is
  catalog-level and does not move when a file is replaced in place — without this, 74% of
  resources are cached permanently while the footer advertises freshness (Principle #2).
- LRU eviction to `access_cache_max_bytes`; delete superseded rows for the same
  `resource_id` on `put`. One full pass is ~6 GB raw.
- `PRAGMA journal_mode=WAL`, `busy_timeout`; **any cache read/write failure degrades to a
  refetch, never an exception**. Reuse `db.connect()`; add a `make cache-purge` target.

### 11. Orchestrator (`access/data_client.py`)
- `fetch_resource(resource, *, transport, cache_conn, settings, params=None, provenance=None) -> TableData`:
  guard → cache → path branch → identify → decode → parse → sanity → assemble.
- **DataStore paging:** issue page 0 unconditionally; `limit = min(page_limit, remaining)`;
  **pass an explicit `sort=_id`** (`api_findings.md §4b`: without a sort, deep offset paging
  "can drift and skip/duplicate rows" — the harvester already learned this); advance
  `offset += len(records)`; stop on an empty page, on `offset >= total`, or at the row cap
  (`complete=False`, `incomplete_reason="row_cap"`). Treat a missing `total` as
  `ResourceUnavailableError`; record `upstream_total`. Handle CKAN's `{"success": false}` at
  HTTP 200. Map Postgres `fields[].type` (`int4/int8/numeric/float8/bool/timestamp/date/json/_text`)
  to `Column.type` via an explicit table, defaulting unknown → `text`. Drop `_id` from **both**
  columns and rows. Stringify record values with a stated rule so DataStore and CSV agree.
- **On DataStore 404, fall back to download** — label this an assumption, not an
  `api_findings.md` finding — and record the real path taken.
- One `log_event` per fetch: `resource_id`, `access_path`, `from_cache`, `off_portal`,
  `bytes_read`, `row_count`, `complete`, `incomplete_reason`, `latency_ms`. Never a URL.
- `fetch_for_plan(plan, ...)` → `NoMatchError` unless `MATCHED`; loads via
  `catalog.get_resource(plan.resource_id)`; if the row is missing or its id ≠
  `plan.resource_id`, raise rather than substitute.

### 12. ADR-0006, tests, `make fetch`, docs
- **`docs/adr/0006-access-layer-contract.md`** — v1 had no ADR at all, breaking the discipline
  of Phases 3–4. Record: uncoerced `str` rows + separate `Column.type`; byte-level cache with
  a TTL ceiling; `complete`/`incomplete_reason` as the honesty contract; params deferral
  (task 7); off-portal URL policy.
- `make fetch RESOURCE_ID=…` (Makefile vars, `.PHONY`, and `CLAUDE.md §7` in sync).
- Update `docs/api_findings.md` with the measured fetch surface, that **declared `size` is
  unusable** (78% are NULL/≤1), and the observed SAS expiry window.

## Testing Strategy

Everything below runs offline.

- **guard:** null/empty/`ftp`/`http` URLs; loopback, `127.0.0.1`, `169.254.169.254`,
  `10.x`, `100.64.x`, `::1` rejected — including **as a redirect hop**, not just as the initial URL.
- **detect:** HTML, ZIP, PDF, OLE and XML bodies all declared `format=csv` → `MalformedPayloadError`.
- **sniff:** UTF-8 ±BOM; cp1253 and iso-8859-7 Greek; a body decodable only as Latin-1 rejected
  rather than mojibake'd; `;` vs `,` including a comma-decimal Greek file; a sniff sample that
  would land inside a quoted field; embedded newlines/quotes; NUL bytes; `\v`/` ` inside a
  field not splitting the row; duplicate/empty headers renamed 1:1; missing header row;
  `sep=;` preamble; ragged rows; all-empty column ⇒ `text`; `0/1` ⇒ integer;
  **byte-truncated input dropping the partial final line** (assert the fabricated row is absent).
- **cache:** hit/miss; `key_field` change; TTL ceiling expiring a `metadata_modified` row;
  incomplete body never cached; `parser_version` bump invalidating; LRU eviction; a corrupt
  BLOB and a deleted DB both degrading to a miss, not an exception.
- **client (FakeTransport):** DataStore multi-page with `sort` asserted in params; `total`
  shrinking mid-page; empty page terminating; `limit=0` not looping; `{"success": false}`;
  **DataStore 404 → download fallback, then a cache hit that parses as CSV** (the v1
  poisoning case); redirect chain > `max_redirects`; redirect to loopback rejected; 5xx
  retried then succeeding; 404 not retried; 429 retried once with a clamped `Retry-After`;
  deadline exceeded; throughput floor tripped; row cap and byte cap each setting
  `complete=False` with the right `incomplete_reason`; cache hit performing **zero**
  transport calls.
- **Credential containment (regression for a verified leak):** capture the **entire logging
  tree** (root handler, `httpx` logger, formatted exceptions) plus the serialized `TableData`
  and the cache row, and assert a `sig=`-bearing redirect target appears in **none** of them.
- **Not covered by `make eval`** — Phase 5 touches neither `retrieval/` nor `planning/`.
  Do not claim eval coverage.

## Acceptance Criteria

- [ ] No path returns a `TableData` with `complete=True` over partial, truncated, or
      page-stopped data; `incomplete_reason` is set whenever `complete` is false.
- [ ] A byte-truncated CSV never yields a fabricated final row and never triggers a
      non-Greek whole-file decode.
- [ ] HTML/ZIP/PDF bodies declared CSV raise `MalformedPayloadError`, never parse as a table.
- [ ] A `sig=`-bearing URL appears in no log record, no exception message, no `TableData`,
      and no cache row — asserted against captured stderr, not by inspection.
- [ ] Redirects are followed manually, ≤ 3 hops, each hop host-validated; loopback and
      link-local are rejected at every hop; `verify=False` appears nowhere.
- [ ] `fetch_resource` respects `access_deadline_s` end-to-end including retries and paging.
- [ ] DataStore paging sends an explicit `sort`, terminates on empty page / `offset ≥ total` /
      row cap, and never loops on `limit=0`.
- [ ] Cache: incomplete bodies never stored; TTL ceiling applies to every row; a
      DataStore→download fallback re-parses correctly from cache; growth bounded by
      `access_cache_max_bytes`.
- [ ] `params.limit` bounds the row budget; deferred params are recorded on `TableData`.
- [ ] `TableData` carries `publisher`, `dataset_title` and `last_updated` — Phase 6 can build
      the mandatory footer without re-querying.
- [ ] `DATA_GOV_GR_TOKEN` unread; `datastore_search_sql` and `query/{dataset}` absent.
- [ ] `make check` green; ADR-0006 written; `docs/api_findings.md` updated.

## Validation Commands

- `uv run ruff check .` · `uv run mypy` · `uv run pytest -q`
- `uv run pytest -q tests/test_access_guard.py tests/test_access_detect.py tests/test_access_sniff.py tests/test_access_cache.py tests/test_access_client.py`
- PowerShell (this project's primary shell):
  `$env:HF_HUB_OFFLINE='1'; $env:TRANSFORMERS_OFFLINE='1'; uv run pytest -q` — proves the new
  tests need no network
- `uv run python -m pythia.access.data_client --resource-id <id>` — run twice; second must be
  `from_cache=True`. Exercise at least: one portal CSV, one **off-portal** GeoServer URL, one
  `datastore_active` resource, and one resource whose bytes are not the declared format.

## Notes

- **No new dependencies.** `httpx`, `tenacity`, `charset-normalizer` are present;
  `csv`/`json`/`zlib`/`sqlite3`/`ipaddress` are stdlib. No pandas — dependency weight, and it
  silently coerces exactly the types this design protects.
- **Memory reality:** `access_max_bytes` bounds the wire, but 50,000 `dict` rows cost roughly
  10–20× the raw CSV in Python objects. Store `columns` + `list[tuple[...]]` internally and
  expose dicts lazily, or the stated bound does not hold.
- **Deferred:** XLSX/PDF (stay `UnsupportedResourceError`), async/concurrent fetching,
  cross-dataset joins (forbidden — no stable geographic keys, `CLAUDE.md §5`).
- **Do not measure Phase 5 by end-to-end answer rate.** Half the correctly-retrieved golden
  datasets have no CSV/JSON resource at all; that is a Phase 3 / coverage limit.

## Panel review outcomes (v1 → v2)

Four independent judges reviewed v1: architecture/ADR consistency, logic/ambiguity, edge
cases, and security/performance. Two independently queried the live catalog; one verified
httpx's behaviour in the pinned `.venv`. Consolidated:

**Accepted — 9 blockers, all fixed above:** credential leak via httpx's own INFO logging and
`raise_for_status` (**verified empirically**, and v1's own `source_url` field persisted it);
75%-off-portal fetch surface invalidating the Azure-Blob threat model (SSRF, TLS, politeness);
truncation fabricating rows and forcing mojibake; cache permanently stale, poisonable by the
DataStore fallback, and unbounded (v1's `"ttl"` branch was dead code — **0 rows**); no content
validation, so an HTML 404 parsed as a table; DataStore paging without `sort` (a bug this repo
had already documented) and with non-terminating conditions; `QueryParams` silently discarded;
no wall-clock deadline (httpx's read timeout is per-chunk); Path A uncapped + gzip-bomb.

**Accepted — majors:** missing ADR; `publisher` absent from the provenance chain; `get_resource`
never existed; `infer_columns` made an all-empty column `boolean`; duplicate headers collapsed
dict rows; "retry 429 / never retry 4xx" was a literal contradiction; the `llm.py` retry idiom
was misattributed (it deliberately does *not* retry `ReadTimeout`); no rate limiting toward
small municipal hosts.

**Accepted — my factual errors:** DataStore is 4.7% of tabular resources, not ~1% (I mixed
denominators); largest resource is 174 MiB, not 9.4 MB; the `;`-delimiter default and the
"stale `datastore_active`" claim were attributed to `api_findings.md`, which says neither.

**Rejected:** two judges claimed `CLAUDE.md §8` still marks Phase 4 `[~]` with a pending eval
gate. It reads `[x]`; both were reading the session-start copy injected into their context.
Recorded so the correction is not re-applied.
