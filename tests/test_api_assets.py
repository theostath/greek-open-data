"""Vendored asset integrity.

A CDN would break the local-first guarantee, so htmx and ECharts are committed. Their
hashes are asserted here rather than merely recorded in ADR-0008, so silent drift fails
``make check`` instead of relying on someone noticing in review.
"""

from __future__ import annotations

import hashlib
import re
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


#: The three crops of the owner-supplied painting: the masthead band and the two gutter strips.
#: Each excludes the figure in the source by its crop box, and each is committed and hashed like
#: any other vendored asset — a background image referenced by URL is exactly the kind of file
#: that gets swapped for a 5 MB original without anyone noticing.
ART = {
    "masthead.webp": "905f1493f6fe64668c4f9d0d8d759b11142034ff8059043cf7a87c15798074f9",
    "field-left.webp": "29c5922384be401ec424a7d67008e8bae99558df3415ae15f4815080d004532f",
    "field-right.webp": "c2d731138097e81cac15b2ff01bb9c766e08e19b297bf22af271d136e0784a7b",
}
ART_DIR = Path("static/img")

#: All of it is decoration on a page whose job is a fast, sourced figure, so the *total* gets a
#: ceiling rather than each file getting its own — three files at 119 KB each would pass a
#: per-file limit and still be three quarters of a megabyte.
ART_MAX_TOTAL_BYTES = 220_000


@pytest.mark.parametrize("name", sorted(ART))
def test_a_painting_crop_matches_its_pinned_hash(name: str) -> None:
    path = ART_DIR / name
    assert path.is_file(), f"{name} is not vendored"

    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    assert digest == ART[name], f"{name} drifted from its committed crop"


def test_the_decoration_stays_within_its_byte_budget() -> None:
    """Decoration is allowed to be here; it is not allowed to grow unnoticed."""
    total = sum((ART_DIR / name).stat().st_size for name in ART)

    assert total <= ART_MAX_TOTAL_BYTES, f"page art is {total} bytes; re-encode or drop a crop"


def test_every_asset_url_in_the_stylesheet_resolves() -> None:
    """A typo in a background-image URL is invisible until someone opens the page."""
    css = Path("static/app.css").read_text(encoding="utf-8")

    for url in re.findall(r"url\(\"(/static/[^\"]+)\"\)", css):
        assert Path(url.lstrip("/")).is_file(), f"{url} is referenced by app.css but missing"


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
