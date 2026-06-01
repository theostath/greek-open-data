# data.gov.gr — API findings (Phase 1)

> **Curated source of truth** for the portal's API, per CLAUDE.md §5. Hand-maintained.
> Raw, rerunnable probe evidence lives in [`api_probe_raw.md`](api_probe_raw.md)
> (regenerate with `make probe`). When the API changes, update this file and say so.
>
> Last verified: **2026-06-01** (CKAN **2.11.3**). Verification: `make probe` + two
> live read-only discovery passes (catalog schema/pagination; data-access mechanism).

---

## 1. Headline conclusions

1. **The catalog is CKAN 2.11.3.** Metadata is served by the standard CKAN Action API at
   `https://data.gov.gr/api/3/action/…`. Reads are **anonymous** — no token required.
2. **The legacy data API is GONE.** `https://data.gov.gr/api/v1/query/{dataset}` returns the
   portal's HTML **404** page. The token + `query/{dataset}` pattern from CLAUDE.md §5 no
   longer exists post-relaunch. Do not build on it.
3. **There is no single "fetch the rows" endpoint.** Actual data lives one of two ways,
   **per resource**: CKAN **DataStore** (only ~1% of resources) or a **direct file download**
   that 302-redirects to a short-lived signed Azure Blob URL (the common case).
4. **Scale:** `package_search` reports **21,930 datasets** — materially more than the
   "~9,500" in CLAUDE.md §1. The mission number should be updated.
5. **Auth:** the `DATA_GOV_GR_TOKEN` was **not needed for anything** discovered so far
   (catalog, DataStore, downloads are all public). Keep it out of the read path.
6. **Encoding:** no mojibake observed at the source. Catalog JSON is `ensure_ascii=True`
   (Greek as `\uXXXX`, decodes to clean UTF-8); sampled CSV/DataStore payloads are valid
   UTF-8. CLAUDE.md's Windows-1253/Latin-1 warning stands as a *defensive* measure, not an
   observed fact.

---

## 2. Catalog API (CKAN Action API) — CONFIRMED

Base: `https://data.gov.gr/api/3/action/`. Envelope: `{ "help": …, "success": bool, "result": … }`.

| Endpoint | Use |
| --- | --- |
| `package_search?rows=&start=` | Paged dataset listing **with full package objects** — use for harvest. |
| `package_show?id=<slug-or-uuid>` | Full metadata for one dataset. |
| `package_list` | All object slugs (superset; includes non-datasets — see quirks). |
| `resource_show?id=<rid>` | One resource's metadata. |
| `datastore_search?resource_id=<rid>` | Tabular rows **iff** `datastore_active`. |
| `datastore_info?id=<rid>` | DataStore table info. |

### Pagination (measured)
- Total datasets: `package_search?rows=1` → `result.count = 21930`.
- **`package_search` max page size = 15000** (measured: `rows` 1000/2000/5000/15000 honored;
  16000/20000/50000 clamp to 15000 — i.e. `ckan.search.rows_max = 15000`).
- Offset paging via `start` confirmed (disjoint pages).
- **Harvest recipe:** read `count` from the first page, then loop
  `start += rows` with **`rows = 1000`** (~22 requests; larger pages approach the 15 s
  timeout). Prefer `package_search` over `package_list` + per-slug `package_show`
  (which would be ~22k requests).

---

## 3. Data access (the rows/values) — two paths, per resource

Resolve a dataset's resources via `package_show`, then branch on each resource:

### Path A — CKAN DataStore (preferred when available, but rare)
- `GET …/api/3/action/datastore_search?resource_id=<rid>&limit=&offset=`
- Only when `result.resources[i].datastore_active == true`. On a non-DataStore resource it
  returns **404**.
- Response `result`: `total` (int), `limit` (echoed), `fields` (`[{id,type}]`, typed columns
  incl. synthetic `_id`), `records` (row dicts), `_links.next` (carries next `offset`).
- **Pagination:** `limit`/`offset`; **max `limit` = 32000** (larger is silently clamped);
  page until `offset >= total`; offset past end → empty `records`, no error.
- **`datastore_search_sql` is DISABLED portal-wide** (`400 "Action name not known"`). Do not use.
- Coverage is sparse: in a ~150-dataset / 841-resource scan, only **8 resources (~1%)** had
  `datastore_active` — all from the Piraeus smart-city CSV cluster.

### Path B — Direct file download (the common case)
- Resource `url` looks like
  `…/dataset/<pkg-uuid>/resource/<rid>/download/<file>.csv`.
- A GET **302-redirects to a signed Azure Blob URL**
  (`*.blob.core.windows.net/new-opendata/...?se=<expiry>&sig=…`). **Must follow redirects.**
  The SAS link is **short-lived** — resolve fresh each time; never cache the blob URL.
- Blob serves raw bytes as `application/octet-stream` (no charset header) and honors
  `Range` (206) — use a ranged GET to sniff/preview large files before a full pull.
- **All three CLAUDE.md anchor datasets are file-only** (no DataStore):
  `mcp_traffic_accidents` (CSV ~5.7 KB), `mcp_forest_fires` (2× CSV ~9.4 MB),
  `mdg_emvolio` (CSV ~2.9 MB).

### Format spread (sample of 841 resources)
ZIP 229 · PDF 162 · CSV 144 · XLS 70 · XLSX 46 · SHP 45 · KML 25 · JSON 13 · + WMS/GeoTIFF/DOC.
→ A large share is non-tabular/geospatial. **MVP supports CSV/JSON only**; flag the rest as
"not yet supported."

---

## 4. Metadata schema (for Phase 2 ingestion)

**Dataset top-level fields** (on `type=dataset`, `state=active` records): `id`, `name`
(slug), `title`, `title_translated{el,en}`, `notes`, `notes_translated{el,en}`, `type`,
`state`, `isopen`, `metadata_created`, `metadata_modified`, `num_resources`, `num_tags`,
`owner_org`, `organization{name,title,description,…}`, `tags[]`, `groups[]`, `theme[]`,
`license_id`, `license_title`, `frequency`, `language_options[]`, `temporal_coverage[]`,
`spatial_coverage[]`, `dcat_type`, `hvd_category`, `access_rights`, `url`, author/maintainer,
`tracking_summary`. `theme`/`frequency`/`language_options` are **EU Publications Office
vocabulary URIs**, not labels.

**Resource fields:** `id`, `package_id`, `name`, `description`, `format`, `mimetype`, `url`,
`size`, `hash`, `position`, `state`, `created`, `last_modified`, `metadata_modified`,
`datastore_active`, `license*`.

- **Provenance / freshness `last_updated` = `metadata_modified`** (present on every dataset;
  ISO-8601 with microseconds, **no timezone suffix** — treat as naive/UTC consistently).
- **Embedding text (Phase 3) = `title` + `notes` + `tags`**, and—since every dataset carries
  both—**also `title_translated.en` + `notes_translated.en`** for free bilingual recall.

### Proposed SQLite schema (Phase 2)
```sql
CREATE TABLE datasets (
    id                TEXT PRIMARY KEY,     -- CKAN id (UUID)
    name              TEXT UNIQUE NOT NULL, -- CKAN name (slug)
    title             TEXT,                 -- title (~= title_translated.el)
    title_en          TEXT,                 -- title_translated.en
    notes             TEXT,                 -- notes (description, Greek)
    notes_en          TEXT,                 -- notes_translated.en
    org_name          TEXT,                 -- organization.name (publisher slug)
    org_title         TEXT,                 -- organization.title (publisher, Greek)
    license_id        TEXT,                 -- often NULL
    license_title     TEXT,                 -- often NULL
    frequency         TEXT,                 -- EU vocab URI (mostly NOT_PLANNED/NULL)
    language_options  TEXT,                 -- JSON array
    theme             TEXT,                 -- JSON array (EU data-theme URIs)
    num_resources     INTEGER,              -- prefer len(resources) (field can be NULL)
    tags              TEXT,                 -- JSON array of tag names (for embedding)
    temporal_start    TEXT,                 -- temporal_coverage[0].start (often sentinel)
    temporal_end      TEXT,                 -- temporal_coverage[0].end (often sentinel)
    spatial_text      TEXT,                 -- spatial_coverage[0].text
    metadata_created  TEXT,                 -- ISO-8601
    last_updated      TEXT,                 -- *** metadata_modified (PROVENANCE) ***
    state             TEXT,                 -- filter to 'active'
    harvested_at      TEXT,                 -- our ingest timestamp
    embed_text        TEXT                  -- derived: title+notes+tags (+en)
);

CREATE TABLE resources (
    id                TEXT PRIMARY KEY,
    dataset_id        TEXT NOT NULL REFERENCES datasets(id),
    name              TEXT,
    description       TEXT,
    format            TEXT,                 -- CSV/JSON/XLSX/WMS/...
    mimetype          TEXT,
    url               TEXT,                 -- download endpoint (302 -> signed blob)
    size              INTEGER,              -- may be NULL
    datastore_active  INTEGER,              -- 0/1 -> queryable via datastore_search
    position          INTEGER,
    last_modified     TEXT,                 -- resource freshness / cache key
    metadata_modified TEXT,
    state             TEXT,
    is_tabular        INTEGER               -- derived: format in (CSV,JSON,XLS,XLSX)
);
CREATE INDEX idx_resources_dataset ON resources(dataset_id);
```

---

## 5. Quirks the harvester/normalizer must handle

1. **`package_list` (22004) > `package_search` count (21930):** the list includes non-dataset
   objects (e.g. *showcases*, which lack `organization`). Harvest via `package_search` and/or
   filter `type == 'dataset'` and `state == 'active'`.
2. **`license_id`/`license_title` often `None`**; `isopen` was `False` even on flagship
   datasets. Store NULL; never assume an open license.
3. **`num_resources` can be `None`** while `resources[]` is empty — derive from `len(resources)`.
4. **Bilingual dual fields** (`title` vs `title_translated`, `notes` vs `notes_translated`):
   keep both languages; don't drop English.
5. **`spatial_coverage` geometry fields are JSON-encoded *strings*** (stringified GeoJSON);
   `temporal_coverage` often uses sentinels (`1900-01-01` … `2099-12-31`) meaning
   "unspecified" — don't surface as real coverage.
6. **Vocab URIs** for `theme`/`frequency`/`language_options` — map to labels at display time.
7. **Downloads:** no charset header (`octet-stream`) — decode UTF-8 with a
   `charset-normalizer` fallback rather than trusting headers.

---

## 6. Recommendations by phase

- **Phase 2 (ingest):** `harvest.py` walks `package_search` (`rows=1000`, paging `start`);
  `normalize.py` maps to the schema above, derives `embed_text`, `is_tabular`, and a clean
  `num_resources`; store `metadata_modified` as `last_updated`. Filter to
  `type=dataset`/`state=active`.
- **Phase 5 (access):** `data_client.py` branches per resource — **DataStore** (`datastore_search`,
  `limit ≤ 32000`, page on `offset`) when `datastore_active`, else **file download**
  (`follow_redirects=True`, fetch `url` fresh, decode UTF-8 + normalizer fallback, parse with
  stdlib `csv`/`json`), else flag non-tabular as unsupported. Cache by
  `resource_id` + `last_modified`. **Never** use `datastore_search_sql` (disabled) or the
  legacy `query/{dataset}` endpoint (gone). All anonymous.
- **Open for a later probe:** confirm DataStore field types across more resources; verify the
  Azure SAS expiry window; spot-check a non-UTF-8 straggler actually exists in the wild.
