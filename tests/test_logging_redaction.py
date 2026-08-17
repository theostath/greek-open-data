"""Regression tests for credential redaction in logs.

Verified 2026-07-29 before the fix: httpx emits "HTTP Request: GET <full-url>" at INFO on
its own logger, and configure_logging sets the ROOT logger to INFO — so a signed Azure Blob
URL reached our structured logs without any of our code logging it. raise_for_status embeds
it too. Discipline in our modules was not sufficient.
"""

from __future__ import annotations

import io
import logging

from pythia.logging_setup import configure_logging, redact_secrets

SIGNED = (
    "https://x.blob.core.windows.net/new-opendata/f.csv"
    "?se=2026-07-30T00%3A00%3A00Z&sp=r&sv=2022-11-02&sig=SUPERSECRETSIGNATURE%3D"
)


def test_redacts_azure_sas_signature() -> None:
    """The sig value is removed; the rest of the URL stays readable."""
    redacted = redact_secrets(SIGNED)
    assert "SUPERSECRETSIGNATURE" not in redacted
    assert "blob.core.windows.net" in redacted
    assert "sig=REDACTED" in redacted


def test_redacts_other_credential_params() -> None:
    """se/sp/sv/token/key and the portal's own fdl token are all stripped."""
    for param in ("se", "sp", "sv", "st", "skoid", "fdl", "token", "key", "api_key"):
        redacted = redact_secrets(f"https://h/x?{param}=SECRETVALUE")
        assert "SECRETVALUE" not in redacted, param


def test_httpx_logger_is_silenced_and_filtered() -> None:
    """An httpx-style INFO record cannot carry a signature into the handler output."""
    configure_logging()
    stream = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(root.handlers[0].formatter)
    for existing in root.handlers:
        for filt in existing.filters:
            handler.addFilter(filt)
    root.addHandler(handler)
    try:
        # Emit through the httpx logger exactly as httpx does, at WARNING so the level
        # change cannot mask the redaction being exercised.
        logging.getLogger("httpx").warning('HTTP Request: GET %s "HTTP/1.1 200 OK"', SIGNED)
    finally:
        root.removeHandler(handler)
    assert "SUPERSECRETSIGNATURE" not in stream.getvalue()


def test_httpx_info_logging_is_disabled() -> None:
    """We also stop httpx emitting request URLs at all, rather than relying on redaction."""
    configure_logging()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_traceback_text_is_redacted() -> None:
    """httpx puts the request URL in exception messages, which land in payload['exc']."""
    configure_logging()
    stream = io.StringIO()
    root = logging.getLogger()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(root.handlers[0].formatter)
    root.addHandler(handler)
    try:
        try:
            raise RuntimeError(f"Client error '403 Forbidden' for url '{SIGNED}'")
        except RuntimeError:
            logging.getLogger("test").exception("fetch failed")
    finally:
        root.removeHandler(handler)
    assert "SUPERSECRETSIGNATURE" not in stream.getvalue()
