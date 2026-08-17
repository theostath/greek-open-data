"""Structured JSON logging configuration."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from typing import Any

# Query-string credentials that must never reach a log line. Azure SAS (`sig`, `se`, `sp`,
# `sv`, `st`, `skoid`, `sktid`) plus the portal's own `fdl` token and generic key/token names.
_SECRET_PARAMS = re.compile(
    r"([?&](?:sig|se|sp|sv|st|skoid|sktid|fdl|token|key|api_key)=)[^&\s'\"]*",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Strip credential values out of any URL embedded in ``text``."""
    return _SECRET_PARAMS.sub(r"\1REDACTED", text)


class RedactingFilter(logging.Filter):
    """Redact credentials from every record, whoever emitted it.

    Belt-and-braces: ``httpx`` logs the full request URL at INFO on its own, so a signed
    Azure Blob URL reaches our handler without any of our code logging it. Discipline in
    our modules is not sufficient — a future dependency will do this again.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Rewrite the record's message and args in place; never drop records."""
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    key: redact_secrets(value) if isinstance(value, str) else value
                    for key, value in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(arg) if isinstance(arg, str) else arg for arg in record.args
                )
        return True


class JsonFormatter(logging.Formatter):
    """Format log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Render a log record as a JSON string."""
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            # Tracebacks re-embed request URLs (httpx puts them in exception messages), so
            # redact here too rather than trusting the filter to have seen the formatted text.
            payload["exc"] = redact_secrets(self.formatException(record.exc_info))
        payload["msg"] = redact_secrets(payload["msg"])
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging to emit structured JSON to stderr, with secrets redacted."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs "HTTP Request: GET <full-url>" at INFO — including a signed Azure Blob URL
    # on every download. Redaction covers it, but there is no reason to emit it at all.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger."""
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, msg: str, **fields: Any) -> None:
    """Emit a structured log event with arbitrary key/value ``fields``."""
    logger.log(level, msg, extra={"extra_fields": fields})
