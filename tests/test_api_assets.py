"""Vendored asset integrity.

A CDN would break the local-first guarantee, so htmx and ECharts are committed. Their
hashes are asserted here rather than merely recorded in ADR-0008, so silent drift fails
``make check`` instead of relying on someone noticing in review.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

VENDOR = Path("static/vendor")

#: Pinned versions and their SHA-256. Changing a bundle means changing this table, which is
#: the point: the diff makes the swap visible.
EXPECTED = {
    "htmx.min.js": "e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447",
    # Apache ECharts 5.5.1, the `common` build: line/bar/pie plus the full grid component and
    # the accessibility module (aria + decal). The full dist is 1.0 MB and buys chart types
    # this product has no use for; `simple` drops grid features the axes need.
    "echarts.common.min.js":
        "66f17003724d5b6c4c2348b907290afe98363c6e7beb4a594fdb616f00496d55",
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_vendored_bundle_matches_its_pinned_hash(name: str) -> None:
    path = VENDOR / name
    assert path.is_file(), f"{name} is not vendored; the page must work with the cable out"

    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    assert digest == EXPECTED[name], f"{name} drifted from its pinned build"


def test_no_template_reaches_for_a_cdn() -> None:
    """Local-first is a guarantee, not a preference: the page must render offline."""
    for template in Path("templates").rglob("*.html"):
        body = template.read_text(encoding="utf-8")
        for host in ("cdn.jsdelivr", "unpkg.com", "cdnjs", "googleapis", "//cdn."):
            assert host not in body, f"{template} loads from {host}"


def test_the_stylesheet_declares_a_reduced_motion_alternative() -> None:
    """WCAG 2.2 AA and DESIGN.md both make this non-optional."""
    css = Path("static/app.css").read_text(encoding="utf-8")

    assert "prefers-reduced-motion" in css
