"""Tests for the access-layer URL policy (Phase 5).

75% of fetchable resources point off-portal at publisher-supplied URLs, and Ollama listens
on localhost:11434 on the dev box — so these are the SSRF and downgrade defences.
"""

from __future__ import annotations

import pytest

from pythia.access.guard import check_hop, check_url
from pythia.access.models import UnsupportedResourceError

_OK = {"allow_http": False, "allow_off_portal": True}


def test_https_portal_url_allowed() -> None:
    """A normal portal URL passes and is flagged as on-portal."""
    target = check_url("https://data.gov.gr/dataset/x/resource/y/download/z.csv", **_OK)
    assert target.host == "data.gov.gr"
    assert target.off_portal is False
    assert target.scheme == "https"


def test_off_portal_https_allowed_but_flagged() -> None:
    """A municipal GeoServer URL is allowed and marked off_portal for provenance."""
    target = check_url("https://gis.crete.gov.gr/geoserver/ows?service=WFS", **_OK)
    assert target.off_portal is True


@pytest.mark.parametrize("url", [None, "", "   "])
def test_missing_url_rejected(url: str | None) -> None:
    """6 catalog resources have no URL; that is a typed refusal, not a crash."""
    with pytest.raises(UnsupportedResourceError):
        check_url(url, **_OK)


def test_ftp_scheme_rejected() -> None:
    """Two catalog resources are ftp://; we do not speak it."""
    with pytest.raises(UnsupportedResourceError):
        check_url("ftp://example.gov.gr/data.csv", **_OK)


def test_plain_http_rejected_by_default() -> None:
    """109 resources are plain http://; cleartext is opt-in, not silent."""
    with pytest.raises(UnsupportedResourceError):
        check_url("http://gis.example.gov.gr/data.csv", **_OK)


def test_plain_http_allowed_when_opted_in() -> None:
    """The override exists and is explicit."""
    target = check_url("http://gis.example.invalid/data.csv", allow_http=True,
                       allow_off_portal=True)
    assert target.scheme == "http"


def test_off_portal_rejected_when_disallowed() -> None:
    """The off-portal switch actually gates."""
    with pytest.raises(UnsupportedResourceError):
        check_url("https://gis.crete.gov.gr/x.csv", allow_http=False, allow_off_portal=False)


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "169.254.169.254", "10.0.0.5", "192.168.1.1", "100.64.0.1",
     "0.0.0.0", "::1"],
)
def test_non_public_addresses_rejected(host: str) -> None:
    """Loopback, RFC1918, link-local, CGNAT and unspecified are all refused.

    localhost:11434 is a live Ollama instance on this machine, and 169.254.169.254 is the
    cloud metadata endpoint — both are the SSRF targets this guard exists for.
    """
    with pytest.raises(UnsupportedResourceError):
        check_hop(host)


def test_check_url_does_no_dns() -> None:
    """URL policy stays pure: an unresolvable host passes here and is caught by check_hop.

    Address validation belongs to check_hop, which the transport runs before every
    connection — including the first — so nothing is fetched unvalidated.
    """
    target = check_url("https://no-such-host.invalid/data.csv", **_OK)
    assert target.host == "no-such-host.invalid"
    with pytest.raises(UnsupportedResourceError):
        check_hop(target.host)
