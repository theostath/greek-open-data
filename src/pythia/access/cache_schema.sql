-- Phase 5 response cache (ADR-0006). Volatile; safe to delete at any time.
-- Stores raw upstream bodies so a parser fix does not force a re-download of the portal.

CREATE TABLE IF NOT EXISTS response_cache (
    resource_id     TEXT NOT NULL,
    -- Which catalog field produced the key, kept separate from its value: collapsing them
    -- lets a disappearing last_modified resurrect an older metadata_modified-keyed body.
    key_field       TEXT NOT NULL,          -- 'last_modified' | 'metadata_modified'
    key_value       TEXT NOT NULL,
    -- The path that actually produced these bytes. Never re-derive from datastore_active:
    -- after a DataStore 404 falls back to download, the live flag still says 'datastore'.
    access_path     TEXT NOT NULL,          -- 'datastore' | 'download'
    resource_format TEXT,                   -- as declared at fetch time
    body            BLOB NOT NULL,          -- zlib-compressed raw payload
    bytes_read      INTEGER NOT NULL,       -- uncompressed size
    parser_version  INTEGER NOT NULL,       -- bump to invalidate after a parser change
    cached_at       TEXT NOT NULL,          -- ISO-8601 UTC
    last_accessed   TEXT NOT NULL,          -- ISO-8601 UTC, for LRU eviction
    PRIMARY KEY (resource_id, key_field, key_value)
);

CREATE INDEX IF NOT EXISTS idx_cache_lru ON response_cache (last_accessed);
