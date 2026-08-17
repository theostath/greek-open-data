"""URL policy for the access layer (pure, no I/O).

Resource URLs are publisher-supplied free text copied verbatim from CKAN, and 75% of
fetchable CSV/JSON resources point off-portal at ~51 third-party hosts. Redirects are
therefore followed manually so every hop can be checked here; ``follow_redirects=True``
would make per-hop validation impossible.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

from pythia.access.models import UnsupportedResourceError

_ALLOWED_SCHEMES = ("https", "http")
_PORTAL_HOST_SUFFIX = "data.gov.gr"


@dataclass(frozen=True)
class Target:
    """A URL that passed policy, with the facts the caller needs for provenance."""

    url: str
    scheme: str
    host: str
    off_portal: bool


def check_url(url: str | None, *, allow_http: bool, allow_off_portal: bool) -> Target:
    """Validate a resource URL against scheme/host policy, or raise.

    Raises ``UnsupportedResourceError`` — a policy refusal is not an upstream outage.
    """
    if url is None or not url.strip():
        raise UnsupportedResourceError("resource has no URL")
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsupportedResourceError(f"unsupported URL scheme: {scheme or '(none)'}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsupportedResourceError("resource URL has no host")
    if scheme == "http" and not allow_http:
        raise UnsupportedResourceError(
            "refusing plaintext http:// (set access_allow_http to override)"
        )
    off_portal = not (host == _PORTAL_HOST_SUFFIX or host.endswith("." + _PORTAL_HOST_SUFFIX))
    if off_portal and not allow_off_portal:
        raise UnsupportedResourceError(f"off-portal host not allowed: {host}")
    # Deliberately no DNS here: this function stays pure so it is testable offline. Address
    # validation is check_hop, which the transport calls before every connection — including
    # the first — so the initial URL is checked at exactly the moment it is used.
    return Target(url=url.strip(), scheme=scheme, host=host, off_portal=off_portal)


def check_hop(host: str) -> None:
    """Reject a host that resolves anywhere we must never fetch from.

    Blocks loopback, private, link-local, CGNAT and reserved ranges. Ollama listens on
    ``localhost:11434`` on the dev machine, so an attacker-controlled catalog URL (or a
    redirect from a compromised municipal host) would otherwise be an SSRF primitive;
    ``169.254.169.254`` matters the moment this is hosted.

    Residual risk: DNS rebinding. We validate the resolved address but connect by hostname,
    so a TTL-0 record could resolve differently on the actual connection. Accepted for the
    MVP; revisit if the backend is ever publicly hosted.
    """
    for address in _resolve(host):
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
            or _is_cgnat(address)
        ):
            raise UnsupportedResourceError(f"refusing non-public address for host {host}")


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Return every address a host resolves to; a literal IP resolves to itself."""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsupportedResourceError(f"cannot resolve host {host}") from exc
    return [ipaddress.ip_address(str(info[4][0])) for info in infos]


def _is_cgnat(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether the address is in the 100.64.0.0/10 carrier-grade NAT range."""
    return address.version == 4 and address in ipaddress.ip_network("100.64.0.0/10")
