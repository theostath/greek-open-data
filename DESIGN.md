<!-- SEED: re-run /impeccable document once there's code to capture the actual tokens and components. -->
---
name: Pythia
description: A reference desk for Greek open data — ask a question, get a cited figure or an honest no.
---

# Design System: Pythia

## 1. Overview

**Creative North Star: "The Reference Desk"**

A good reference librarian does three things: takes a question in the words you have, comes
back with the source rather than an assertion, and tells you plainly when the collection does
not hold the answer — then points at the three nearest things that might. That is the whole
interface. Not a chat partner, not a monitoring surface: a desk where a question goes in and a
citation comes out.

The physical scene decides the theme. A journalist at 16:40, on deadline, in daylight, with the
CMS in one tab and this in another, copying a figure and its source into a story that files at
18:00. That is a **light** interface — daylight ambient, adjacent to a text editor, destined for
print. Dark would be the reflex ("tools look cool dark").

The register is product: the tool should disappear into the task. Familiarity is a feature here.
Standard affordances, one component vocabulary, nothing invented for flavour. What earns
distinctiveness is not the chrome but the honesty — a refusal screen designed with the same care
as an answer, and provenance sitting inside the answer rather than beneath it.

This system explicitly rejects the two shapes named in PRODUCT.md: **a ChatGPT clone** and
**a BI dashboard**. It also rejects the shape its own content invites — the SaaS hero metric,
a big number over a small label with supporting stat tiles. The number here is never allowed to
appear without what licenses it.

**Key Characteristics:**

- A white reading field framed by chrome in the painting's own dark blue
- One accent, used only on things you can act on or on the system's current choice; inside the
  dark chrome it inverts to a light blue, because the accent is invisible there
- Freshness and coverage stated in words, never encoded in hue alone
- Flat at rest; depth appears only as a response to interaction
- Figures set in tabular numerals so a column of numbers can be scanned, not decoded
- Greek and English are equal citizens in the type system, not a primary and a fallback

## 2. Colors

A white reading field framed by chrome in the painting's own dark blue, with one accent on
everything actionable. The colour strategy is **Committed**, not Restrained. What the strategy
does *not* touch is the evidence — no band sits behind a figure, no hue reports a value.

> **Direction change, 2026-08-10 — user decision.** This section previously specified Honey
> Amber (`oklch(0.660 0.130 60)`), chosen precisely *because* government blue is the first
> reflex for a Greek civic data tool. The owner directed a white-and-blue palette on the
> grounds that the product is a platform for exploring Greek open data, and that call
> supersedes the amber rationale. It is recorded here rather than quietly overwritten, because
> the reflex argument was correct on its own terms and anyone revisiting the palette should
> know it was overruled deliberately, not forgotten. The accessibility bars did not move, and
> the blue clears them by wider margins than the amber did.
>
> **Second pass, same day.** Swapping the accent token alone left the page still reading white
> and grey, so the owner repeated the instruction and the strategy moved from Restrained to
> **Committed**: chrome bands, blue link text, blue-tinted example controls. The honesty rules
> were kept intact rather than traded away — see the Named Rules below, which survive the
> change because bands use a *surface* token and links are genuinely actionable.
>
> **Third pass, same day.** Once the painting shipped, the pale band read as a generic blue
> sitting *next to* the artwork rather than drawn from it. Every surface token moved onto the
> painting's own hue, measured rather than eyeballed.
>
> **Fourth pass, same day — the chrome went dark.** Percentile sampling of the source settled
> the question the earlier passes had been dancing around: the painting's 707,810 blue pixels
> run **L 0.31 → 0.62**, median `oklch(0.429 0.053 237)`. *It contains no light blues at all.*
> A pale tint therefore cannot match its intensity — it can only sit beside it, which is exactly
> what the owner kept seeing. So the masthead and colophon took the sampled median directly and
> flipped to light text. The suggestion buttons deliberately did **not** follow them down: three
> dark blocks mid-column would have fought the Ask button for primacy, so they took the most
> saturated *light* tint their own contrast duty allows instead.

### Primary

- **Signal Blue** — `oklch(0.48 0.155 255)`. The single accent: primary actions, the current
  selection, focus rings, and the active state of the ask control. Its hue must not drift more
  than ±10° from 255; `tests/test_api_contrast.py` asserts that.

  It measures **5.87:1** against white, against the **3:1** WCAG 2.2 1.4.11 asks of a focus
  indicator and of a control's own visible boundary. (The superseded amber needed its lightness
  resolved down to L=0.660 just to reach 3.10:1; blue at this lightness has real headroom.)
  Because the fill is dark, the button label is **white** — `--accent-ink: oklch(1 0 0)`, which
  measures 6.63:1 on the fill and 8.94:1 on `--accent-hover: oklch(0.41 0.135 255)`.

  **The accent alone stays at hue 255 while every surface moved to 242**, and that is
  deliberate. Chroma barely affects luminance, so the contrast budget did not force it — the
  sRGB gamut did. Toward 242 the gamut narrows sharply: max chroma at L=0.48 falls from 0.160
  at hue 255 to 0.115 at hue 242. Matching the painting exactly would have bought a hue shift
  nobody can perceive on a control that small, at the cost of a visibly duller button.

  A related defect was found and fixed in the same pass: the previous `oklch(0.50 0.170 255)`
  was **outside sRGB** (red channel −0.003) and had been silently gamut-mapped by the browser,
  so the shipped colour was never quite the measured one. `test_a_token_is_inside_the_srgb_gamut`
  now checks every colour token.

  A form control's boundary uses a separate tinted neutral,
  `--control-border: oklch(0.56 0.028 242)` (3.77:1 on white, 3.21:1 on a grained band) —
  `--rule-strong` at 2.21:1 is fine for a decorative divider and not for a control. It sits at
  0.56 rather than 0.62 because it owes 3:1 against the chrome band too, where 0.58 measured
  2.96:1.

### Neutral

- **True White** (`oklch(1 0 0)`): the reading field. Literally `#ffffff`, chroma zero. Figures,
  charts and citations all sit here; the colour is in the frame, never behind the evidence.
- **Band Blue** (`oklch(0.43 0.055 237)`): the masthead and colophon, taken straight from the
  painting's median blue. It carries **light** text — white at 8.02:1, quiet text at 5.27:1.
- **Panel Blue** (`oklch(0.89 0.057 237)`): the provenance footer and the skeleton blocks. Light,
  because a citation is read closely and at length.
- **Wash Blue** (`oklch(0.87 0.068 237)`): the suggestion buttons and the searched-terms
  highlight — the most chromatic light surface in the system, since it is a control.
- **Ink** (`oklch(0.24 0.016 237)`): body text and figures, 14.53:1 on grained white. If a value
  is borderline it moves toward ink, never toward elegance.
- **Rule** (`oklch(0.86 0.030 237)`) and **Rule Strong** (`oklch(0.75 0.040 237)`): hairline
  dividers and table rules on the white field. `--band-rule` (`oklch(0.56 0.045 237)`) does the
  same job inside the dark chrome.
- **Quiet Ink** (`oklch(0.45 0.020 237)`): secondary text on white — 6.55:1 grained. Secondary
  means smaller and calmer, not lighter than legible. Placeholder text is held to the same bar.

**Both light surfaces are capped by the same thing, and it is worth knowing which:** a blue link
sitting on them still owes 4.5:1. The wash stops at L=0.87 (link 4.52:1) and the panel at L=0.89
(4.81:1). Neither can go more saturated without either lightening the link or giving up links on
those surfaces.

### Named Rules

**The Accent Is A Verb Rule.** Blue marks what you can act on and what the system has currently
chosen — nothing else. It is never decoration, never a section marker, and never applied to a
data value. If blue appears on something the user cannot click and the system did not decide,
it is wrong.

The Committed strategy does not weaken this; it applies it more literally. **Link text is blue**
because a link is the most common actionable thing on the page. The two chrome bands are *not* an
exception: they use `--band`, a surface token, not the accent. The old "≤10% of any screen" target
no longer applies to the accent as a text colour; the substantive constraint is the sentence above
it, not the percentage.

**Inside the dark chrome the accent inverts.** `--accent` measures **1.1:1** against `--band` —
literally invisible — so within `.masthead` and `.colophon` the verb is carried by `--band-link`
(5.29:1) and the focus ring by `--band-ink` (8.02:1). This is the same rule, not an exemption
from it: the job "mark what is actionable" is unchanged, only the colour that can do it on that
surface. `test_the_accent_is_unusable_on_the_band` pins the 1.1:1 measurement, so if a future
palette change ever makes the accent legible there, the override stops being load-bearing and
somebody has to notice on purpose.

The wordmark is the one thing that stays a plain ink — now `--band-ink`, white. At display size a
verb colour would stop reading as "you can act on this" and start reading as a logo colour, which
is exactly the failure this rule exists to prevent.

**The Canvas Grain Rule.** The page carries a texture: desaturated fractal noise at **0.055**
opacity, authored inline as an SVG data URI in `--canvas` and painted on the body and the two
chrome bands. It exists because a 68ch column floating in flat white read as empty, and it is
the only decoration in the system. Three constraints make it affordable:

- **It is generated, not sourced.** No file, no request, no licence to inherit — which matters
  because §1 of CLAUDE.md only permits Apache-2.0-compatible vendored assets, and stock
  photography of Greece almost never is.
- **Its opacity is measured, not chosen.** `tests/test_api_contrast.py` composites the grain's
  darkest possible noise onto each surface and re-measures every text pair against that worst
  case. Raising 0.055 fails `make check`. This caught a real regression: link text on a grained
  band measured 4.35:1, which is why the accent sits at L=0.50 rather than L=0.52.
- **It comes off where the surface stops being a screen.** `@media print` and
  `(forced-colors: active)` both drop it. The answer is destined for print, and paper gets the
  clean plane — as does the provenance panel, which stays deliberately flat so the citation
  sits on an untextured surface.

**The Painted Field Rule.** Three crops of an owner-supplied oil painting — a night city in
blues and lamp-yellows — carry the page's material: `.masthead-art` runs full bleed across the
top of every page (`masthead.webp`, 1000×330), and `field-left.webp` / `field-right.webp`
(250×1000 each) continue the canvas down the gutters either side of the column. 200 KB for the
three. Four constraints keep them from becoming a liability:

- **No text is ever set over it.** Measured: the painting's brightest lamp specks reach
  Y=0.965, so white text over it would need a **0.55** black scrim to clear 4.5:1 — and that
  scrim flattens the impasto the band exists to show. Text-free, it owes no ratio and gets no
  scrim, so the paint runs at full strength. If anyone later puts a wordmark on it, the scrim
  comes back and this number is the reason.
- **The figure is excluded by the crop, not painted over.** The source has a man at centre-
  bottom, roughly x 455–620 from y=505 down. The band is `(0, 15) → (1000, 345)`, stopping 160px
  above him; the gutter strips are `x 0–250` and `x 750–1000`, stopping 200px either side. No
  crop contains any part of the figure, so none of this depends on retouching holding up.
- **The gutter strips can never reach the text.** Their width is
  `clamp(0px, (100vw - var(--measure)) / 2 - var(--space-7), 20rem)` — the space genuinely left
  over after the measure and a 3rem buffer, which also means it computes to 0 on a narrow
  viewport and the strips hide themselves with no breakpoint to maintain. Because no text ever
  crosses them, they owe no contrast ratio. Their inner edge fades to transparent **in the
  image's own alpha channel**, since a CSS background layer cannot carry a mask; that is what
  stops them ending on a hard vertical line.
- **They are pinned and capped as a set.** `tests/test_api_assets.py` asserts each SHA-256 and
  a **220 KB total** budget, alongside the htmx and ECharts bundles. The budget is on the total
  deliberately: three files under a 120 KB per-file limit would pass and still be a third of a
  megabyte.
- **They drop out where they would cost something.** `@media print`, `(forced-colors: active)`
  and `(prefers-reduced-data: reduce)` remove all three, and reduced-data also drops the grain —
  it was the field the strips exist to enrich. This is the heaviest thing the page loads and it
  is decoration; a connection that asked for less does not pay for it.

> **Licence: owner's own work.** Confirmed by the repository owner on 2026-08-10: the source
> painting is theirs and they hold the rights to use it here, which satisfies CLAUDE.md §1's
> requirement that every vendored asset be Apache-2.0-compatible. Note for anyone replacing it:
> the palette's hue is *sampled from this file*, so a different image means re-running the hue
> measurement, not just swapping the crops.

**The Staleness Is Words Rule.** Freshness is never carried by hue. `footer.py` already writes the
sentence — *"not updated for 4 years — possibly abandoned"* — and that sentence is the design.
A four-step indicator may sit beside it in neutral ink, but colour never carries the meaning
alone. This is a colorblind-safety requirement and an honesty one: a yellow dot is deniable,
a sentence is not.

**The Chart Palette Is Not The Brand Rule.** Categorical series colours come from a dedicated,
colorblind-safe categorical scheme resolved at implementation — never from the blue accent. A
series is not an action, and the moment a data series is the accent blue, the accent stops meaning
"actionable". Note for implementation: `synthesis/chart.py` currently sets `encoding.color` with
no `scheme`, so specs fall back to Vega-Lite's default tableau10. That default must be replaced
deliberately, and `scale` is already on the `validate_spec` allowlist, so doing so is permitted.

## 3. Typography

**Display Font:** *[single family, to be chosen at implementation]*
**Body Font:** same family
**Label/Mono Font:** none — see The Greek First Rule

**Character:** One humanist sans across headings, body, labels and data. The product register is
right that a well-tuned single family carries a tool better than a pairing, and here it also
avoids a concrete trap: a mono face for figures would look like the correct instrument choice
right up until a Greek dataset label renders in fallback glyphs on the one screen that has to
look trustworthy.

### Hierarchy

Fixed rem scale, not fluid — users view this at consistent DPI and a clamped heading that shrinks
inside a result panel looks worse, not better. Scale ratio 1.125–1.2. Exact steps
*[to be resolved during implementation]*.

- **Display**: the answer's figure. Prominent through size, weight and position — never through
  colour, never inside a tile, never above a row of supporting stats.
- **Headline**: the question as restated, and page-level headings. One `<h1>` per page.
- **Title**: dataset name in the footer; section headings within a refusal.
- **Body**: narration prose and refusal explanations. Capped at 65–75ch.
- **Label**: form labels, the figure's basis line, metadata keys. Sentence case, not uppercase
  tracked — the all-caps micro-label is the eyebrow trope wearing a different hat.

### Named Rules

**The Tabular Rule.** Every figure renders with `font-variant-numeric: tabular-nums`. A column of
numbers whose digits shift width cannot be scanned, only read one line at a time — and scanning
a column is the entire job.

**The Greek First Rule.** The family must carry full Greek, including accented capitals
(Ά Έ Ή Ί Ό Ύ Ώ) and the final sigma (ς). Test with real harvested dataset titles before
committing, not with lorem ipsum. A fallback glyph inside a publisher's name is indistinguishable,
to the reader, from corrupted data — and this project's oldest recurring bug class is encoding.

## 4. Elevation

Flat by default. Motion and depth are restrained to state changes only (150–250 ms, ease-out,
with a `prefers-reduced-motion` alternative for each), so the system conveys depth through hairline
rules and tonal separation rather than a shadow vocabulary. Shadows are reserved for genuinely
floating surfaces, of which this interface currently has none planned.

### Named Rules

**The Flat Until Interactive Rule.** Surfaces are flat at rest. Any shadow must be a *response* —
hover, focus, or a genuinely overlaid element. A shadow at rest is decoration, and on an
instrument, decoration reads as imprecision.

## 5. Components

**Built in Phase 7, not yet captured properly.** Twelve templates now exist under
`templates/`; this section still predates them. **Re-run `/impeccable document` against the
real templates** to capture actual tokens and components and to generate the
`.impeccable/design.json` sidecar. Until then, `static/app.css` is the source of truth.

What shipped, in brief:

- **Provenance footer** (`partials/_footer.html`) — the signature component, on every
  non-refusal answer, structurally impossible to omit because `_answer.html` *raises* rather
  than render without it. Freshness is the sentence `footer.py` writes plus a four-step
  indicator in neutral ink, both derived from the same thresholds so they cannot disagree.
- **Ask control** (`partials/_ask.html`) — persists above the result; re-asking replaces the
  result rather than appending, so there is no scrollback.
- **Three refusal branches** (`partials/_refusal.html`) — the structural rule is that a
  refusal carrying provenance renders it, and one that does not shows what was searched
  instead. That is why `matched_but_refused` resembles an answer more than the other two.
- **Progress** (`partials/_progress.html`) — skeleton content, a distinct queued state, and
  elapsed seconds, because one stage can legitimately last ~90 s.

**Known open critique:** the UI is functional but not liked (2026-08-07). A
`/impeccable critique` pass, and probably `bolder` or `typeset`, is queued work — see
REPO_REPORT.md Part 3.3. The layout decision most worth revisiting first is
**claim → provenance → chart**, which deliberately inverts the usual order so the quotable
unit never scrolls away from its citation.

## 6. Do's and Don'ts

### Do:

- **Do** keep the provenance footer inside the answer, visually bound to the figure it licenses.
  An answer that has scrolled away from its source has failed.
- **Do** design the refusal screen with the same investment as the answer screen. On the golden
  set 12/26 questions find no dataset and 6/26 find one with no tabular resource — refusal is the
  most common screen this product has, not an error path.
- **Do** state coverage, truncation and provisional figures in words, at the point of the claim.
- **Do** hold placeholder and secondary text to the same 4.5:1 as body text.
- **Do** give every interactive component all seven states — default, hover, focus, active,
  disabled, loading, error — before shipping it.
- **Do** use skeleton content while an answer computes, and name the current stage.
- **Do** test every heading at every breakpoint with real Greek dataset titles, which are long.

### Don't:

- **Don't** build **a ChatGPT clone**: no message bubbles, no assistant avatar, no typing dots,
  no endless scrollback. This returns one cited result per question.
- **Don't** build **a BI dashboard**: no KPI tiles, no gauge charts, no widget grid. That
  vocabulary implies monitoring and completeness, and this tool answers from one dataset at a time
  and frequently declines.
- **Don't** render the answer as a hero metric — big number, small label, supporting stats, accent
  flourish. The content invites it and it is the SaaS cliché.
- **Don't** let the accent blue touch a data value, a chart series, or a section marker.
- **Don't** encode freshness, completeness or confidence in colour alone.
- **Don't** use `border-left` or `border-right` above 1px as a coloured accent stripe on a card,
  callout or list item.
- **Don't** use gradient text, decorative glassmorphism, or a tiny uppercase tracked eyebrow above
  each section.
- **Don't** put a warm tint in the body surface. The accent is the warmth; the surface is
  `#ffffff`.
- **Don't** imply precision the data does not have — no smoothed chart lines across a gap, no
  interpolation between reported points, no rounding that hides a lower bound.
