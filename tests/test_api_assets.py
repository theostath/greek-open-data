"""Vendored asset integrity.

A CDN would break the local-first guarantee, so htmx and the Vega bundles are committed. Their
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
    "vega.min.js": "e432c751a6363f4a61da62920cc7d7ebd13cf09d82949f8f486248f8071dc3ce",
    "vega-lite.min.js": "cd32314b1e76e7d879dc9f0534b62be714df03554486c7ca2381abfd0a92d2f4",
    "vega-embed.min.js": "072c054f2a6310725e038c38a71e00052705e31835632462c9717a23a384e895",
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
