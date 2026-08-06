# Uplink

[![CI](https://github.com/evanderpool/uplink/actions/workflows/ci.yml/badge.svg)](https://github.com/evanderpool/uplink/actions/workflows/ci.yml)

**Local, private document retrieval — your documents never leave your machine.**

Uplink indexes a folder of mixed-format documentation (Markdown, PDF, Word,
Excel, CSV/TSV, plain text) into a single SQLite database and answers
questions over it with BM25 full-text search. It is built to be the retrieval
layer under an LLM assistant (Claude Code in the reference deployment): the
model calls Uplink as a tool, reads the top-ranked chunks, and composes an
answer with citations back to the source files.

Built and maintained by [Erick Vanderpool](https://github.com/evanderpool) as
part of the [Artificial Management](https://github.com/evanderpool/artificial-management)
AI operating system.

## Why local-first

- **Privacy is the feature.** The index is a file on disk. Nothing is
  embedded in the cloud, nothing is uploaded, and the phone-access path
  (Tailscale + a signed request queue) never exposes a public endpoint.
- **The retrieval layer is mechanical, not aspirational.** Search opens the
  database with SQLite's `mode=ro` URI — the query path is *incapable* of
  writing, by construction.
- **Retrieved text is data, not instructions.** Any LLM consuming Uplink
  results treats chunk text as untrusted corpus content — a prompt-injection
  boundary inherited from the parent system's bridge design.

## Architecture

```
 documents (md, pdf, docx, xlsx, csv, tsv, txt)
      |
      v
  extractors ──> chunker ──> SQLite (documents + chunks + FTS5)
                                  |
                     read-only    v
  phone / operator ──> LLM ──> search CLI (BM25, AND->OR fallback)
                        |
                        v
              answer with file:section citations
```

- `uplink/extractors.py` — one extractor per file type; optional libraries
  (pypdf, python-docx, openpyxl) degrade gracefully when missing.
- `uplink/chunker.py` — paragraph-boundary chunks (~1600 chars, 200 overlap);
  tabular chunks keep their header row so every chunk is self-describing.
- `uplink/indexer.py` — incremental by SHA-256: unchanged files are skipped,
  changed files re-chunked, deleted files purged.
- `uplink/search.py` — FTS5 BM25, section titles weighted 3x, stopword-
  filtered queries, all tokens quoted (FTS5 query syntax in user input is
  neutralized), AND with OR fallback.
- `uplink/evaluate.py` — golden-question harness. Retrieval changes must
  show before/after numbers here before they count as improvements.
- `uplink/report.py` — deterministic HTML reports (corpus health, retrieval
  quality, activity) computed straight from the index; an LLM narrative is
  injected into a marked, escaped block. The script computes, the model
  narrates.
- `uplink/svgchart.py` — dependency-free SVG charts (theme-aware via CSS
  custom properties; light and dark).

## Install

Python 3.11+ (3.12 recommended). Core retrieval (Markdown, TXT, CSV/TSV) is
stdlib-only. Extractors for the other formats are optional extras:

```
pip install -e .            # core only - zero dependencies
pip install -e ".[pdf]"     # + PDF (pypdf)
pip install -e ".[docx]"    # + Word (python-docx)
pip install -e ".[xlsx]"    # + Excel (openpyxl)
pip install -e ".[dev]"     # everything + pytest
```

## Use

```
python -m uplink index  "C:\path\to\your\docs"          # build/refresh index
python -m uplink search "when do backups run" --k 5     # human output
python -m uplink search "when do backups run" --json    # for LLM consumption
python -m uplink eval   fixtures/golden.jsonl           # measure retrieval
python -m uplink eval   fixtures/golden.jsonl --log --label "baseline"
python -m uplink report all --fixtures fixtures/golden.jsonl --out reports
python -m uplink status                                  # index statistics
```

The index lives at `data/index.db` by default (`--db` to override) and is
never committed — it may contain private corpus content.

## Measured retrieval quality

Corpus: the Artificial Management operating system's own documentation
(91 markdown documents, 918 chunks). Fixtures: `fixtures/golden.jsonl`,
18 real operational questions. Scoring is strict: a hit requires the
expected *file path* in the top-k — matching on section titles was rejected
in review as too lenient.

| Retrieval | hit@1 | hit@5 | MRR |
|---|---|---|---|
| BM25, raw query tokens | 44% | 67% | 0.532 |
| BM25 + query stopword filter (current) | **67%** | **89%** | **0.769** |

The two remaining misses are vocabulary-mismatch questions (the question's
words don't appear in the answering document) — the documented motivation for
phase 2.

## Reports

`python -m uplink report all` renders three self-contained HTML reports —
**Corpus Health**, **Retrieval Quality** (live eval + a quality-over-time
chart fed by `eval --log` history), and **Corpus Activity** — with inline SVG
charts, no JavaScript, and light/dark theming. Every figure is computed from
the index; an optional narrative paragraph (written by an operator or LLM via
`--narrative-file`) is injected escaped into a clearly-marked block. Reports
land in `reports/`, which is gitignored: generated reports contain corpus
content, so the capability is public but your outputs are not.

## Roadmap

- **Phase 2 — hybrid retrieval:** local embeddings (fastembed +
  `bge-small-en-v1.5`, CPU-only) + reciprocal rank fusion with BM25, gated on
  before/after eval numbers. EmbeddingGemma noted as the upgrade path.
- **Phone access:** question queue via the parent system's signed-request
  bridge pattern (read-only; answers only, no actions).

## Design decisions

| Decision | Why |
|---|---|
| BM25 first, vectors second | On a few hundred structured docs, lexical search is ~90% of the value; vectors must *measure* their improvement, not assert it |
| SQLite over a vector DB service | Zero services to run; FTS5 is stdlib; the schema is inspectable SQL |
| CLI, not a daemon | No port, no lifecycle, no auth surface — the LLM session invokes a process |
| Read-only search connections | "The query path can't write" is enforced by SQLite, not by convention |
| Eval fixtures in-repo | The harness runs against any corpus with one command; the numbers above come from a corpus in a separate (public) repo, so treat them as our measurement, reproducible with that repo cloned |

## Tests

```
python -m pytest tests -q
```

The suite covers every extractor (including a byte-level generated PDF — no
PDF library needed to test), chunker no-loss properties, incremental
indexing, deletion purging, read-only enforcement (including hostile `#`/`%`
database paths), unicode queries and piped-console output on Windows,
cross-corpus purge protection, FTS5 query-injection neutralization, the eval
harness, and the report layer (byte-determinism, HTML escaping of corpus
content and narrative, chart gap/label semantics, empty-index safety) —
every finding from the adversarial reviews is pinned by a regression test.

## License

MIT
