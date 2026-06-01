-- Catalog metadata harvested from the data.gov.gr CKAN portal (Phase 2).
-- One file, committed schema. See docs/api_findings.md for field provenance.

CREATE TABLE IF NOT EXISTS datasets (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,     -- CKAN slug; NOT unique (upstream has collisions)
    title             TEXT,
    title_en          TEXT,
    notes             TEXT,
    notes_en          TEXT,
    org_name          TEXT,
    org_title         TEXT,
    license_id        TEXT,
    license_title     TEXT,
    frequency         TEXT,
    language_options  TEXT,             -- JSON array
    theme             TEXT,             -- JSON array
    num_resources     INTEGER NOT NULL,
    tags              TEXT,             -- JSON array
    temporal_start    TEXT,
    temporal_end      TEXT,
    spatial_text      TEXT,
    metadata_created  TEXT,
    last_updated      TEXT,             -- CKAN metadata_modified (provenance/freshness)
    state             TEXT,
    harvested_at      TEXT NOT NULL,
    embed_text        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    id                TEXT PRIMARY KEY,
    dataset_id        TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
    name              TEXT,
    description       TEXT,
    format            TEXT,
    mimetype          TEXT,
    url               TEXT,
    size              INTEGER,
    datastore_active  INTEGER NOT NULL,  -- 0/1
    position          INTEGER,
    last_modified     TEXT,
    metadata_modified TEXT,
    state             TEXT,
    is_tabular        INTEGER NOT NULL   -- 0/1
);

CREATE INDEX IF NOT EXISTS idx_datasets_name ON datasets(name);
CREATE INDEX IF NOT EXISTS idx_resources_dataset ON resources(dataset_id);
