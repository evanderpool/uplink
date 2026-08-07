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
- `uplink/webapp.py` — the local web UI: stdlib HTTP server, JSON search
  API, zero frameworks; writes (upload, feedback) exist only on a loopback
  bind.
- `uplink/feedback.py` — the query log, thumbs feedback, and the
  feedback→fixture promotion loop.
- `uplink/export.py` — JSONL export of documents+chunks (the migration path
  to a future vector store).
- `uplink/asks.py` — the ask queue: file-pair request/response handoff to
  whatever LLM session is acting as the brain.
- `uplink/notes.py` — saved notes (append-only JSONL with tombstones) and
  the shared torn-tail-safe line writer every log uses.
- `uplink/suggest.py` — opening questions derived deterministically from
  the index, so the empty state can never propose a topic the corpus
  does not contain.
- `uplink/static/` — the workspace UI: index.html, app.css, app.js, and
  one vendored animation library (see NOTICE.md). No build step.

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
python -m uplink index  "C:\path\to\your\docs" --collection ops   # build/refresh
python -m uplink search "when do backups run" --k 5               # human output
python -m uplink search "when do backups run" --json              # for LLM consumption
python -m uplink search "q1 budget" --collection finance          # scope to one collection
python -m uplink eval   fixtures/golden.jsonl                     # measure retrieval
python -m uplink eval   fixtures/golden.jsonl --log --label "baseline"
python -m uplink report all --fixtures fixtures/golden.jsonl --out reports
python -m uplink serve                                            # web UI at localhost:8180
python -m uplink status                                           # index statistics
python -m uplink export --collection ops --out ops.jsonl          # docs+chunks as JSONL
python -m uplink promote                                          # feedback -> fixtures
python -m uplink asks                                             # questions waiting for the brain
python -m uplink forget --collection ops --yes                    # drop a collection from the index
python -m uplink forget --all --yes                               # clear the index (files untouched)
python -m uplink upgrade --db old.db                              # v0.1 -> collections schema
```

## Collections

Since v0.2 every document belongs to a named **collection** — a department or
industry inside one organization (`ops`, `finance`, `health`, …). Collections
share one database and one search surface (filter with `--collection` or the
web UI's picker); **separate clients get separate database files**, keeping
the client privacy boundary a filesystem boundary rather than a WHERE clause.
Each collection is bound to one source folder; indexing a different folder
into it is refused rather than silently purging its documents.

Public-domain test corpora can be fetched with
`python scripts/fetch_corpora.py` and indexed as `finance` / `health` /
`tech` collections. Each industry mixes narrative documents with tabular
data, so retrieval is exercised across both extraction paths:

| Collection | Narrative | Tabular |
|---|---|---|
| finance | SEC 10-Ks (Apple, Microsoft, Tesla) | BLS employment tables (.xlsx) |
| health | CDC infection-control guidelines | BLS injury/illness rates (.xlsx), CDC provisional deaths (.csv) |
| tech | NIST security publications | CISA known-exploited vulnerabilities (.csv) |

`fixtures/industry-golden.jsonl` scores retrieval over all of them.
SEC and BLS require a contact address in the User-Agent — pass
`--contact you@example.com` (and `--org`), or those files are skipped
rather than fetched under a fake identity.

## The workspace

`python -m uplink serve` opens a three-panel workspace at `localhost:8180`:

- **Sources** — every document in the collection with its chunk count. The
  checkboxes are not a display filter: deselecting a source removes it from
  retrieval, and deselecting all of them honestly returns nothing.
- **Conversation** — a thread of questions with their results. Search
  returns cited passages instantly; **Ask AI** returns a written answer.
  The empty state offers opening questions derived from the index itself,
  so a new corpus is never a blank box.
- **Studio** — the metrics surface plus saved notes: retrieval accuracy
  (hit@1, hit@k, MRR with a trend line, straight from the logged eval
  history), performance (median and p95 latency, zero-hit rate), the human
  feedback loop (votes in, fixtures out, and how many upvotes are still
  awaiting promotion), and corpus health. Nothing is estimated — if a number
  has not been measured it says so instead of showing a figure.

Click any citation, source name, or result path to open the **source
reader**, which shows the document two ways:

- **Original file** — the real PDF/text as stored on disk, rendered in place.
  A PDF citation opens at the cited page.
- **Indexed text** — exactly what retrieval read, centred on the cited chunk
  and pageable through the document.

Serving originals is the one place Uplink reads a corpus file at request
time, so the path never comes from the client: the (collection, path) pair
is looked up in the index, joined to that collection's own recorded corpus
root, resolved, and then required to still be inside it. A document that is
not indexed cannot be requested at all.

The conversation persists across reloads (stored in the browser, never
sent anywhere) with a Clear control, and sources are listed by readable
title with their filename, type, size, and passage count underneath.

The interface is served from `uplink/static/` — plain HTML, CSS, and
JavaScript with no build step and no framework. One library is vendored
(GSAP, for motion; see `uplink/static/NOTICE.md`) rather than loaded from a
CDN, because a CDN request would put a network call into a product whose
guarantee is that nothing leaves the machine. Motion degrades to none if it
fails to load, and respects `prefers-reduced-motion`.

**The localhost-only write rule:** upload and thumbs-feedback endpoints exist
only while the server is bound to a loopback address (the default). Bound to
anything wider — `--host <tailscale-ip>` to reach it from your phone — every
write returns 403 and the controls never render: the remote surface is
ask-only by construction, not by convention. Search connections stay SQLite
`mode=ro` either way; the only database writes go through the same indexer
the CLI uses.

Browser-borne attacks are closed off separately: the Host header must name
the server or be an IP literal (defeats DNS rebinding), and write POSTs
require the custom `X-Uplink` header plus a loopback Origin (defeats CSRF).
Uploads are constrained by extension whitelist, size cap, sanitized bare
filenames, and land only in `data/uploads/<collection>/` — never in a
folder-bound collection's source folder.

Thumbs feedback accumulates in `data/feedback.jsonl`; `python -m uplink
promote` turns thumbs-up votes (last vote wins) into golden-question
fixtures, so the eval suite grows from real usage. Every web search is
logged to `data/query-log.jsonl` with hit count and latency.

## Ask AI — generated answers without an LLM in the box

Search returns chunks; **Ask AI** returns an answer. Uplink still contains no
language model — it borrows one. The button queues your question into
`data/asks/`, an LLM session (Claude Code in the reference deployment) drains
the queue, reads the retrieved chunks, and writes back a composed answer with
citations; the page polls and renders it. `AGENT.md` is the contract that
session follows, and `scripts/watch_asks.py` is the watcher that wakes it.

```
python scripts/watch_asks.py     # arm the brain session (background)
python -m uplink asks --json     # what is waiting to be answered
```

**Answers are grounded mechanically, not on trust.** Before an answer is
published, every citation is checked against the index and against the
sources you selected: a citation naming a document that is not indexed, or
one you deselected, is REFUSED and the answer is never written. The source
checkboxes travel with the question, so a scoped question is answered only
from the documents you chose — and the answer card shows what it stood on
("3 passages from 1 document · within your selection").

This keeps the privacy story exact: **documents never leave the machine, and
no third-party API is involved.** The trade-off is availability — answers
arrive only while a brain session is armed, which is why the button reports
"waiting for the brain session" and gives up after three minutes.

Questions are untrusted data everywhere they surface: they reach the session
quoted as JSON string literals so a question can never forge a watcher
instruction line, and answers render through `textContent` only. The queue
sits behind the same localhost-only write gate as uploads, capped at 25
pending, and the watcher will not re-fire an unanswered question for 15
minutes.

## Verifiable citations

Every citation is a button, and every search result's path is a link: click
one and the source panel opens **the indexed text itself**, centred on the
cited chunk and pageable through the rest of the document. A citation you
cannot open is a claim, not evidence.

What you see is what retrieval saw — `GET /api/doc` reads chunks from the
index, never the filesystem, so there is no file-read surface to traverse and
nothing outside the corpus is reachable. A path that exists in more than one
collection returns 409 with the candidates rather than guessing, because
guessing would present unrelated text as the source of a claim. Citations
carry `path`, `section`, `seq`, and `collection` straight from the search
JSON so they anchor exactly; `AGENT.md` makes that a requirement of the
answering contract.

Note the trade-off: document reads are GETs, so they work wherever the page
does — the localhost-only rule governs writes, not confidentiality. Search
and document reads are both appended to `data/query-log.jsonl`.

The index lives at `data/uplink.db` by default (`--db` to override) and is
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

On the public-domain industry corpora (15 documents, 6,062 chunks across
PDF, HTML-derived text, Excel and CSV; `fixtures/industry-golden.jsonl`,
19 questions): **hit@1 95% / hit@5 95% / MRR 0.947**, one
vocabulary-mismatch miss. Adding 1,401 spreadsheet and CSV passages left
the original 13 questions unchanged and all six new tabular questions
answer at rank 1. Reproducible with `scripts/fetch_corpora.py` + three
`index --collection` commands.

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
- **Phone access:** expose the Ask page over Tailscale (ask-only by
  construction) and add HMAC-signed ask requests, mirroring the parent
  system's bridge — gated on the systems-integration review.

## Design decisions

| Decision | Why |
|---|---|
| BM25 first, vectors second | On a few hundred structured docs, lexical search is ~90% of the value; vectors must *measure* their improvement, not assert it |
| SQLite over a vector DB service | Zero services to run; FTS5 is stdlib; the schema is inspectable SQL |
| CLI, not a daemon | No port, no lifecycle, no auth surface — the LLM session invokes a process |
| Read-only search connections | "The query path can't write" is enforced by SQLite, not by convention |
| Localhost-only writes | Whether writes exist is decided by the bind address at startup, not by request-time checks an attacker might route around |
| Collections in one DB, clients in separate DBs | Departments share a search surface; client isolation is a filesystem boundary, not a WHERE clause |
| Generation by borrowed brain, not an embedded API key | Keeps "documents never leave the machine" literally true and adds no per-question cost; availability is the accepted trade-off |
| Eval fixtures in-repo | The harness runs against any corpus with one command; the numbers above come from a corpus in a separate (public) repo, so treat them as our measurement, reproducible with that repo cloned |

## Tests

```
python -m pytest tests -q
```

The suite (295 tests) covers every extractor (including a byte-level
generated PDF — no PDF library needed to test), chunker no-loss properties,
incremental indexing, deletion purging, read-only enforcement (including
hostile `#`/`%` database paths), unicode queries and piped-console output on
Windows, cross-corpus purge protection, FTS5 query-injection neutralization,
the eval harness, the report layer (byte-determinism, HTML escaping of
corpus content and narrative, chart gap/label semantics, empty-index
safety), the v1→v2 migration (id preservation, idempotence), collection
scoping and purge isolation, the localhost-only write rule, CSRF and
DNS-rebinding defenses, upload constraints (traversal, overwrite, size,
byte-exactness, cleanup on failure), the feedback→promote loop, and the ask
queue (id validation, malformed-response handling, cap-under-concurrency,
watcher dedup, and the answer-card render contract executed as real
JavaScript in a DOM shim), and the source viewer (index-only reads,
ambiguous-path refusal, gapless paging across a whole document, int64
overflow, audit logging), and the workspace (per-source scoping by
composite (collection, path) identity, the empty-selection guarantee
asserted over HTTP rather than one layer below it, static-asset
whitelisting, torn-tail-safe logs, and the render contracts executed as
real JavaScript) — every finding from the adversarial reviews is pinned
by a regression test.

## License

MIT
