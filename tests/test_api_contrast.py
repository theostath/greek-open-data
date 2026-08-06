"""WCAG contrast, measured rather than asserted.

The Phase 7 acceptance criteria require WCAG 2.2 AA, and "we chose a dark-looking grey" is not
evidence. This computes the real ratios from the OKLCH tokens and pins them, so a later tweak
toward elegance fails ``make check`` instead of shipping.

Two thresholds are in play. **1.4.3** wants 4.5:1 for body text. **1.4.11** wants 3:1 for
non-text UI — a focus ring and a control's own boundary both count, which is what the first
run of this check caught: the DESIGN.md accent anchor measured 2.76:1 and the input border
1.87:1.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

CSS = Path("static/app.css")


def _srgb(lightness: float, chroma: float, hue_deg: float) -> tuple[float, float, float]:
    """OKLCH -> linear-light sRGB (values may fall outside 0..1 when out of gamut)."""
    hue = math.radians(hue_deg)
    a, b = chroma * math.cos(hue), chroma * math.sin(hue)
    l3 = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m3 = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s3 = (lightness - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3,
        -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3,
        -0.0041960863 * l3 - 0.7034186147 * m3 + 1.7076147010 * s3,
    )


def _luminance(colour: tuple[float, float, float]) -> float:
    """WCAG relative luminance of an OKLCH triple."""
    r, g, b = (min(1.0, max(0.0, channel)) for channel in _srgb(*colour))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(foreground: tuple[float, float, float],
             background: tuple[float, float, float]) -> float:
    """WCAG contrast ratio between two OKLCH colours."""
    a, b = _luminance(foreground), _luminance(background)
    return (max(a, b) + 0.05) / (min(a, b) + 0.05)


# Kept in step with static/app.css by test_the_tokens_still_match_the_stylesheet below.
BG = (1.0, 0.0, 0.0)
SUNKEN = (0.975, 0.0, 0.0)
INK = (0.24, 0.0, 0.0)
INK_QUIET = (0.46, 0.0, 0.0)
ACCENT = (0.660, 0.130, 60.0)
ACCENT_INK = (0.20, 0.03, 60.0)
CONTROL_BORDER = (0.66, 0.0, 0.0)

CSS_LITERALS = ("--accent: oklch(0.660 0.130 60)", "--control-border: oklch(0.66 0 0)",
                "--ink: oklch(0.24 0 0)", "--ink-quiet: oklch(0.46 0 0)")


@pytest.mark.parametrize(("name", "fg", "bg", "needed"), [
    # 1.4.3 — text.
    ("body ink on the page", INK, BG, 4.5),
    ("body ink on the provenance panel", INK, SUNKEN, 4.5),
    # "Secondary" means smaller and calmer, never lighter than legible. Placeholder text is
    # held to the same bar, which is the single most common AA failure in AI-made UI.
    ("secondary ink on the page", INK_QUIET, BG, 4.5),
    ("secondary ink on the provenance panel", INK_QUIET, SUNKEN, 4.5),
    ("button label on the accent fill", ACCENT_INK, ACCENT, 4.5),
    # 1.4.11 — non-text UI.
    ("focus ring / accent fill against the page", ACCENT, BG, 3.0),
    ("form control boundary against the page", CONTROL_BORDER, BG, 3.0),
])
def test_a_token_pair_meets_its_wcag_threshold(
    name: str, fg: tuple[float, float, float], bg: tuple[float, float, float], needed: float
) -> None:
    measured = contrast(fg, bg)

    assert measured >= needed, f"{name}: {measured:.2f}:1, needs {needed}:1"


def test_the_tokens_still_match_the_stylesheet() -> None:
    """Guards the guard: these ratios mean nothing if app.css has moved on without them."""
    css = CSS.read_text(encoding="utf-8")

    for literal in CSS_LITERALS:
        assert literal in css, f"{literal!r} is no longer in app.css; re-measure the ratios"


def test_the_accent_hue_has_not_drifted() -> None:
    """DESIGN.md pins the accent hue to within ±10° of 60."""
    assert abs(ACCENT[2] - 60.0) <= 10.0
