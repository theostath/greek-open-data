"""Identify what bytes actually are, before anything tries to parse them (pure).

The catalog's ``format`` field lies: 70% of CSV/JSON resource URLs are extensionless query
APIs, and resources declared CSV are observed serving HTML. Without this gate an HTML 404
page parses into a one-column table of markup and reaches Phase 6 as grounded data —
``charset_normalizer`` will not save us, because it returns a best-effort guess for
arbitrary binary rather than failing.
"""

from __future__ import annotations

from pythia.access.models import MalformedPayloadError

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"PK\x03\x04", "zip"),  # also xlsx/ods
    (b"%PDF", "pdf"),
    (b"\xd0\xcf\x11\xe0", "ole"),  # legacy xls/doc
    (b"\x1f\x8b", "gzip"),
    (b"BZh", "bzip2"),
    (b"\x89PNG", "png"),
    (b"GIF8", "gif"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"SQLite format 3", "sqlite"),
)

_HTML_MARKERS = ("<!doctype html", "<html", "<head", "<body")
_XML_MARKERS = ("<?xml", "<featurecollection", "<wfs:", "<serviceexceptionreport")

# Formats this layer is willing to parse (mirrors planning.select._SUPPORTED_FORMATS).
_PARSEABLE = ("csv", "json")


def identify(head: bytes, content_type: str | None = None) -> str:
    """Return a coarse kind for the payload: csv|json|html|xml|zip|pdf|ole|binary|empty."""
    if not head.strip():
        return "empty"
    for signature, kind in _MAGIC:
        if head.startswith(signature):
            return kind
    if content_type and "html" in content_type.lower():
        return "html"
    # Decode a small prefix leniently: this is classification, not the real decode.
    prefix = head[:1024].decode("utf-8", errors="ignore").lstrip("﻿").lstrip().lower()
    if any(prefix.startswith(marker) for marker in _HTML_MARKERS):
        return "html"
    if any(prefix.startswith(marker) for marker in _XML_MARKERS):
        return "xml"
    if prefix.startswith(("{", "[")):
        return "json"
    if b"\x00" in head[:4096]:
        return "binary"
    return "csv"


def ensure_matches_declared(kind: str, declared_format: str | None) -> None:
    """Raise unless the sniffed kind is consistent with the catalog's declared format.

    ``csv`` and ``json`` are allowed to disagree with each other — the declared format is
    frequently wrong and both are parseable — but anything else is a refusal, because the
    alternative is presenting markup or binary as data.
    """
    declared = (declared_format or "").strip().lower()
    if kind == "empty":
        raise MalformedPayloadError("resource returned an empty body")
    if kind not in _PARSEABLE:
        raise MalformedPayloadError(
            f"resource declared {declared or 'unknown'} but the body looks like {kind}"
        )
