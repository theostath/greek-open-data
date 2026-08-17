# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-08-17

First tagged release. The pipeline runs end to end, on a laptop, with no API keys: a question
in Greek, Greeklish or English becomes a grounded, cited answer with a chart — or an honest
refusal.

### Added

- **Ingestion** — harvests the CKAN 2.11.3 catalogue into SQLite: 21,806 datasets and 106,678
  resources, each carrying `last_updated`.
- **Retrieval** — hybrid search over a Chroma dense index (`multilingual-e5-large`) and SQLite
  FTS5/BM25, fused with RRF (ADR-0001). Greeklish→Greek transliteration (ADR-0005). An opt-in
  cross-encoder reranker, shipped **off** on measured latency (ADR-0002).
- **Planning** — `make_plan()` turns a question into a typed `QueryPlan`, choosing the dataset
  and a CSV/JSON resource deterministically. The LLM contributes a relevance verdict, nothing
  more. Local Qwen via Ollama (ADR-0004).
- **Access** — `fetch_resource()` returns a typed `TableData` via the CKAN DataStore or a file
  download, with scheme/host/IP policy, manual redirect limits, magic-byte format detection,
  Greek codec sniffing and a TTL cache. `complete` has no default and is validated against
  `incomplete_reason` (ADR-0006).
- **Synthesis** — a grounded `Answer` with facts, prose, an Apache ECharts option and a
  mandatory provenance footer (ADR-0007, ADR-0009). Every figure is computed in Python; the
  model receives opaque placeholders and never sees the table.
- **Interface** — one FastAPI process serving Jinja2 + HTMX (ADR-0008): `uv run pythia-dev`.
  Includes `/explore` for deterministic catalogue browsing and `/stats` for outcome mix.
  Origin checking, CSP, vendored and hash-asserted assets, measured WCAG contrast.
- **Apache-2.0 licence**, with the constraint that every dependency and vendored asset must
  carry a compatible grant.

### Fixed

- Caveat-carrying answers no longer lose the model's prose. The limitation is appended by the
  program rather than requested from the model, so its presence does not depend on compliance
  (#33).
- The claim guard now refuses narrations that assert the data is whole when it is not. It
  previously tested omission and never contradiction.
- A numeric dimension label — a year — can now be cited. `allowed_tokens` was built only from
  fact values, so `render_template` could print "2016: 753" while the same sentence from the
  model was rejected, silently degrading every year-dimensioned question.
- Superlatives are licensed by direction: the largest categories are known by construction,
  the smallest live in the omitted tail (#30).
- A temporal axis is never reordered by magnitude.

### Known limitations

- **Retrieval is the ceiling.** R@1 is 0.46 on a 26-question golden set; the set is too small
  for confident comparison and expanding it is the next priority (#13).
- **Multi-series tables produce no facts**, so those questions render the deterministic
  template rather than model prose.
- **Three caveat strings in `bind.py` are English-only** and can appear in a Greek answer.
- **Latency is dominated by the local model** — 25–130 s per question on CPU.
- Datasets published only as PDF or spreadsheet are out of scope; 24.4% of the catalogue has a
  CSV or JSON resource.

### Metrics at this tag

Retrieval, 26 golden questions, e5-large, reranker off, Greeklish normalization on:

| Slice | n | MRR | R@1 | R@5 | R@10 |
| --- | --- | --- | --- | --- | --- |
| overall | 26 | 0.544 | 0.46 | 0.62 | 0.69 |
| el | 12 | 0.595 | 0.50 | 0.67 | 0.83 |
| en | 7 | 0.571 | 0.43 | 0.71 | 0.71 |
| greeklish | 7 | 0.429 | 0.43 | 0.43 | 0.43 |

Corpus snapshot harvested 2026-07-29. Suite: 600 tests, green on Python 3.11 and 3.12.

[0.1.0]: https://github.com/theostath/greek-open-data/releases/tag/v0.1.0
