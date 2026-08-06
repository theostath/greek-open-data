# ADR 0008: Server-rendered interface (FastAPI + Jinja2 + HTMX, one process)

## Status

Accepted · 2026-08-06

This ratifies a reversal of an abandoned `plan.md` TODO rather than proposing something new,
matching how 0005/0006/0007 were accepted on their implementing commit. Note it supersedes a
**`plan.md` direction change, not a numbered ADR** — none of 0001–0007 ever covered the
frontend.

## Context

`plan.md` recorded a direction change on 2026-06-02: *"Frontend: Vercel app (replaces
server-rendered Jinja2 + HTMX)"*, with an explicit unresolved blocker — *"Resolve the hosting
tension first."* That tension was never resolved, and by Phase 6 the shape of the system had
answered it:

- The planning and synthesis LLM is **Qwen on `localhost:11434`** (ADR-0004). No API key, no
  egress.
- Retrieval needs a **~2.2 GB e5-large model** in process, plus a persistent Chroma index.
- Two **local SQLite databases** hold the catalogue (21,806 datasets / 106,678 resources) and
  the access cache.

A cloud-hosted page cannot reach any of that without either exposing the laptop to the
internet or re-hosting the whole stack — which contradicts Core Principle #3, local-first and
reproducible, and would require paying for GPU inference the project deliberately avoids.

Two further forces:

- Phases 0–6 produce a complete pipeline reachable only through
  `make answer QUESTION="..."`. That hides the product's most common outcome: on the golden
  set only 6/26 questions answer, 6 find the right dataset with no tabular resource and 12
  find nothing. A CLI makes each of those look like a failed command.
- The honesty contract lives in Python dataclasses. `Answer` cannot be constructed without
  provenance, but a template can happily omit the footer. Keeping the view whitelist in the
  **same process** as the dataclass invariants is what lets one test suite cover both.

## Decision

**One FastAPI process serves the HTTP routes and the server-rendered Jinja2 templates,
progressively enhanced with HTMX.** No SPA, no build step, no second runtime, no CORS, no
client state. `make dev` starts it on `127.0.0.1:8000`.

Supporting decisions made under this one:

- **`Pipeline` (`api/service.py`) is the single orchestration path**, shared by the CLI and
  the web app. `answer.py::_run` was lifted into it and the CLI refactored to delegate.
- **`RecoveryContext` is resolved by the caller**, not inside `synthesis/`, because
  `synthesis/` does no I/O — the same reason `RefusalContext` exists.
- **`api/view.py` is a publish whitelist**, naming every field a template may read, so a field
  added to `QueryPlan` later is invisible by default.
- **Questions run on a bounded pool** and the browser polls; the terminal fragment stops the
  polling by omitting `hx-trigger`.
- **Client assets are vendored and hash-pinned**, because a CDN would break the local-first
  guarantee.

## Rationale

- **Local-first is the constraint that decides it.** Everything the answer depends on is on
  the laptop; serving the page from the same process is the only option that keeps it there.
- **No Node toolchain in a Python repo.** An SPA would add a second package manager, a build
  step and a deploy story to a project whose stated bar is "runs fully on a laptop."
- **The honesty contract is easier to hold in one process.** The `AnswerView` whitelist, the
  template that raises rather than render a footerless answer, and the dataclass invariants
  are all covered by one offline `pytest` run.
- **HTMX earns its place on two interactions only** — submitting a question and polling for
  progress. Both are server-state problems, which is what HTMX is good at.

## Consequences

- **No public deployment without a further decision.** This ADR does not authorise one;
  hosting would require re-deciding the LLM and embedding story first.
- **~853 KB of vendored JavaScript** (htmx 2.0.4, vega 5.30.0, vega-lite 5.21.0,
  vega-embed 6.26.0) is committed to the repo. Their SHA-256 digests are asserted in
  `tests/test_api_assets.py`, so a silent swap fails `make check` rather than relying on
  review. Digests are listed below.
- **Browser-reachability triggers the "revisit if ever publicly hosted" caveat** in
  `access/guard.py:67`. The response is in this phase: an **Origin check** on every mutating
  route (a loopback bind is not an access control — CORS stops the *reading* of a
  cross-origin form POST, not the request), plus **CSP** with `script-src 'self'` and
  `frame-ancestors 'none'`, `X-Content-Type-Options: nosniff` and `Referrer-Policy:
  no-referrer` on every response. Every asset is local, so a strict CSP costs nothing.
- **Results are in-memory and ephemeral.** A bounded, TTL-evicting store; `make dev` runs
  `--reload`, so a file save wipes them. Each job id carries a per-process epoch so a lost
  result can say "the server restarted" rather than the untrue "your result expired."
- **Single-user by design.** Two concurrent inference slots, four pending. Multi-user
  concerns, a JSON API, streaming narration and durable results are deliberately deferred.

## Amendment to ADR-0007 (chart palette)

`chart.py` set `encoding.color` with no `scheme`, so specs fell back to Vega-Lite's
`tableau10`, which is not colourblind-safe and whose orange collides with the amber UI accent
— and the accent means "actionable", so an amber data series quietly steals its only job.
Series colours are now an explicit **Okabe-Ito** range, reordered so its orange (`#e69f00`)
and yellow (`#f0e442`) fall past the point realistic series counts reach.

**Correction to the Phase 7 spec:** the spec stated `scale` was already on the `validate_spec`
allowlist and so no guard change was needed. That was wrong — the guard filters *every key at
every depth*, and the child key `range` was rejected. `range` is now allowlisted; Vega-Lite
accepts only scalars or arrays of scalars there, and the leaf-scalar check still applies, so
the executable surface is unchanged.

## Vendored asset digests (SHA-256)

| File | Version | SHA-256 |
|---|---|---|
| `htmx.min.js` | 2.0.4 | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `vega.min.js` | 5.30.0 | `e432c751a6363f4a61da62920cc7d7ebd13cf09d82949f8f486248f8071dc3ce` |
| `vega-lite.min.js` | 5.21.0 | `cd32314b1e76e7d879dc9f0534b62be714df03554486c7ca2381abfd0a92d2f4` |
| `vega-embed.min.js` | 6.26.0 | `072c054f2a6310725e038c38a71e00052705e31835632462c9717a23a384e895` |
