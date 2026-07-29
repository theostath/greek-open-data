"""The only module in the access layer that performs I/O.

Protocol + fake mirrors ``llm.LLMClient``/``retrieval.rerank.Scorer`` so the orchestrator is
testable offline. Two deliberate divergences from the ``llm.py`` idiom, both load-bearing:

* ``ReadTimeout`` **is** retried here (a GET is idempotent; an LLM generation is not).
* ``str(exc)`` is **never** put into an ``AccessError``. httpx embeds the request URL in its
  exception messages, and after a redirect that URL carries the signed-blob credential.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from pythia.access.guard import check_hop
from pythia.access.models import ResourceUnavailableError

_USER_AGENT = "pythia-access/0.1 (+https://github.com/theostath/greek-open-data)"
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_RETRY_AFTER_S = 30.0


@dataclass(frozen=True)
class RawResponse:
    """Bytes plus the facts the caller may keep.

    Deliberately carries ``final_host``, never the final URL: the resolved Azure Blob URL is
    a credential and must not escape this module.
    """

    body: bytes
    complete: bool  # False when the byte cap stopped the read
    status: int
    content_type: str | None
    redirected: bool
    final_host: str


class Deadline:
    """A monotonic wall-clock budget shared across redirects, pages and retries."""

    def __init__(self, seconds: float) -> None:
        """Start the budget now."""
        self._end = time.monotonic() + seconds

    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self._end - time.monotonic())

    def check(self) -> None:
        """Raise if the budget is exhausted."""
        if self.remaining() <= 0:
            raise ResourceUnavailableError("access deadline exceeded")


class Transport(Protocol):
    """Minimal HTTP surface the data client depends on."""

    def get_json(self, url: str, params: dict[str, Any], *, max_bytes: int,
                 deadline: Deadline) -> dict[str, Any]:
        """GET a JSON document, bounded by ``max_bytes``."""
        ...

    def get_bytes(self, url: str, *, max_bytes: int, deadline: Deadline) -> RawResponse:
        """GET raw bytes, streaming and stopping at ``max_bytes``."""
        ...


class HttpxTransport:
    """``Transport`` over httpx with manual redirects, streaming caps and politeness."""

    def __init__(self, client: httpx.Client, *, max_redirects: int, attempts: int,
                 min_throughput_bps: int, host_min_interval_s: float) -> None:
        """Wrap a caller-owned client; the caller sets timeouts and TLS."""
        self._client = client
        self._max_redirects = max_redirects
        self._attempts = attempts
        self._min_throughput_bps = min_throughput_bps
        self._host_min_interval_s = host_min_interval_s
        self._last_request_at: dict[str, float] = {}

    def get_json(self, url: str, params: dict[str, Any], *, max_bytes: int,
                 deadline: Deadline) -> dict[str, Any]:
        """GET and parse a bounded JSON document.

        A JSON page cannot be honestly truncated, so hitting the cap is a failure rather
        than a partial result.
        """
        response = self._fetch(url, params=params, max_bytes=max_bytes, deadline=deadline)
        if not response.complete:
            raise ResourceUnavailableError("JSON response exceeded the byte cap")
        import json  # local: keeps the pure-parse dependency out of module import order

        try:
            payload = json.loads(response.body)
        except ValueError as exc:
            raise ResourceUnavailableError(f"upstream returned non-JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise ResourceUnavailableError("upstream JSON envelope was not an object")
        return payload

    def get_bytes(self, url: str, *, max_bytes: int, deadline: Deadline) -> RawResponse:
        """GET raw bytes, streaming and stopping at ``max_bytes``."""
        return self._fetch(url, params=None, max_bytes=max_bytes, deadline=deadline)

    def _fetch(self, url: str, *, params: dict[str, Any] | None, max_bytes: int,
               deadline: Deadline) -> RawResponse:
        """Run the retry loop around one logical request."""
        last: Exception | None = None
        for attempt in range(1, self._attempts + 1):
            deadline.check()
            try:
                return self._once(url, params=params, max_bytes=max_bytes, deadline=deadline)
            except _Retryable as exc:
                last = exc
                if attempt == self._attempts or (exc.status == 429 and attempt >= 2):
                    break
                wait = min(exc.retry_after or (0.5 * 2**attempt), _MAX_RETRY_AFTER_S,
                           deadline.remaining())
                if wait <= 0:
                    break
                time.sleep(wait)
        raise ResourceUnavailableError(
            f"upstream unavailable after {self._attempts} attempt(s)"
        ) from last

    def _once(self, url: str, *, params: dict[str, Any] | None, max_bytes: int,
              deadline: Deadline) -> RawResponse:
        """Perform one request, following redirects manually with per-hop validation."""
        current = url
        redirected = False
        for _hop in range(self._max_redirects + 1):
            deadline.check()
            host = httpx.URL(current).host
            check_hop(host)  # every connection, including the first
            self._be_polite(host)
            try:
                with self._client.stream(
                    "GET", current, params=params, follow_redirects=False,
                    headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"},
                    timeout=httpx.Timeout(deadline.remaining(), connect=None),
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ResourceUnavailableError("redirect without a location")
                        current = str(httpx.URL(current).join(location))
                        check_hop(httpx.URL(current).host)
                        redirected = True
                        continue
                    if response.status_code in _RETRY_STATUSES:
                        raise _Retryable(response.status_code,
                                         _retry_after(response.headers.get("retry-after")))
                    if response.status_code >= 400:
                        # No raise_for_status(): its message embeds the (signed) URL.
                        raise ResourceUnavailableError(
                            f"upstream returned HTTP {response.status_code}"
                        )
                    body, complete = self._read_capped(response, max_bytes, deadline)
                    return RawResponse(
                        body=body, complete=complete, status=response.status_code,
                        content_type=response.headers.get("content-type"),
                        redirected=redirected, final_host=httpx.URL(current).host or "",
                    )
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError,
                    httpx.ReadError) as exc:
                raise _Retryable(None, None) from exc
            except httpx.HTTPError as exc:
                raise ResourceUnavailableError(
                    f"transport failure: {type(exc).__name__}"
                ) from None
        raise ResourceUnavailableError(f"exceeded {self._max_redirects} redirects")

    def _read_capped(self, response: httpx.Response, max_bytes: int,
                     deadline: Deadline) -> tuple[bytes, bool]:
        """Stream the body, stopping at the cap or when throughput collapses.

        Uses ``iter_bytes`` (decoded) rather than ``.content``: httpx's gzip decoder has no
        length bound, so reading the whole body first would defeat the cap entirely.
        """
        chunks: list[bytes] = []
        total = 0
        started = time.monotonic()
        for chunk in response.iter_bytes():
            deadline.check()
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                return b"".join(chunks)[:max_bytes], False
            elapsed = time.monotonic() - started
            if elapsed > 15 and total / elapsed < self._min_throughput_bps:
                raise ResourceUnavailableError("transfer below the minimum throughput floor")
        return b"".join(chunks), True

    def _be_polite(self, host: str | None) -> None:
        """Space out requests per host; 76% of fetches hit small municipal servers."""
        if not host:
            return
        last = self._last_request_at.get(host)
        now = time.monotonic()
        if last is not None and (gap := self._host_min_interval_s - (now - last)) > 0:
            time.sleep(gap)
        self._last_request_at[host] = time.monotonic()


class _Retryable(Exception):
    """Internal marker for a condition worth retrying."""

    def __init__(self, status: int | None, retry_after: float | None) -> None:
        """Record the status and any server-supplied backoff."""
        super().__init__(f"retryable: {status}")
        self.status = status
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    """Parse Retry-After as delta-seconds or an HTTP-date, clamped."""
    if not value:
        return None
    try:
        return min(float(value.strip()), _MAX_RETRY_AFTER_S)
    except ValueError:
        pass
    from email.utils import parsedate_to_datetime

    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    from datetime import UTC, datetime

    delta = (target - datetime.now(UTC)).total_seconds()
    return min(max(delta, 0.0), _MAX_RETRY_AFTER_S)


class FakeTransport:
    """Deterministic ``Transport`` double: canned responses keyed by URL substring."""

    def __init__(self, *, json_responses: dict[str, Any] | None = None,
                 byte_responses: dict[str, RawResponse] | None = None,
                 errors: dict[str, Exception] | None = None) -> None:
        """Configure canned results; keys are matched as substrings of the URL."""
        self._json = json_responses or {}
        self._bytes = byte_responses or {}
        self._errors = errors or {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get_json(self, url: str, params: dict[str, Any], *, max_bytes: int,
                 deadline: Deadline) -> dict[str, Any]:
        """Return the canned JSON for a matching URL."""
        self.calls.append((url, dict(params)))
        self._maybe_raise(url)
        for key, payload in self._json.items():
            if key in url:
                return dict(payload)
        raise ResourceUnavailableError("no canned JSON for URL")

    def get_bytes(self, url: str, *, max_bytes: int, deadline: Deadline) -> RawResponse:
        """Return the canned bytes for a matching URL."""
        self.calls.append((url, None))
        self._maybe_raise(url)
        for key, response in self._bytes.items():
            if key in url:
                return response
        raise ResourceUnavailableError("no canned body for URL")

    def _maybe_raise(self, url: str) -> None:
        """Raise a configured error when the URL matches."""
        for key, exc in self._errors.items():
            if key in url:
                raise exc
