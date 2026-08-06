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
print. Dark would be the reflex ("tools look cool dark"), and for a civic data tool that has
already refused government blue it is the *second*-order reflex too.

The register is product: the tool should disappear into the task. Familiarity is a feature here.
Standard affordances, one component vocabulary, nothing invented for flavour. What earns
distinctiveness is not the chrome but the honesty — a refusal screen designed with the same care
as an answer, and provenance sitting inside the answer rather than beneath it.

This system explicitly rejects the two shapes named in PRODUCT.md: **a ChatGPT clone** and
**a BI dashboard**. It also rejects the shape its own content invites — the SaaS hero metric,
a big number over a small label with supporting stat tiles. The number here is never allowed to
appear without what licenses it.

**Key Characteristics:**

- Light, near-white, with all warmth carried by a single amber accent — never by the surface
- One accent, used only on things you can act on or on the system's current choice
- Freshness and coverage stated in words, never encoded in hue alone
- Flat at rest; depth appears only as a response to interaction
- Figures set in tabular numerals so a column of numbers can be scanned, not decoded
- Greek and English are equal citizens in the type system, not a primary and a fallback

## 2. Colors

A near-white instrument surface with one warm accent, sparingly used. All character lives in the
accent and the type; the background does no expressive work at all.

### Primary

- **Honey Amber** (`oklch(0.700 0.130 60)` anchor; ramp *[to be resolved during implementation]*):
  the single accent. Primary actions, the current selection, focus rings, and the active state of
  the ask control. Chosen because the reflex for a Greek civic data tool is government blue, and
  the reflex one tier deeper is terminal green — amber is neither, and it reads as a marked-up
  page rather than a screen. Its hue must not drift more than ±10° from the anchor.

### Neutral

- **True White** (`oklch(1 0 0)`): the body surface. Literally `#ffffff`, chroma zero. Not a warm
  near-white, not cream, not sand, not paper. The accent is warm; putting warmth in the surface
  as well is the tell. Stripe and Notion are warm brands on pure white for exactly this reason.
- **Ink** (chroma 0, dark end *[to be resolved]*): body text and figures. Must clear 4.5:1 against
  True White with room to spare — if a value is borderline, it moves toward ink, never toward
  elegance.
- **Rule Gray** (chroma 0, mid-light *[to be resolved]*): hairline dividers, table rules, input
  strokes, and the footer's separation from the answer above it.
- **Quiet Ink** (chroma 0, mid-dark *[to be resolved]*): secondary text — the footer's metadata,
  timestamps, the basis line under a figure. **Still 4.5:1.** Secondary means smaller and calmer,
  not lighter than legible. Placeholder text is held to the same 4.5:1 as body text.

Neutrals sit at chroma 0 deliberately. The usual move is to tint neutrals toward the brand hue;
here that would push a warm surface under a warm accent and land squarely in the cream band.

### Named Rules

**The Accent Is A Verb Rule.** Amber marks what you can act on and what the system has currently
chosen — nothing else. It is never decoration, never a section marker, and never applied to a
data value. If amber appears on something the user cannot click and the system did not decide,
it is wrong. Target: ≤10% of any screen.

**The Staleness Is Words Rule.** Freshness is never carried by hue. `footer.py` already writes the
sentence — *"not updated for 4 years — possibly abandoned"* — and that sentence is the design.
A four-step indicator may sit beside it in neutral ink, but colour never carries the meaning
alone. This is a colorblind-safety requirement and an honesty one: a yellow dot is deniable,
a sentence is not.

**The Chart Palette Is Not The Brand Rule.** Categorical series colours come from a dedicated,
colorblind-safe categorical scheme resolved at implementation — never from the amber accent. A
series is not an action, and the moment a data series is amber, the accent stops meaning
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

*Omitted: no components exist yet. The next `/impeccable document` pass, run against real
templates, will capture them.* The one to design first is the provenance footer — it is the
signature component of this product, it appears on every non-refusal answer, and the `Answer`
dataclass makes it structurally impossible to omit.

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
- **Don't** let amber touch a data value, a chart series, or a section marker.
- **Don't** encode freshness, completeness or confidence in colour alone.
- **Don't** use `border-left` or `border-right` above 1px as a coloured accent stripe on a card,
  callout or list item.
- **Don't** use gradient text, decorative glassmorphism, or a tiny uppercase tracked eyebrow above
  each section.
- **Don't** put a warm tint in the body surface. The accent is the warmth; the surface is
  `#ffffff`.
- **Don't** imply precision the data does not have — no smoothed chart lines across a gap, no
  interpolation between reported points, no rounding that hides a lower bound.
