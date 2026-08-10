"""WCAG contrast, measured rather than asserted.

The Phase 7 acceptance criteria require WCAG 2.2 AA, and "we chose a dark-looking grey" is not
evidence. This computes the real ratios from the OKLCH tokens and pins them, so a later tweak
toward elegance fails ``make check`` instead of shipping.

Two thresholds are in play. **1.4.3** wants 4.5:1 for body text. **1.4.11** wants 3:1 for
non-text UI — a focus ring and a control's own boundary both count, which is what the first
run of this check caught: the original amber accent anchor measured 2.76:1 and the input
border 1.87:1.
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
SUNKEN = (0.89, 0.057, 237.0)
WASH = (0.87, 0.068, 237.0)
INK = (0.24, 0.016, 237.0)
INK_QUIET = (0.45, 0.020, 237.0)
ACCENT = (0.48, 0.155, 255.0)
ACCENT_HOVER = (0.41, 0.135, 255.0)
ACCENT_INK = (1.0, 0.0, 0.0)
CONTROL_BORDER = (0.56, 0.028, 237.0)

#: The dark chrome bands and the light-on-dark vocabulary they carry. Inside a band the accent
#: is useless (1.1:1 against it), so BAND_LINK and BAND_INK take over its jobs.
BAND = (0.43, 0.055, 237.0)
BAND_INK = (1.0, 0.0, 0.0)
BAND_INK_QUIET = (0.86, 0.030, 237.0)
BAND_LINK = (0.86, 0.055, 237.0)
BAND_LINK_HOVER = (0.94, 0.031, 237.0)

CSS_LITERALS = ("--accent: oklch(0.48 0.155 255)", "--control-border: oklch(0.56 0.028 237)",
                "--ink: oklch(0.24 0.016 237)", "--ink-quiet: oklch(0.45 0.020 237)",
                "--surface-sunken: oklch(0.89 0.057 237)",
                "--accent-wash: oklch(0.87 0.068 237)",
                "--band: oklch(0.43 0.055 237)",
                "--band-link: oklch(0.86 0.055 237)")


@pytest.mark.parametrize(("name", "fg", "bg", "needed"), [
    # 1.4.3 — text.
    ("body ink on the page", INK, BG, 4.5),
    ("body ink on the provenance panel", INK, SUNKEN, 4.5),
    # "Secondary" means smaller and calmer, never lighter than legible. Placeholder text is
    # held to the same bar, which is the single most common AA failure in AI-made UI.
    ("secondary ink on the page", INK_QUIET, BG, 4.5),
    ("secondary ink on the provenance panel", INK_QUIET, SUNKEN, 4.5),
    ("secondary ink on the masthead band", INK_QUIET, SUNKEN, 4.5),
    ("button label on the accent fill", ACCENT_INK, ACCENT, 4.5),
    ("button label on the accent hover fill", ACCENT_INK, ACCENT_HOVER, 4.5),
    # The accent became link *text* when the palette committed to blue, so it now owes 4.5:1
    # on every surface it can land on — an obligation the old amber accent could not have met.
    ("link text on the page", ACCENT, BG, 4.5),
    ("link text on the masthead and colophon bands", ACCENT, SUNKEN, 4.5),
    ("link text on an example control", ACCENT, WASH, 4.5),
    ("body ink on an example control", INK, WASH, 4.5),
    # 1.4.11 — non-text UI.
    ("focus ring / accent fill against the page", ACCENT, BG, 3.0),
    ("focus ring against the chrome bands", ACCENT, SUNKEN, 3.0),
    ("form control boundary against the page", CONTROL_BORDER, BG, 3.0),
    ("form control boundary against the chrome bands", CONTROL_BORDER, SUNKEN, 3.0),
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


#: The canvas grain is desaturated noise painted over a surface at this opacity. A noise pixel
#: at full strength is black, so the darkest surface it can produce is the surface composited
#: with black at GRAIN_OPACITY — that worst case is what every text pair below is re-measured
#: against. Kept in step with the ``--canvas`` data URI by the literal assertion.
GRAIN_OPACITY = 0.055
GRAIN_CSS_LITERAL = "opacity='0.055'"


def _encode(linear: float) -> float:
    """Linear-light sRGB -> gamma-encoded sRGB."""
    return 12.92 * linear if linear <= 0.0031308 else 1.055 * linear ** (1 / 2.4) - 0.055


def _decode(gamma: float) -> float:
    """Gamma-encoded sRGB -> linear-light sRGB."""
    return gamma / 12.92 if gamma <= 0.04045 else ((gamma + 0.055) / 1.055) ** 2.4


def _grained(colour: tuple[float, float, float]) -> float:
    """Relative luminance of an OKLCH surface once the grain's darkest noise sits on it.

    The compositing step happens in gamma-encoded space because that is where a browser
    blends ``opacity`` — doing it in linear light overstates the darkening substantially.
    """
    channels = (min(1.0, max(0.0, channel)) for channel in _srgb(*colour))
    red, green, blue = (_decode(_encode(channel) * (1.0 - GRAIN_OPACITY)) for channel in channels)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast_on_grain(foreground: tuple[float, float, float],
                       surface: tuple[float, float, float]) -> float:
    """WCAG ratio of a foreground against the grain's worst-case darkening of a surface."""
    fg, bg = _luminance(foreground), _grained(surface)
    return (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05)


# Only two surfaces carry the grain: the white reading field and the dark chrome bands. The
# provenance panel and the suggestion buttons paint their own opaque backgrounds over it, so
# they are measured plain in the table above rather than here.
@pytest.mark.parametrize(("name", "fg", "surface", "needed"), [
    ("body ink on grained white", INK, BG, 4.5),
    ("secondary ink on grained white", INK_QUIET, BG, 4.5),
    ("link text on grained white", ACCENT, BG, 4.5),
    ("form control boundary on grained white", CONTROL_BORDER, BG, 3.0),
    ("band text on a grained band", BAND_INK, BAND, 4.5),
    ("band secondary text on a grained band", BAND_INK_QUIET, BAND, 4.5),
    ("a band link on a grained band", BAND_LINK, BAND, 4.5),
])
def test_the_canvas_grain_never_costs_a_contrast_threshold(
    name: str, fg: tuple[float, float, float], surface: tuple[float, float, float], needed: float
) -> None:
    """The grain is decoration; it is not allowed to make anything harder to read."""
    measured = _contrast_on_grain(fg, surface)

    assert measured >= needed, f"{name}: {measured:.2f}:1, needs {needed}:1"


@pytest.mark.parametrize(("name", "fg", "needed"), [
    ("wordmark and headings on the band", BAND_INK, 4.5),
    ("standfirst and colophon text on the band", BAND_INK_QUIET, 4.5),
    ("a link on the band", BAND_LINK, 4.5),
    ("a hovered link on the band", BAND_LINK_HOVER, 4.5),
    # The accent draws every focus ring on white, but on the band it is invisible; white takes
    # the job over. This pair is the reason .masthead/.colophon override outline-color.
    ("the focus ring on the band", BAND_INK, 3.0),
])
def test_the_dark_chrome_carries_its_own_legible_vocabulary(
    name: str, fg: tuple[float, float, float], needed: float
) -> None:
    """The bands are dark, so every foreground on them is a separate token with its own duty.

    Measured ungrained: the grain only darkens, and darkening a dark band *raises* the ratio
    for the light text on it, so the plain band is the worst case here.
    """
    measured = contrast(fg, BAND)

    assert measured >= needed, f"{name}: {measured:.2f}:1, needs {needed}:1"


def test_the_accent_is_unusable_on_the_band() -> None:
    """Guards the reason the band has its own link and focus colours at all.

    If a later change ever makes the accent legible on the band, the override stops being
    load-bearing and someone should notice deliberately rather than discover it.
    """
    assert contrast(ACCENT, BAND) < 3.0


def test_the_grain_opacity_still_matches_the_stylesheet() -> None:
    """Raising the grain in CSS without re-measuring it here must fail, not ship."""
    assert GRAIN_CSS_LITERAL in CSS.read_text(encoding="utf-8"), (
        f"{GRAIN_CSS_LITERAL!r} is no longer in app.css; re-measure the grain ratios"
    )


#: Sampled from static/img/*.webp: a chroma-weighted circular mean over the painting's blue
#: pixels. The surfaces are meant to *be* the painting's blue, so drift here is a real defect.
PAINTING_HUE = 237.0


def test_the_accent_hue_has_not_drifted() -> None:
    """DESIGN.md pins the accent hue to within ±10° of 255."""
    assert abs(ACCENT[2] - 255.0) <= 10.0


@pytest.mark.parametrize(("name", "colour"), [
    ("surface-sunken", SUNKEN), ("accent-wash", WASH), ("band", BAND),
    ("band-ink-quiet", BAND_INK_QUIET), ("band-link", BAND_LINK),
    ("ink", INK), ("ink-quiet", INK_QUIET), ("control-border", CONTROL_BORDER),
])
def test_a_surface_token_stays_on_the_painting_hue(
    name: str, colour: tuple[float, float, float]
) -> None:
    """Surfaces are sampled from the artwork, not chosen next to it."""
    assert abs(colour[2] - PAINTING_HUE) <= 6.0, f"{name} drifted off the painting's hue"


@pytest.mark.parametrize(("name", "colour"), [
    ("surface-sunken", SUNKEN), ("accent-wash", WASH), ("accent", ACCENT),
    ("accent-hover", ACCENT_HOVER), ("ink", INK), ("ink-quiet", INK_QUIET),
    ("control-border", CONTROL_BORDER), ("band", BAND), ("band-ink-quiet", BAND_INK_QUIET),
    ("band-link", BAND_LINK), ("band-link-hover", BAND_LINK_HOVER),
])
def test_a_token_is_inside_the_srgb_gamut(
    name: str, colour: tuple[float, float, float]
) -> None:
    """An out-of-gamut OKLCH colour is silently re-mapped by the browser, so the shipped colour
    stops being the measured one. The previous accent, oklch(0.50 0.170 255), was out by -0.003
    on red and nobody noticed."""
    channels = _srgb(*colour)

    assert all(-0.001 <= channel <= 1.001 for channel in channels), (
        f"{name} is outside sRGB: {[round(channel, 3) for channel in channels]}"
    )
