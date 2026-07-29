# ADR 0006 — Access-layer data contract and fetch policy

## Status

Accepted · 2026-07-29 (Phase 5)

## Context

Phase 5 turns a `QueryPlan` into rows. The spec's first draft assumed the pattern
`docs/api_findings.md §3` documents — a `data.gov.gr` URL 302-redirecting to a signed Azure
Blob. Measuring the catalog showed that model covers a **minority** of the work. Of the
**6,154 active CSV/JSON resources**:

| Property | Value |
|---|---|
| Host not `data.gov.gr` | **4,671 (75%)**, ~51 hosts, mostly municipal GeoServers |
| Plain `http://` | 109 |
| No file extension (live query APIs) | 4,333 (70%) |
| `last_modified` NULL | 4,580 (74%) |
| Both freshness fields NULL | **0** |
| `size` NULL or ≤ 1 | 4,822 (78%) |
| Largest declared size | **182,278,591 B (~174 MiB)** |

So every fetch is untrusted third-party content over a publisher-supplied URL. Combined with
Principle #1 (grounded or silent), the risk is not that a fetch fails — it is that it
**succeeds and returns plausible wrong data**.

## Decision

**1. Rows are uncoerced text; `Column.type` carries the inference.**
`rows: list[dict[str, str | None]]`. Greek exports use `,` as a decimal separator, so eager
float parsing would corrupt figures. Phase 6 converts explicitly. Mapping `Column.type` to
Vega-Lite encodings is Phase 6's job — source types and presentation types stay separate.

**2. Completeness is explicit and cannot default.**
`TableData.complete: bool` has no default and `__post_init__` rejects any inconsistency with
`incomplete_reason ∈ {row_cap, byte_cap, page_stop, upstream_cap}`. A single `truncated`
flag was falsifiable by five different paths.

**3. Every boundary can reject.**
scheme/host policy (`guard`) → magic bytes and content type (`detect`) → restricted-codec
decode (`sniff.decode_bytes`) → parse → `sanity_check`. Anything unrecognised is a typed
`AccessError`, never a table. Concretely: an HTML 404 served with HTTP 200 is refused rather
than parsed into a one-column table of markup.

**4. Truncation trims to the last complete line before decoding.**
A mid-line byte cut turns `1234` into a valid-looking `12`; a mid-codepoint cut pushes the
decoder onto a single-byte fallback that mojibakes the whole file.

**5. Redirects are followed manually, ≤ 3 hops, each host validated.**
`follow_redirects=True` makes per-hop validation impossible. Loopback, RFC1918, link-local,
CGNAT and reserved addresses are refused — Ollama listens on `localhost:11434` on the dev
machine, so a publisher-controlled URL is otherwise an SSRF primitive.

**6. The resolved URL never leaves the transport.**
`TableData.source_url` and the cache are always the CKAN resource URL. The transport returns
`final_host`, never `final_url`.

**7. The cache stores raw bytes, keyed on `(resource_id, key_field, key_value)`, with a TTL
ceiling on every row.** Incomplete bodies are never stored; `access_path` and `format` are
stored rather than re-derived; `parser_version` invalidates on parser change; LRU eviction
bounds growth; any cache failure degrades to a refetch.

**8. `params.limit` is applied; date/region/aggregation are deferred and recorded.**
`datastore_search` supports only equality filters, and the download path supports none.
Applying ranges for 4.7% of resources and not the rest would make Phase 6's input silently
path-dependent. They are surfaced on `TableData.deferred_params` so Phase 6 filters uniformly.

**9. `TableData` carries `publisher`, `dataset_title` and `last_updated`.**
Principle #2 requires the publisher in every answer, and neither `Candidate` nor `QueryPlan`
carried `org_title`.

## Rationale

Alternatives considered and rejected:

- **pandas for parsing** — a large dependency for stdlib work, and it silently coerces
  exactly the types decision (1) protects.
- **Caching parsed rows instead of bytes** — parsing is cheap; re-fetching a rate-limited
  government portal is not. Caching bytes means a parser fix does not re-download the portal.
- **Trusting the catalog's `format` and `datastore_active`** — both are observably wrong
  (HTML served for declared CSV; DataStore 404s on flagged resources).
- **Relying on logging discipline for the SAS credential** — verified insufficient: httpx
  logs the full request URL at INFO on its own logger, and `configure_logging()` sets the
  root logger to INFO. Hence the redaction filter plus silencing `httpx`/`httpcore`.

## Consequences

- Phase 6 must convert types explicitly, apply `deferred_params`, and **refuse aggregates
  over a table with `complete=False`** — a total over a byte-capped file is a wrong figure
  presented as fact.
- More refusals than a trusting client: some genuinely awkward resources will return
  `MalformedPayloadError`. That is the intended trade under grounded-or-silent.
- The 30-day TTL ceiling means a slow trickle of re-fetches even for unchanged resources.
  Accepted: `metadata_modified` does not move when a file is replaced in place, so without
  it 74% of resources would be cached permanently behind a confident freshness footer.
- Per-host politeness (1 s) makes multi-resource work slower by design; 76% of traffic hits
  small municipal servers and the portal is documented as blocking crawlers.
- `access_max_bytes` bounds the wire, not resident memory: 50,000 dict rows cost roughly
  10–20× the raw CSV. Revisit with a columnar internal representation if it bites.
