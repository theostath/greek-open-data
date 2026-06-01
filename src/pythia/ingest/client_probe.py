"""Phase 1 API discovery probe for the data.gov.gr portal.

Runs a fixed set of read-only GET probes against candidate catalog and data
endpoints, detects response encoding, and writes everything it learns to
``docs/api_findings.md`` as the durable source of truth.

Design constraints (see CLAUDE.md sections 5 and 6):
- Safe to run repeatedly: GET-only, idempotent, overwrites the findings file.
- Rate-limit-friendly: short timeouts and a polite delay between requests.
- The bearer token is sent only in the ``Authorization`` header and is NEVER
  logged or written to the findings file.
- Resilient: any failure (missing token, DNS, TLS, timeout, 5xx) is recorded
  in the findings file instead of crashing.
"""

from __future__ import annotations

import logging
import ssl
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from charset_normalizer import from_bytes
from config import get_settings

from pythia.logging_setup import configure_logging, get_logger, log_event

LOGGER_NAME = "pythia.ingest.client_probe"
# Raw machine evidence; the curated source of truth is docs/api_findings.md.
FINDINGS_PATH = Path("docs/api_probe_raw.md")
USER_AGENT = "pythia-probe/0.0 (Phase 1 discovery; data.gov.gr)"
REQUEST_TIMEOUT = httpx.Timeout(15.0, connect=8.0)
POLITE_DELAY_SECONDS = 1.0
BODY_SNIPPET_CHARS = 800
KNOWN_DATASET_ID = "mcp_traffic_accidents"


@dataclass(frozen=True)
class ProbeSpec:
    """A single read-only request to attempt against the portal."""

    name: str
    purpose: str
    url: str
    params: dict[str, str] = field(default_factory=dict)
    auth: bool = False


@dataclass
class EncodingReport:
    """What the probe could infer about a response's text encoding."""

    declared_charset: str | None
    detected_encoding: str | None
    detection_confidence: float | None
    decoded_as_utf8: bool
    has_replacement_chars: bool
    non_utf8: bool
    likely_mojibake: bool


@dataclass
class ProbeResult:
    """The outcome of a single probe, success or failure."""

    spec: ProbeSpec
    ok: bool
    status: int | None = None
    content_type: str | None = None
    elapsed_ms: int | None = None
    encoding: EncodingReport | None = None
    json_keys: list[str] | None = None
    is_ckan_shape: bool | None = None
    body_snippet: str = ""
    error: str | None = None


def build_probes(base_url: str) -> list[ProbeSpec]:
    """Return the ordered list of discovery probes for ``base_url``."""
    base = base_url.rstrip("/")
    return [
        ProbeSpec(
            name="portal_root",
            purpose="Fetch the portal root to see what is served (HTML app vs API).",
            url=f"{base}/",
        ),
        ProbeSpec(
            name="ckan_package_search",
            purpose="Test whether the catalog is CKAN (Action API package_search).",
            url=f"{base}/api/3/action/package_search",
            params={"rows": "1"},
        ),
        ProbeSpec(
            name="ckan_package_list",
            purpose="Test CKAN package_list (full id listing).",
            url=f"{base}/api/3/action/package_list",
        ),
        ProbeSpec(
            name="ckan_package_show",
            purpose="Test CKAN package_show against a known dataset id.",
            url=f"{base}/api/3/action/package_show",
            params={"id": KNOWN_DATASET_ID},
        ),
        ProbeSpec(
            name="legacy_query_plain",
            purpose="Test the legacy data API pattern with no date params.",
            url=f"{base}/api/v1/query/{KNOWN_DATASET_ID}",
            auth=True,
        ),
        ProbeSpec(
            name="legacy_query_dates",
            purpose="Test the legacy data API with date_from/date_to params.",
            url=f"{base}/api/v1/query/{KNOWN_DATASET_ID}",
            params={"date_from": "2023-01-01", "date_to": "2023-12-31"},
            auth=True,
        ),
    ]


def charset_from_content_type(content_type: str | None) -> str | None:
    """Extract the declared charset from a Content-Type header, if present."""
    if not content_type:
        return None
    for part in content_type.split(";"):
        part = part.strip()
        if part.lower().startswith("charset="):
            return part.split("=", 1)[1].strip() or None
    return None


def detect_encoding(raw: bytes, declared_charset: str | None) -> EncodingReport:
    """Inspect raw bytes for charset, UTF-8 cleanliness, and mojibake markers."""
    match = from_bytes(raw).best()
    detected = match.encoding if match is not None else None
    confidence = round(1.0 - float(match.chaos), 3) if match is not None else None

    try:
        text = raw.decode("utf-8")
        decoded_as_utf8 = True
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        decoded_as_utf8 = False

    has_replacement = "�" in text
    detected_norm = (detected or "").replace("-", "_").lower()
    non_utf8 = detected is not None and detected_norm not in ("utf_8", "ascii")
    likely_mojibake = (not decoded_as_utf8) or has_replacement

    return EncodingReport(
        declared_charset=declared_charset,
        detected_encoding=detected,
        detection_confidence=confidence,
        decoded_as_utf8=decoded_as_utf8,
        has_replacement_chars=has_replacement,
        non_utf8=non_utf8,
        likely_mojibake=likely_mojibake,
    )


def inspect_json(response: httpx.Response) -> tuple[list[str] | None, bool | None]:
    """Return (top-level JSON keys, is-CKAN-shape) or (None, None) if not JSON."""
    try:
        payload = response.json()
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, False
    keys = sorted(str(k) for k in payload)
    is_ckan = "success" in payload and "result" in payload
    return keys, is_ckan


def run_probe(client: httpx.Client, spec: ProbeSpec, token: str | None) -> ProbeResult:
    """Execute one probe, capturing evidence and never raising on failure."""
    headers: dict[str, str] = {}
    if spec.auth and token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        response = client.get(spec.url, params=spec.params, headers=headers)
    except httpx.HTTPError as exc:
        # Token lives only in the header, so the exception text is token-free.
        return ProbeResult(spec=spec, ok=False, error=f"{type(exc).__name__}: {exc}")

    raw = response.content
    declared = charset_from_content_type(response.headers.get("content-type"))
    encoding = detect_encoding(raw, declared)
    snippet = raw.decode("utf-8", errors="replace")[:BODY_SNIPPET_CHARS]
    json_keys, is_ckan = inspect_json(response)

    return ProbeResult(
        spec=spec,
        ok=True,
        status=response.status_code,
        content_type=response.headers.get("content-type"),
        elapsed_ms=int(response.elapsed.total_seconds() * 1000),
        encoding=encoding,
        json_keys=json_keys,
        is_ckan_shape=is_ckan,
        body_snippet=snippet,
    )


def _result_by_name(results: list[ProbeResult], name: str) -> ProbeResult | None:
    """Return the probe result with the given name, if any."""
    return next((r for r in results if r.spec.name == name), None)


def _summary_lines(results: list[ProbeResult], token_present: bool) -> list[str]:
    """Derive confirmed-vs-unknown summary bullets from the raw results."""
    lines: list[str] = []

    ckan_hits = [
        r
        for r in results
        if r.spec.name.startswith("ckan_") and r.ok and r.status == 200 and r.is_ckan_shape
    ]
    if ckan_hits:
        lines.append("- **Catalog API (CKAN): CONFIRMED** — Action API returned the CKAN "
                     "`{success, result}` envelope.")
    elif any(r.spec.name.startswith("ckan_") and not r.ok for r in results):
        lines.append("- **Catalog API (CKAN): UNREACHABLE** — endpoints could not be reached "
                     "(see per-probe errors).")
    else:
        lines.append("- **Catalog API (CKAN): NOT CONFIRMED** — endpoints reachable but did not "
                     "return a CKAN-shaped response; inspect snippets below.")

    legacy = _result_by_name(results, "legacy_query_plain")
    legacy_is_html = bool(legacy and legacy.content_type and "html" in legacy.content_type)
    if legacy and legacy.ok and legacy.status == 200:
        lines.append("- **Legacy data API (`/api/v1/query/{id}`): RESPONDED 200** — pattern is "
                     "live; confirm payload shape in the snippet.")
    elif legacy and legacy.ok and legacy.status == 404 and legacy_is_html:
        lines.append("- **Legacy data API (`/api/v1/query/{id}`): GONE** — returns the portal's "
                     "HTML 404 page, i.e. the route does not exist post-relaunch. Data access "
                     "must use a CKAN mechanism instead (see Phase 2/5 notes).")
    elif legacy and legacy.ok:
        lines.append(f"- **Legacy data API: responded HTTP {legacy.status}** — pattern reachable "
                     "but not a 200; check auth/dataset id.")
    elif legacy and not legacy.ok:
        lines.append("- **Legacy data API: UNREACHABLE** — see error below.")
    else:
        lines.append("- **Legacy data API: not probed.**")

    lines.append(
        f"- **Auth:** bearer token was {'present' if token_present else 'NOT present'} this run; "
        "auth-required probes "
        + ("included an Authorization header." if token_present else "ran unauthenticated.")
    )

    flagged = [
        r
        for r in results
        if r.encoding and (r.encoding.non_utf8 or r.encoding.likely_mojibake)
    ]
    if flagged:
        names = ", ".join(r.spec.name for r in flagged)
        lines.append(f"- **Encoding:** non-UTF-8 / mojibake flagged on: {names}.")
    else:
        lines.append("- **Encoding:** no non-UTF-8 or mojibake detected in reachable responses.")

    return lines


def _render_result(result: ProbeResult) -> list[str]:
    """Render one probe result as Markdown lines."""
    spec = result.spec
    auth_label = "Bearer (masked)" if spec.auth else "none"
    params = spec.params or "—"
    lines = [
        f"### `{spec.name}` — {spec.purpose}",
        "",
        f"- Request: `GET {spec.url}`",
        f"- Params: `{params}`",
        f"- Auth: {auth_label}",
    ]
    if not result.ok:
        lines += [f"- Outcome: **FAILED** — `{result.error}`", ""]
        return lines

    lines += [
        f"- Outcome: **ok** — HTTP `{result.status}`, "
        f"`{result.content_type}`, {result.elapsed_ms} ms",
    ]
    if result.is_ckan_shape is not None:
        lines.append(f"- CKAN-shaped JSON: `{result.is_ckan_shape}`")
    if result.json_keys is not None:
        lines.append(f"- Top-level JSON keys: `{result.json_keys}`")
    enc = result.encoding
    if enc is not None:
        lines.append(
            f"- Encoding: declared=`{enc.declared_charset}` "
            f"detected=`{enc.detected_encoding}` (conf={enc.detection_confidence}) "
            f"utf8_clean=`{enc.decoded_as_utf8}` "
            f"replacement_chars=`{enc.has_replacement_chars}` "
            f"non_utf8=`{enc.non_utf8}` mojibake=`{enc.likely_mojibake}`"
        )
    safe_snippet = result.body_snippet.replace("```", "`​``")
    lines += ["- Body snippet:", "", "```", safe_snippet, "```", ""]
    return lines


def build_markdown(results: list[ProbeResult], token_present: bool, base_url: str) -> str:
    """Assemble the full ``api_findings.md`` document from probe results."""
    ts = datetime.now(UTC).isoformat(timespec="seconds")
    lines = [
        "# data.gov.gr — API probe raw evidence (Phase 1)",
        "",
        "> AUTO-GENERATED by `src/pythia/ingest/client_probe.py`; overwritten on every "
        "`make probe`.",
        "> The curated source of truth is [`api_findings.md`](api_findings.md) — edit that, "
        "not this file.",
        f"> Generated (UTC): {ts}",
        f"> Base URL probed: `{base_url}`",
        f"> Bearer token present this run: **{'yes' if token_present else 'no'}**",
        "",
        "## Summary — confirmed vs unknown",
        "",
        *_summary_lines(results, token_present),
        "",
        "## Probe results",
        "",
    ]
    for result in results:
        lines += _render_result(result)

    lines += [
        "## Notes for Phase 2 (ingestion)",
        "",
        "- If the catalog is CKAN: harvest via `package_search` (paginate with `rows`/`start`),",
        "  then `package_show` per id for full resource metadata.",
        "- If not CKAN: capture the real metadata endpoint and shape from the snippets above",
        "  before writing the harvester.",
        "- Persist `last_updated`/provenance for every dataset row (CLAUDE.md principle 2).",
        "- Normalize encoding on ingest; treat any probe flagged `non_utf8`/`mojibake` as a",
        "  signal that the harvester must detect-and-recode, not assume UTF-8.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    """Run all probes and write the findings document. Returns a process exit code."""
    configure_logging()
    logger = get_logger(LOGGER_NAME)
    settings = get_settings()
    token = settings.data_gov_gr_token
    base_url = settings.data_gov_gr_base_url
    log_event(
        logger, logging.INFO, "probe.start", base_url=base_url, token_present=bool(token)
    )

    # On this host the OS trust store carries the root CA needed to reach the
    # internet through the proxy; the default certifi bundle does not.
    ssl_context = ssl.create_default_context()

    results: list[ProbeResult] = []
    with httpx.Client(
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        verify=ssl_context,
        follow_redirects=True,
    ) as client:
        for index, spec in enumerate(build_probes(base_url)):
            if index:
                time.sleep(POLITE_DELAY_SECONDS)
            result = run_probe(client, spec, token)
            log_event(
                logger,
                logging.INFO,
                "probe.result",
                name=spec.name,
                ok=result.ok,
                status=result.status,
                error=result.error,
            )
            results.append(result)

    FINDINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    FINDINGS_PATH.write_text(
        build_markdown(results, token_present=bool(token), base_url=base_url),
        encoding="utf-8",
    )
    log_event(logger, logging.INFO, "probe.done", findings=str(FINDINGS_PATH))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
