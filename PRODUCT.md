# Product

## Register

product

## Platform

web

## Users

Journalists and researchers, usually on deadline, who need a defensible figure about Greece
with a citation they can quote. They arrive with a question, not a dataset id, and they will
push back on any number they cannot verify — so the source, its publisher and its
`last_updated` are the deliverable as much as the figure is.

The tool is the primary surface, but its landing state also has to hold a first-time visitor
who has never heard of data.gov.gr and does not yet know what is answerable. That is a
requirement on the empty state, not a second audience to design around.

## Product Purpose

Make Greece's ~21,900 open datasets usable by anyone who can type a question. The portal's
weakness is discoverability, not data volume: the datasets exist, but finding the right one is
the hard part. Pythia maps a natural-language question in Greek or English to the correct
dataset, fetches the values, and returns a grounded answer with a chart and a provenance
footer — or says plainly that no dataset covers the question.

Success is a journalist getting a quotable, sourced figure faster than they could by browsing
the portal, and never getting a figure they later discover was wrong.

## Positioning

The only way to query Greek open data that will tell you when it can't answer.

## Brand Personality

Precise, sober, verifiable. It should read like an instrument rather than an assistant: no
decoration competing with the data, no confidence the evidence hasn't earned. The voice is
plain and factual in both languages. Chrome — buttons, labels, empty states — is in English;
answers follow the language the question was asked in.

## Anti-references

**A ChatGPT clone.** No message bubbles, no assistant avatar, no typing dots, no endless
scrollback. This returns one cited result per question, and bubble UI promises a conversational
back-and-forth the product does not have.

**A BI dashboard.** No KPI tiles, no gauge charts, no grid of widgets. That vocabulary implies
monitoring and completeness, which is precisely wrong for a tool that answers from a single
dataset at a time and frequently declines.

## Design Principles

**Provenance is part of the answer.** The footer naming dataset, publisher, freshness and row
coverage sits inside the answer, never below it as fine print. An answer that has scrolled away
from its source has failed.

**A refusal is a route, not a dead end.** Roughly half of all questions correctly find nothing.
Those screens must show what was searched, name the near-miss datasets, and offer a way to try
again — a refusal that leaves the user with no next move is a design failure even when the
underlying verdict is right.

**Show the seams.** Which dataset was chosen, which resource, how stale, how many rows were
actually used. The user must be able to audit the machine's choice rather than trust it.

**Earned familiarity.** Standard affordances, one component vocabulary, nothing invented for
flavour. The tool should disappear into the task; a journalist on deadline has no attention to
spend on learning an interface.

**Never imply more precision than the data has.** Partial answers, truncated ranges and
provisional figures are labelled as such in the interface, not smoothed over.

## Accessibility & Inclusion

WCAG 2.2 AA: contrast ratios met for body and large text, full keyboard reachability, visible
focus states, labelled controls, and a `prefers-reduced-motion` alternative for every
animation. Greek and English content both render in the same type system, so the font stack
must carry full Greek coverage including accented capitals.
