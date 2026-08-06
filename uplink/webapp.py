"""Uplink web UI — a local, read-only retrieval explorer.

`python -m uplink serve` starts a stdlib-only HTTP server (no frameworks, no
JavaScript dependencies) with three surfaces:

    GET /                     the Ask page (search box + cited results)
    GET /api/search?q=&k=     search results as JSON (read-only)
    GET /api/status           index statistics as JSON
    GET /reports/<kind>.html  generated reports, if present

Security posture:
- binds to 127.0.0.1 unless --host says otherwise (Tailscale exposure is a
  deliberate operator choice, never a default);
- every database connection is read-only (SQLite mode=ro) — there is no
  write endpoint and no write capability;
- corpus text reaches the page as JSON and is rendered with textContent,
  never innerHTML, so document content cannot inject markup or script.
"""

from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db
from .search import hits_to_dicts, search

MAX_QUERY_LEN = 500
MAX_K = 25


def make_handler(db_path: Path, reports_dir: Path | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Uplink"

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            url = urlparse(self.path)
            try:
                if url.path == "/":
                    self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
                elif url.path == "/api/search":
                    self._api_search(url)
                elif url.path == "/api/status":
                    self._api_status()
                elif url.path.startswith("/reports/"):
                    self._report(url.path)
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found")
            except FileNotFoundError as exc:
                self._json(404, {"error": str(exc)})
            except Exception as exc:  # never leak a traceback to the client
                self._json(500, {"error": f"{type(exc).__name__}"})
                raise

        def do_POST(self) -> None:  # noqa: N802
            # Read-only surface: no mutating methods, by construction.
            # Drain the request body first or the client sees a reset
            # instead of the 405 (Windows aborts on unread request data).
            length = int(self.headers.get("Content-Length") or 0)
            while length > 0:
                length -= len(self.rfile.read(min(length, 1 << 16)))
            self._send(405, "text/plain; charset=utf-8", b"read-only")

        do_PUT = do_DELETE = do_PATCH = do_POST

        def _api_search(self, url) -> None:
            params = parse_qs(url.query)
            query = (params.get("q") or [""])[0].strip()[:MAX_QUERY_LEN]
            try:
                k = min(MAX_K, max(1, int((params.get("k") or ["8"])[0])))
            except ValueError:
                k = 8
            if not query:
                self._json(400, {"error": "missing q parameter"})
                return
            hits = search(db_path, query, k=k)
            self._json(200, {"query": query, "k": k, "hits": hits_to_dicts(hits)})

        def _api_status(self) -> None:
            conn = db.connect_ro(db_path)
            try:
                docs = conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
                chunks = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
                latest = conn.execute(
                    "SELECT MAX(indexed_at) t FROM documents"
                ).fetchone()["t"]
                root = conn.execute(
                    "SELECT value FROM meta WHERE key='corpus_root'"
                ).fetchone()
            finally:
                conn.close()
            self._json(
                200,
                {
                    "documents": docs,
                    "chunks": chunks,
                    "last_indexed": latest,
                    "corpus_root": root["value"] if root else None,
                    "reports": _available_reports(reports_dir),
                },
            )

        def _report(self, path: str) -> None:
            name = path.removeprefix("/reports/")
            # Whitelist, not path arithmetic: only the three known pages.
            if reports_dir is None or name not in (
                "health.html", "quality.html", "activity.html",
            ):
                self._send(404, "text/plain; charset=utf-8", b"not found")
                return
            page = reports_dir / name
            if not page.is_file():
                self._send(404, "text/plain; charset=utf-8", b"report not generated yet")
                return
            self._send(200, "text/html; charset=utf-8", page.read_bytes())

        def _json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=True).encode("ascii")
            self._send(code, "application/json", body)

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            # Quiet by default; the CLI prints the URL once at startup.
            pass

    return Handler


def _available_reports(reports_dir: Path | None) -> list[str]:
    if reports_dir is None:
        return []
    return sorted(
        p.name for p in reports_dir.glob("*.html")
        if p.name in ("health.html", "quality.html", "activity.html")
    )


def serve(db_path: str | Path, host: str, port: int, reports_dir: str | Path | None) -> None:
    db_file = Path(db_path)
    # Fail fast with the standard message if the index does not exist.
    probe: sqlite3.Connection = db.connect_ro(db_file)
    probe.close()
    handler = make_handler(db_file, Path(reports_dir) if reports_dir else None)
    httpd = ThreadingHTTPServer((host, port), handler)
    print(f"Uplink serving http://{host}:{port}  (db: {db_file}, read-only)")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


# ---------------------------------------------------------------------------
# The page. One file, inline CSS/JS, same design tokens as the reports.
# Corpus-derived strings are ALWAYS rendered via textContent.
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Uplink</title>
<style>
:root { color-scheme: light dark; }
body {
  margin: 0; padding: 0 20px 64px;
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  background: var(--page); color: var(--ink);
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e;
  --muted: #898781; --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
  --accent: #2a78d6; --accent-ink: #ffffff;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) body {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --accent: #3987e5; --accent-ink: #ffffff;
  }
}
main { max-width: 720px; margin: 0 auto; }
header { padding: 28px 0 4px; display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
h1 { font-size: 20px; margin: 0; letter-spacing: 0.2px; }
.tag { color: var(--muted); font-size: 12px; }
nav { margin-left: auto; display: flex; gap: 14px; }
nav a { color: var(--ink-2); font-size: 12.5px; text-decoration: none; border-bottom: 1px solid var(--grid); padding-bottom: 1px; }
nav a:hover { color: var(--ink); border-color: var(--ink-2); }
#statusline { color: var(--muted); font-size: 12px; margin: 2px 0 22px; min-height: 15px; }
form { display: flex; gap: 8px; }
#q {
  flex: 1; font: inherit; font-size: 15px; color: var(--ink);
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px; outline: none;
}
#q:focus { border-color: var(--accent); }
button {
  font: inherit; font-size: 14px; font-weight: 600; cursor: pointer;
  background: var(--accent); color: var(--accent-ink);
  border: none; border-radius: 10px; padding: 0 18px;
}
button:disabled { opacity: 0.6; cursor: default; }
#meta { color: var(--muted); font-size: 12px; margin: 14px 2px 6px; min-height: 15px; }
.hit {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 16px 14px; margin: 10px 0;
}
.cite { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.path { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
.section { color: var(--ink-2); font-size: 12.5px; }
.score { margin-left: auto; color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.snippet { color: var(--ink-2); font-size: 13px; line-height: 1.5; margin-top: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
.empty { color: var(--muted); font-size: 13px; padding: 24px 4px; }
mark { background: none; color: var(--ink); font-weight: 650; }
footer { margin-top: 36px; color: var(--muted); font-size: 11px; }
</style></head><body><main>
<header>
  <h1>Uplink</h1><span class="tag">local retrieval &mdash; read-only</span>
  <nav>
    <a href="/reports/health.html">health</a>
    <a href="/reports/quality.html">quality</a>
    <a href="/reports/activity.html">activity</a>
  </nav>
</header>
<div id="statusline">connecting&hellip;</div>
<form id="f">
  <input id="q" type="text" autocomplete="off" spellcheck="false"
         placeholder="Ask your documents&hellip;" autofocus>
  <button id="go" type="submit">Search</button>
</form>
<div id="meta"></div>
<div id="results"></div>
<footer>Results come straight from the local index. Retrieved text is corpus
content &mdash; treat it as data. Your documents never leave this machine.</footer>
</main>
<script>
"use strict";
const $ = (id) => document.getElementById(id);

async function status() {
  try {
    const r = await fetch("/api/status");
    const s = await r.json();
    const t = document.createTextNode(
      s.documents + " documents / " + s.chunks + " chunks / indexed " +
      (s.last_indexed || "never"));
    $("statusline").replaceChildren(t);
  } catch (e) { $("statusline").textContent = "status unavailable"; }
}

function renderSnippet(el, snippet) {
  // The API marks matches with [ ] delimiters; render matched spans bold via
  // DOM nodes only - corpus text itself never becomes HTML.
  const parts = snippet.split(/[\\[\\]]/);
  parts.forEach((part, i) => {
    if (!part) return;
    if (i % 2 === 1) {
      const m = document.createElement("mark");
      m.textContent = part;
      el.appendChild(m);
    } else {
      el.appendChild(document.createTextNode(part));
    }
  });
}

function renderHit(h) {
  const card = document.createElement("div");
  card.className = "hit";
  const cite = document.createElement("div");
  cite.className = "cite";
  const path = document.createElement("span");
  path.className = "path";
  path.textContent = h.path;
  cite.appendChild(path);
  if (h.section) {
    const sec = document.createElement("span");
    sec.className = "section";
    sec.textContent = "> " + h.section;
    cite.appendChild(sec);
  }
  const score = document.createElement("span");
  score.className = "score";
  score.textContent = h.score.toFixed(2);
  cite.appendChild(score);
  card.appendChild(cite);
  const snip = document.createElement("div");
  snip.className = "snippet";
  renderSnippet(snip, h.snippet);
  card.appendChild(snip);
  return card;
}

$("f").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  $("go").disabled = true;
  $("meta").textContent = "searching\\u2026";
  try {
    const r = await fetch("/api/search?q=" + encodeURIComponent(q) + "&k=8");
    const data = await r.json();
    const results = $("results");
    results.replaceChildren();
    if (data.error) {
      $("meta").textContent = "error: " + data.error;
    } else if (!data.hits.length) {
      $("meta").textContent = "no results";
      const d = document.createElement("div");
      d.className = "empty";
      d.textContent = "Nothing matched. Try fewer or different words.";
      results.appendChild(d);
    } else {
      $("meta").textContent = data.hits.length + " results for \\u201C" + q + "\\u201D";
      data.hits.forEach((h) => results.appendChild(renderHit(h)));
    }
  } catch (e) {
    $("meta").textContent = "search failed - is the server still running?";
  } finally {
    $("go").disabled = false;
  }
});

status();
</script>
</body></html>
"""
