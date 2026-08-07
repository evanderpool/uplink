"""Uplink web UI — a local retrieval explorer with localhost-gated writes.

`python -m uplink serve` starts a stdlib-only HTTP server (no frameworks, no
JavaScript dependencies):

    GET  /                     the Ask page (search box + cited results)
    GET  /api/search?q=&k=&collection=   search results as JSON (read-only)
    GET  /api/status           index statistics as JSON
    GET  /api/doc?path=&collection=&seq=&start=&limit=   source text (read-only)
    GET  /reports/<kind>.html  generated reports, if present
    POST /api/upload           add a document to a collection   (localhost only)
    POST /api/feedback         thumbs up/down on a result       (localhost only)

Security posture:
- binds to 127.0.0.1 unless --host says otherwise (Tailscale exposure is a
  deliberate operator choice, never a default);
- THE LOCALHOST-ONLY WRITE RULE: the two write endpoints exist only when the
  server is bound to a loopback address. Bound to anything wider, every POST
  returns 403 and the UI never renders the controls — a phone on the
  tailnet is ask-only by construction, not by convention;
- browser-borne attacks on the loopback server are closed off twice: every
  request's Host header must name the server itself (defeats DNS
  rebinding), and write POSTs must carry the custom X-Uplink header and a
  loopback Origin (defeats CSRF — a hostile web page cannot attach custom
  headers cross-origin without a CORS preflight this server never grants);
- search paths still open the database read-only (SQLite mode=ro); the only
  database writes go through the same indexer the CLI uses;
- uploads are constrained: extension whitelist, size cap, sanitized bare
  filename, stored under data/uploads/<collection>/. A collection bound to a
  real folder refuses uploads rather than writing into the operator's files;
- corpus text reaches the page as JSON and is rendered with textContent,
  never innerHTML, so document content cannot inject markup or script;
- the query log and feedback log are local JSONL files next to the database.

Read surface, stated plainly: `/api/doc` serves untruncated chunk text so a
citation can be opened and checked — verification is the point of a RAG UI.
That makes reads wider than `/api/search` alone (which truncates to 1200
chars per hit), so anyone who can reach the port can page a document out in
full. Reads are GETs and therefore unaffected by the localhost-only write
rule: "ask-only" describes writes, not confidentiality. Both search and
document reads are appended to the local query log. Before binding beyond
loopback, decide that corpus reads by anyone on that network are acceptable.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import asks, db
from .extractors import SUPPORTED_EXTENSIONS
from .feedback import VALID_VOTES, append_jsonl
from .search import hits_to_dicts, search

MAX_QUERY_LEN = 500
MAX_K = 25
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_FEEDBACK_BYTES = 10 * 1024

LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]")


# SQLite binds 64-bit integers; anything wider raises OverflowError at query
# time, which would surface as a 500 with a traceback.
_INT64_MAX = 2 ** 63 - 1


def _int_param(params: dict, name: str, default: int | None) -> int | None:
    """Parse one integer query parameter, falling back on anything odd."""
    raw = (params.get(name) or [""])[0].strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if not -_INT64_MAX <= value <= _INT64_MAX:
        return default
    return value


def sanitize_filename(raw: str) -> str:
    """Reduce a client-supplied filename to a safe bare name, or raise.

    Path separators are stripped (basename only), unsafe characters removed,
    and hidden/empty names rejected. The extension must be one the indexer
    supports — anything else has no reason to enter the corpus.
    """
    name = raw.replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _SAFE_FILENAME_RE.sub("", name)
    if not name or name.startswith(".") or len(name) > 150:
        raise ValueError("invalid filename")
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS or len(Path(name).stem.strip()) == 0:
        allowed = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(f"unsupported file type '{ext or '(none)'}' — allowed: {allowed}")
    return name


def parse_multipart(body: bytes, content_type: str) -> dict[str, tuple[str, bytes]]:
    """Minimal multipart/form-data parser (stdlib `cgi` is gone in 3.13).

    Returns {field_name: (filename, value_bytes)}; filename is "" for plain
    fields. Preserves part content byte-exactly: only the single transport
    CRLF before the next boundary is removed, never the payload's own
    trailing newlines (a stripped final 0x0A corrupts uploads and makes the
    stored sha256 hash a file the operator never sent). Raises ValueError on
    anything that does not look like multipart.
    """
    m = re.search(r'boundary="?([^";]+)"?', content_type)
    if not m:
        raise ValueError("missing multipart boundary")
    delim = b"--" + m.group(1).encode("ascii", "replace")
    fields: dict[str, tuple[str, bytes]] = {}
    segments = body.split(delim)
    for seg in segments[1:]:  # segments[0] is the preamble
        if seg.startswith(b"--"):
            break  # closing delimiter
        if seg.startswith(b"\r\n"):
            seg = seg[2:]
        head, sep, payload = seg.partition(b"\r\n\r\n")
        if not sep:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]  # the transport CRLF before the delimiter
        disp = ""
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-disposition"):
                disp = line.decode("utf-8", "replace")
        # `name=` must not match inside `filename=` regardless of parameter
        # order (RFC 7578 fixes neither order nor our luck).
        name_m = re.search(r'(?<![a-zA-Z])name="([^"]*)"', disp)
        if not name_m:
            continue
        file_m = re.search(r'filename="([^"]*)"', disp)
        fields[name_m.group(1)] = (file_m.group(1) if file_m else "", payload)
    if not fields:
        raise ValueError("empty multipart body")
    return fields


def make_handler(
    db_path: Path,
    reports_dir: Path | None,
    writes_enabled: bool,
    bind_host: str = "127.0.0.1",
):
    data_dir = db_path.parent
    uploads_root = data_dir / "uploads"
    query_log = data_dir / "query-log.jsonl"
    feedback_log = data_dir / "feedback.jsonl"
    asks_dir = data_dir / "asks"
    allowed_hosts = {bind_host.lower(), "127.0.0.1", "localhost", "::1", "[::1]"}
    # Serializes browser uploads: index_folder holds a write transaction for
    # its whole run, so without this a second simultaneous upload waits out
    # busy_timeout and dies with "database is locked".
    write_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "Uplink"

        def _host_ok(self) -> bool:
            """The Host header must name this server or be an IP literal —
            defeats DNS rebinding, where evil.com re-points to 127.0.0.1 so a
            hostile page's fetches become same-origin and corpus text becomes
            readable off-machine. IP-literal Hosts are safe (rebinding needs
            a DNS name) and keep deliberate wide binds (0.0.0.0, a Tailscale
            IP) reachable by address."""
            host = (self.headers.get("Host") or "").strip().lower()
            if not host:
                return True  # HTTP/1.0 clients; browsers always send Host
            if host.startswith("["):  # [::1]:8180
                host = host.split("]", 1)[0].lstrip("[")
            else:
                host = host.rsplit(":", 1)[0]
            if host in allowed_hosts or f"[{host}]" in allowed_hosts:
                return True
            try:
                ipaddress.ip_address(host)
                return True
            except ValueError:
                return False

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            if not self._host_ok():
                self._json(403, {"error": "unrecognized Host header"})
                return
            url = urlparse(self.path)
            try:
                if url.path == "/":
                    self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
                elif url.path == "/api/search":
                    self._api_search(url)
                elif url.path == "/api/status":
                    self._api_status()
                elif url.path.startswith("/api/ask/"):
                    self._api_ask_status(url.path.removeprefix("/api/ask/"))
                elif url.path == "/api/doc":
                    self._api_doc(url)
                elif url.path.startswith("/reports/"):
                    self._report(url.path)
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found")
            except FileNotFoundError as exc:
                self._json(404, {"error": str(exc)})
            except ValueError as exc:  # bad parameter (e.g. collection name)
                self._json(400, {"error": str(exc)})
            except Exception as exc:  # never leak a traceback to the client
                self._json(500, {"error": f"{type(exc).__name__}"})
                raise

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if not writes_enabled:
                # The localhost-only rule. Drain the body first or Windows
                # clients see a connection reset instead of the status.
                self._drain()
                self._json(403, {"error": "writes are enabled only when bound to localhost"})
                return
            if not self._host_ok():
                self._drain()
                self._json(403, {"error": "unrecognized Host header"})
                return
            # CSRF defense: a browser will not attach a custom header to a
            # cross-origin request without a CORS preflight, and this server
            # never grants one — so requiring X-Uplink (plus a loopback
            # Origin when one is sent) means a hostile page the operator
            # happens to visit cannot write into the corpus.
            origin = (self.headers.get("Origin") or "").strip()
            if origin:
                o_host = urlparse(origin).hostname or ""
                if o_host not in ("127.0.0.1", "localhost", "::1"):
                    self._drain()
                    self._json(403, {"error": "cross-origin writes are not allowed"})
                    return
            if not self.headers.get("X-Uplink"):
                self._drain()
                self._json(403, {"error": "missing X-Uplink header"})
                return
            try:
                if url.path == "/api/upload":
                    self._api_upload()
                elif url.path == "/api/feedback":
                    self._api_feedback()
                elif url.path == "/api/ask":
                    self._api_ask()
                else:
                    self._drain()
                    self._send(404, "text/plain; charset=utf-8", b"not found")
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
            except sqlite3.OperationalError:
                # Another writer (a CLI index run) holds the database lock.
                self._json(503, {"error": "index is busy — try again shortly"})
            except Exception as exc:
                self._json(500, {"error": f"{type(exc).__name__}"})
                raise

        def do_PUT(self) -> None:  # noqa: N802
            self._drain()
            self._send(405, "text/plain; charset=utf-8", b"method not allowed")

        do_DELETE = do_PATCH = do_PUT

        # ------------------------------------------------------------- reads

        def _api_search(self, url) -> None:
            params = parse_qs(url.query)
            query = (params.get("q") or [""])[0].strip()[:MAX_QUERY_LEN]
            collection = (params.get("collection") or [""])[0].strip() or None
            if collection is not None:
                collection = db.validate_collection(collection)
            try:
                k = min(MAX_K, max(1, int((params.get("k") or ["8"])[0])))
            except ValueError:
                k = 8
            if not query:
                self._json(400, {"error": "missing q parameter"})
                return
            t0 = time.perf_counter()
            hits = search(db_path, query, k=k, collection=collection)
            latency_ms = round((time.perf_counter() - t0) * 1000, 1)
            append_jsonl(
                query_log,
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "q": query,
                    "collection": collection,
                    "k": k,
                    "hits": len(hits),
                    "latency_ms": latency_ms,
                },
            )
            self._json(
                200,
                {
                    "query": query,
                    "k": k,
                    "collection": collection,
                    "latency_ms": latency_ms,
                    "hits": hits_to_dicts(hits),
                },
            )

        def _api_status(self) -> None:
            conn = db.connect_ro(db_path)
            try:
                docs = conn.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
                chunks = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
                latest = conn.execute(
                    "SELECT MAX(indexed_at) t FROM documents"
                ).fetchone()["t"]
                collections = db.list_collections(conn)
            finally:
                conn.close()
            self._json(
                200,
                {
                    "documents": docs,
                    "chunks": chunks,
                    "last_indexed": latest,
                    "collections": collections,
                    "writes": writes_enabled,
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

        def _api_doc(self, url) -> None:
            """Serve a window of one document's chunks so a citation can be
            opened and checked.

            The text comes from the INDEX, never the filesystem: the path is
            an exact parameterized lookup in `documents`, so there is no file
            read to traverse and nothing outside the corpus is reachable.
            What you see here is literally what retrieval saw.
            """
            params = parse_qs(url.query)
            path = (params.get("path") or [""])[0].strip()
            if not path:
                self._json(400, {"error": "missing path parameter"})
                return
            collection = (params.get("collection") or [""])[0].strip() or None
            if collection is not None:
                collection = db.validate_collection(collection)
            seq = _int_param(params, "seq", default=None)
            explicit_start = _int_param(params, "start", default=None)
            limit = min(40, max(1, _int_param(params, "limit", default=9) or 9))

            conn = db.connect_ro(db_path)
            try:
                if collection:
                    docs = conn.execute(
                        "SELECT id, path, title, filetype, collection FROM documents "
                        "WHERE collection = ? AND path = ?", (collection, path),
                    ).fetchall()
                else:
                    docs = conn.execute(
                        "SELECT id, path, title, filetype, collection FROM documents "
                        "WHERE path = ? ORDER BY collection", (path,),
                    ).fetchall()
                if not docs:
                    self._json(404, {"error": "document not found in the index"})
                    return
                if len(docs) > 1:
                    # Paths are relative to each collection root, so the same
                    # name can exist in several. Guessing would show the
                    # reader unrelated text as the source of a claim — fail
                    # safe and make the caller name the collection.
                    self._json(
                        409,
                        {
                            "error": f"'{path}' exists in several collections — "
                                     "name one with &collection=",
                            "collections": [d["collection"] for d in docs],
                        },
                    )
                    return
                doc = docs[0]
                total = conn.execute(
                    "SELECT COUNT(*) n FROM chunks WHERE doc_id = ?", (doc["id"],)
                ).fetchone()["n"]
                # `start` pages absolutely; `seq` centres the window on a
                # cited chunk. Mixing the two silently skipped chunks when
                # paging backward, so `start` wins when both are present.
                if explicit_start is not None:
                    start = max(0, explicit_start)
                elif seq is not None:
                    start = max(0, seq - limit // 2)
                else:
                    start = 0
                rows = conn.execute(
                    "SELECT seq, section, text FROM chunks WHERE doc_id = ? "
                    "AND seq >= ? ORDER BY seq LIMIT ?",
                    (doc["id"], start, limit),
                ).fetchall()
            finally:
                conn.close()
            # Reads are logged like searches: /api/doc returns untruncated
            # text, so it belongs in the same local audit trail.
            append_jsonl(
                query_log,
                {
                    "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "kind": "doc",
                    "path": doc["path"],
                    "collection": doc["collection"],
                    "start": start,
                    "chunks": len(rows),
                },
            )
            self._json(
                200,
                {
                    "path": doc["path"],
                    "title": doc["title"],
                    "filetype": doc["filetype"],
                    "collection": doc["collection"],
                    "total_chunks": total,
                    "start": start,
                    "chunks": [
                        {"seq": r["seq"], "section": r["section"] or "", "text": r["text"]}
                        for r in rows
                    ],
                },
            )

        def _api_ask_status(self, ask_id: str) -> None:
            """Poll one queued question. Read-only: reads the response file
            the brain session wrote (or reports pending/unknown)."""
            self._json(200, asks.get_status(asks_dir, ask_id))

        # ------------------------------------------------------ writes (localhost)

        def _api_ask(self) -> None:
            """Queue a question for the brain session (see AGENT.md).

            Behind the localhost-only write gate like every POST: remote
            ask-only access is exactly what the integration review gates,
            so today the queue adds zero remote surface."""
            body = self._read_body(MAX_FEEDBACK_BYTES)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("expected a JSON body") from None
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            q = str(payload.get("q") or "").strip()[:MAX_QUERY_LEN]
            if not q:
                raise ValueError("missing q")
            collection = payload.get("collection")
            if collection:
                collection = db.validate_collection(str(collection))
            try:
                k = min(MAX_K, max(1, int(payload.get("k") or 8)))
            except (TypeError, ValueError):
                k = 8
            req = asks.new_ask(asks_dir, q, collection or None, k=k)
            self._json(200, {"id": req["id"], "state": "pending"})

        def _read_body(self, cap: int) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise ValueError("invalid Content-Length") from None
            if length < 0:
                # A negative length must not reach rfile.read(): read(-1)
                # reads the socket to EOF, bypassing the cap entirely.
                raise ValueError("invalid Content-Length")
            if length > cap:
                # Discard so the client receives the error instead of a reset.
                self._drain()
                raise ValueError(f"body too large (max {cap} bytes)")
            return self.rfile.read(length) if length else b""

        def _api_upload(self) -> None:
            ctype = self.headers.get("Content-Type") or ""
            if "multipart/form-data" not in ctype:
                self._drain()
                raise ValueError("expected multipart/form-data")
            body = self._read_body(MAX_UPLOAD_BYTES)
            fields = parse_multipart(body, ctype)
            filename_raw, file_bytes = fields.get("file", ("", b""))
            if not filename_raw or not file_bytes:
                raise ValueError("missing file field")
            _, coll_bytes = fields.get("collection", ("", b""))
            collection = db.validate_collection(
                coll_bytes.decode("utf-8", "replace").strip() or db.DEFAULT_COLLECTION
            )
            filename = sanitize_filename(filename_raw)

            target_dir = (uploads_root / collection).resolve()
            # A collection bound to a real folder must not be written to.
            conn = db.connect_ro(db_path)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key=?", (f"corpus_root:{collection}",)
                ).fetchone()
            finally:
                conn.close()
            if row and Path(row["value"]) != target_dir:
                self._json(
                    409,
                    {
                        "error": (
                            f"collection '{collection}' is bound to folder "
                            f"'{row['value']}' — drop the file there and re-index, "
                            "or upload to a different collection"
                        )
                    },
                )
                return

            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / filename
            with write_lock:
                self._save_and_index(target, file_bytes, target_dir, collection, filename)

        def _save_and_index(
            self, target: Path, file_bytes: bytes, target_dir: Path,
            collection: str, filename: str,
        ) -> None:
            if target.exists() and target.read_bytes() != file_bytes:
                # Sanitization strips directories, so distinct sources can
                # collide on one name — refuse rather than silently replace
                # an already-indexed document.
                self._json(
                    409,
                    {
                        "error": (
                            f"'{filename}' already exists in '{collection}' with "
                            "different content — rename the file or remove the "
                            "existing one first"
                        )
                    },
                )
                return
            target.write_bytes(file_bytes)

            from .indexer import index_folder

            try:
                stats = index_folder(target_dir, db_path, collection=collection)
            except Exception:
                # Never leave an unindexed file behind: it would silently
                # join the corpus on the next unrelated index run.
                target.unlink(missing_ok=True)
                raise
            own_errors = [e for e in stats.errors if e.startswith(filename + ":")]
            if own_errors:
                target.unlink(missing_ok=True)
                self._json(400, {"error": f"file could not be indexed: {own_errors[0]}"})
                return
            self._json(
                200,
                {
                    "saved": filename,
                    "collection": collection,
                    "indexed": stats.indexed,
                    "unchanged": stats.unchanged,
                    "chunks": stats.chunks,
                    "errors": stats.errors,
                },
            )

        def _api_feedback(self) -> None:
            body = self._read_body(MAX_FEEDBACK_BYTES)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("expected a JSON body") from None
            if not isinstance(payload, dict):
                raise ValueError("expected a JSON object")
            q = str(payload.get("q") or "").strip()[:MAX_QUERY_LEN]
            path = str(payload.get("path") or "").strip()[:500]
            vote = payload.get("vote")
            if not q or not path or vote not in VALID_VOTES:
                raise ValueError("feedback needs q, path, and vote of 'up' or 'down'")
            collection = payload.get("collection")
            if collection:
                collection = db.validate_collection(str(collection))
            # The path must name an indexed document. Unvalidated, a forged
            # short path (e.g. "e") promoted into a fixture matches nearly
            # every result by substring and inflates the quality numbers.
            conn = db.connect_ro(db_path)
            try:
                if collection:
                    known = conn.execute(
                        "SELECT 1 FROM documents WHERE collection = ? AND path = ?",
                        (collection, path),
                    ).fetchone()
                else:
                    known = conn.execute(
                        "SELECT 1 FROM documents WHERE path = ?", (path,)
                    ).fetchone()
            finally:
                conn.close()
            if not known:
                raise ValueError("path does not name an indexed document")
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "q": q,
                "path": path,
                "seq": payload.get("seq") if isinstance(payload.get("seq"), int) else None,
                "vote": vote,
                "collection": collection or None,
            }
            append_jsonl(feedback_log, entry)
            self._json(200, {"recorded": vote})

        # ---------------------------------------------------------- plumbing

        def _drain(self) -> None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            while length > 0:
                read = self.rfile.read(min(length, 1 << 16))
                if not read:
                    break
                length -= len(read)

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
    writes_enabled = host in LOOPBACK_HOSTS
    if writes_enabled and not db_file.exists():
        # First-run flow: serve an empty index so the operator can upload
        # their first document through the browser.
        db.connect_rw(db_file).close()
    # Fail fast with the standard message if the index does not exist.
    probe: sqlite3.Connection = db.connect_ro(db_file)
    probe.close()
    handler = make_handler(
        db_file, Path(reports_dir) if reports_dir else None, writes_enabled, host
    )

    class _Server(ThreadingHTTPServer):
        # ThreadingHTTPServer is AF_INET-only by default; an IPv6 bind
        # (::1, or a Tailscale IPv6 address) needs the right family.
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    httpd = _Server((host, port), handler)
    mode = "writes: localhost-enabled" if writes_enabled else "read-only (non-loopback bind)"
    print(f"Uplink serving http://{host}:{port}  (db: {db_file}, {mode})")
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
# Write controls (upload, thumbs) render only when /api/status says writes
# are enabled — and the server refuses the POSTs regardless of the UI.
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
  --accent: #2a78d6; --accent-ink: #ffffff; --good: #2e7d4f; --bad: #b3402a;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) body {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7;
    --muted: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    --accent: #3987e5; --accent-ink: #ffffff; --good: #58b384; --bad: #e0765f;
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
form#f { display: flex; gap: 8px; }
#q {
  flex: 1; font: inherit; font-size: 15px; color: var(--ink);
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 12px 14px; outline: none; min-width: 0;
}
#q:focus { border-color: var(--accent); }
select {
  font: inherit; font-size: 13px; color: var(--ink);
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 0 10px; max-width: 160px;
}
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
.coll { color: var(--accent); font-size: 11px; border: 1px solid var(--border); border-radius: 999px; padding: 1px 8px; }
.section { color: var(--ink-2); font-size: 12.5px; }
.score { margin-left: auto; color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums; }
.snippet { color: var(--ink-2); font-size: 13px; line-height: 1.5; margin-top: 6px; white-space: pre-wrap; overflow-wrap: anywhere; }
.fb { display: flex; gap: 6px; margin-top: 8px; }
.fb button {
  background: none; border: 1px solid var(--border); color: var(--muted);
  border-radius: 8px; font-size: 12px; padding: 3px 10px; font-weight: 500;
}
.fb button:hover { color: var(--ink); border-color: var(--ink-2); }
.fb button.done-up { color: var(--good); border-color: var(--good); }
.fb button.done-down { color: var(--bad); border-color: var(--bad); }
.empty { color: var(--muted); font-size: 13px; padding: 24px 4px; }
mark { background: none; color: var(--ink); font-weight: 650; }
#answer { margin: 10px 0; }
.ans {
  background: var(--surface); border: 1px solid var(--accent);
  border-radius: 10px; padding: 14px 16px;
}
.ans-label { color: var(--accent); font-size: 11px; font-weight: 700;
  letter-spacing: 0.6px; text-transform: uppercase; margin-bottom: 6px; }
.ans-text { font-size: 13.5px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.ans-cites { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.ans-cites button { color: var(--ink-2); font-size: 11.5px; border: 1px solid var(--border);
  border-radius: 999px; padding: 2px 9px; overflow-wrap: anywhere; background: none;
  font-weight: 500; text-align: left; cursor: pointer; }
.ans-cites button:hover { color: var(--ink); border-color: var(--accent); }
.path.link { cursor: pointer; text-decoration: underline; text-decoration-style: dotted;
  text-underline-offset: 3px; }
.path.link:hover { color: var(--accent); }
#viewer { margin: 12px 0; }
.doc {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px 16px;
}
.doc-head { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap;
  border-bottom: 1px solid var(--grid); padding-bottom: 8px; margin-bottom: 10px; }
.doc-title { font-size: 13px; font-weight: 700; overflow-wrap: anywhere; }
.doc-meta { color: var(--muted); font-size: 11.5px; }
.doc-close { margin-left: auto; background: none; border: 1px solid var(--border);
  color: var(--ink-2); border-radius: 8px; font-size: 12px; padding: 2px 10px; font-weight: 500; }
.chunk { border-left: 2px solid var(--grid); padding: 2px 0 2px 12px; margin: 12px 0; }
.chunk.cited { border-left-color: var(--accent); }
.chunk-head { color: var(--muted); font-size: 11px; font-variant-numeric: tabular-nums;
  margin-bottom: 4px; }
.chunk-text { font-size: 12.5px; line-height: 1.55; white-space: pre-wrap;
  overflow-wrap: anywhere; color: var(--ink-2); }
.chunk.cited .chunk-text { color: var(--ink); }
.doc-nav { display: flex; gap: 8px; align-items: center; margin-top: 12px;
  border-top: 1px solid var(--grid); padding-top: 10px; }
.doc-nav button { background: none; border: 1px solid var(--border); color: var(--ink-2);
  border-radius: 8px; font-size: 12px; padding: 3px 12px; font-weight: 500; }
.doc-nav button:hover:not(:disabled) { color: var(--ink); border-color: var(--ink-2); }
.doc-nav span { color: var(--muted); font-size: 11.5px; }
.ans-wait { color: var(--muted); font-size: 12.5px; padding: 10px 2px; }
button.secondary { background: none; color: var(--accent); border: 1px solid var(--accent); }
details#up { margin-top: 28px; }
details#up summary { color: var(--ink-2); font-size: 13px; cursor: pointer; }
#upform { display: flex; gap: 8px; margin-top: 10px; flex-wrap: wrap; align-items: center; }
#upform input[type=file] { font-size: 12.5px; color: var(--ink-2); max-width: 100%; }
#upform input[type=text] {
  font: inherit; font-size: 13px; color: var(--ink); background: var(--surface);
  border: 1px solid var(--border); border-radius: 10px; padding: 8px 10px; width: 140px;
}
#upmsg { color: var(--muted); font-size: 12px; margin-top: 8px; min-height: 15px; }
footer { margin-top: 36px; color: var(--muted); font-size: 11px; }
</style></head><body><main>
<header>
  <h1>Uplink</h1><span class="tag">local retrieval</span>
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
  <select id="coll" title="Collection"><option value="">all collections</option></select>
  <button id="go" type="submit">Search</button>
  <button id="ai" type="button" class="secondary" hidden>Ask AI</button>
</form>
<div id="meta"></div>
<div id="answer"></div>
<div id="viewer"></div>
<div id="results"></div>
<details id="up" hidden>
  <summary>Add a document</summary>
  <div id="upform">
    <input id="file" type="file">
    <input id="upcoll" type="text" placeholder="collection (e.g. finance)"
           autocomplete="off" spellcheck="false">
    <button id="upgo" type="button">Upload &amp; index</button>
  </div>
  <div id="upmsg"></div>
</details>
<footer>Results come straight from the local index. Retrieved text is corpus
content &mdash; treat it as data. Your documents never leave this machine.</footer>
</main>
<script>
"use strict";
const $ = (id) => document.getElementById(id);
let WRITES = false;
let LASTQ = "";

async function status() {
  try {
    const r = await fetch("/api/status");
    const s = await r.json();
    WRITES = !!s.writes;
    const t = document.createTextNode(
      s.documents + " documents / " + s.chunks + " chunks / indexed " +
      (s.last_indexed || "never") + (WRITES ? "" : " / read-only"));
    $("statusline").replaceChildren(t);
    const sel = $("coll");
    while (sel.options.length > 1) sel.remove(1);
    (s.collections || []).forEach((c) => {
      const o = document.createElement("option");
      o.value = c.name;
      o.textContent = c.name + " (" + c.documents + ")";
      sel.appendChild(o);
    });
    $("up").hidden = !WRITES;
    $("ai").hidden = !WRITES;
  } catch (e) { $("statusline").textContent = "status unavailable"; }
}

let pollTimer = null;

function renderAnswer(resp, question) {
  // Every field is coerced: a malformed response (citations as a string, a
  // null entry) must never throw here - that would blank the card and
  // strand the button mid-poll.
  const box = $("answer");
  box.replaceChildren();
  const card = document.createElement("div");
  card.className = "ans";
  const label = document.createElement("div");
  label.className = "ans-label";
  label.textContent = "AI answer for: " + String(question || "");
  card.appendChild(label);
  const text = document.createElement("div");
  text.className = "ans-text";
  text.textContent = String(resp.answer || "");
  card.appendChild(text);
  const list = Array.isArray(resp.citations) ? resp.citations : [];
  if (list.length) {
    const cites = document.createElement("div");
    cites.className = "ans-cites";
    list.forEach((c) => {
      if (!c || typeof c !== "object") return;
      const path = String(c.path || "");
      if (!path) return;
      // Clickable: a citation you cannot open is a claim, not evidence.
      const chip = document.createElement("button");
      chip.type = "button";
      chip.textContent = path + (c.section ? " > " + String(c.section) : "");
      chip.title = "open this source and read the indexed text";
      const seq = Number.isInteger(c.seq) ? c.seq : null;
      chip.addEventListener("click", () => openDoc(path, c.collection || null, seq));
      cites.appendChild(chip);
    });
    if (cites.childNodes.length) card.appendChild(cites);
  }
  box.appendChild(card);
}

function clearAnswer() {
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  $("answer").replaceChildren();
  $("viewer").replaceChildren();
  $("ai").disabled = false;
}

async function askAI() {
  const q = $("q").value.trim();
  if (!q) return;
  LASTQ = q;
  clearAnswer();
  $("ai").disabled = true;
  const box = $("answer");
  const wait = document.createElement("div");
  wait.className = "ans-wait";
  wait.textContent = "queued - waiting for the brain session\\u2026";
  box.appendChild(wait);
  try {
    const r = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Uplink": "1" },
      body: JSON.stringify({ q: q, collection: $("coll").value || null }),
    });
    const data = await r.json();
    if (data.error) { wait.textContent = "error: " + data.error; $("ai").disabled = false; return; }
    const started = Date.now();
    pollTimer = setInterval(async () => {
      const elapsed = Math.round((Date.now() - started) / 1000);
      try {
        const pr = await fetch("/api/ask/" + data.id);
        const st = await pr.json();
        if (st.state === "answered") {
          clearInterval(pollTimer); pollTimer = null;
          try { renderAnswer(st, q); } finally { $("ai").disabled = false; }
        } else if (st.state === "error") {
          clearInterval(pollTimer); pollTimer = null;
          wait.textContent = "brain error: " + String(st.error || "unknown");
          $("ai").disabled = false;
        } else if (elapsed > 180) {
          clearInterval(pollTimer); pollTimer = null;
          wait.textContent = "no answer after 3 minutes - is the brain session running with its watcher armed?";
          $("ai").disabled = false;
        } else {
          wait.textContent = "waiting for the brain session\\u2026 " + elapsed + "s";
        }
      } catch (e) { /* transient poll failure - keep polling */ }
    }, 2000);
  } catch (e) {
    wait.textContent = "ask failed - is the server still running?";
    $("ai").disabled = false;
  }
}

$("ai").addEventListener("click", askAI);

// --- source viewer: open any citation and read what retrieval actually saw ---

function closeViewer() { $("viewer").replaceChildren(); }

function renderDoc(doc, citedSeq) {
  // Every value is coerced and inserted as text; document content never
  // becomes markup here any more than it does in a search result.
  const box = $("viewer");
  box.replaceChildren();
  const card = document.createElement("div");
  card.className = "doc";

  const head = document.createElement("div");
  head.className = "doc-head";
  const title = document.createElement("span");
  title.className = "doc-title";
  title.textContent = String(doc.path || "");
  head.appendChild(title);
  const meta = document.createElement("span");
  meta.className = "doc-meta";
  const total = Number(doc.total_chunks) || 0;
  meta.textContent = "source text from the index \\u00B7 " +
    String(doc.collection || "") + " \\u00B7 " + total + " chunks";
  head.appendChild(meta);
  const close = document.createElement("button");
  close.className = "doc-close";
  close.textContent = "close";
  close.addEventListener("click", closeViewer);
  head.appendChild(close);
  card.appendChild(head);

  const chunks = Array.isArray(doc.chunks) ? doc.chunks : [];
  chunks.forEach((c) => {
    if (!c || typeof c !== "object") return;
    const div = document.createElement("div");
    // Number.isInteger, not ==: Number(null) is 0, which would mark chunk
    // #0 as "the cited passage" whenever a citation carries no seq.
    const isCited = Number.isInteger(citedSeq) && Number(c.seq) === Number(citedSeq);
    div.className = "chunk" + (isCited ? " cited" : "");
    const h = document.createElement("div");
    h.className = "chunk-head";
    h.textContent = "#" + String(c.seq) + (c.section ? " \\u00B7 " + String(c.section) : "");
    div.appendChild(h);
    const t = document.createElement("div");
    t.className = "chunk-text";
    t.textContent = String(c.text || "");
    div.appendChild(t);
    card.appendChild(div);
  });

  const start = Number(doc.start) || 0;
  const shown = chunks.length;
  const nav = document.createElement("div");
  nav.className = "doc-nav";
  // Paging uses an ABSOLUTE start, never a seq to re-centre on: centring
  // shifts the window by limit/2 as well, which silently skipped chunks
  // every time the reader stepped backward.
  const prev = document.createElement("button");
  prev.textContent = "\\u2190 earlier";
  prev.disabled = start <= 0;
  prev.addEventListener("click", () =>
    openDocAt(doc.path, doc.collection, Math.max(0, start - shown), citedSeq));
  const next = document.createElement("button");
  next.textContent = "later \\u2192";
  next.disabled = start + shown >= total;
  next.addEventListener("click", () =>
    openDocAt(doc.path, doc.collection, start + shown, citedSeq));
  const pos = document.createElement("span");
  pos.textContent = shown
    ? "showing " + (start + 1) + "\\u2013" + (start + shown) + " of " + total
    : "no text";
  nav.appendChild(prev);
  nav.appendChild(next);
  nav.appendChild(pos);
  card.appendChild(nav);

  box.appendChild(card);
  card.scrollIntoView({ block: "nearest" });
}

async function fetchDoc(path, collection, params, citedSeq) {
  const box = $("viewer");
  box.replaceChildren();
  const wait = document.createElement("div");
  wait.className = "ans-wait";
  wait.textContent = "opening " + String(path) + "\\u2026";
  box.appendChild(wait);
  let url = "/api/doc?path=" + encodeURIComponent(path);
  if (collection) url += "&collection=" + encodeURIComponent(collection);
  url += params;
  try {
    const r = await fetch(url);
    const doc = await r.json();
    if (doc.error) {
      let msg = "cannot open: " + String(doc.error);
      if (Array.isArray(doc.collections) && doc.collections.length) {
        msg += " (" + doc.collections.map(String).join(", ") + ")";
      }
      wait.textContent = msg;
      return;
    }
    renderDoc(doc, citedSeq);
  } catch (e) {
    wait.textContent = "could not load the source document";
  }
}

// Open centred on a cited chunk.
function openDoc(path, collection, seq) {
  const anchored = Number.isInteger(seq);
  return fetchDoc(path, collection,
    anchored ? "&seq=" + encodeURIComponent(seq) : "",
    anchored ? seq : null);
}

// Page to an absolute offset, keeping whatever chunk was cited highlighted.
function openDocAt(path, collection, start, citedSeq) {
  return fetchDoc(path, collection, "&start=" + encodeURIComponent(start), citedSeq);
}

function renderSnippet(el, snippet) {
  // The API marks matches with non-printable \\u0001/\\u0002 delimiters (so
  // corpus text containing [ ] renders intact); matched spans become bold
  // via DOM nodes only - corpus text itself never becomes HTML.
  const parts = snippet.split(/[\\u0001\\u0002]/);
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

async function sendFeedback(h, vote, btn, other) {
  try {
    const r = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Uplink": "1" },
      body: JSON.stringify({
        q: LASTQ, path: h.path, seq: h.seq, vote: vote, collection: h.collection,
      }),
    });
    if (r.ok) {
      btn.className = "done-" + vote;
      other.className = "";
    }
  } catch (e) { /* feedback is best-effort */ }
}

function renderHit(h) {
  const card = document.createElement("div");
  card.className = "hit";
  const cite = document.createElement("div");
  cite.className = "cite";
  const path = document.createElement("span");
  path.className = "path link";
  path.textContent = h.path;
  path.title = "open this document at this passage";
  path.addEventListener("click", () => openDoc(h.path, h.collection, h.seq));
  cite.appendChild(path);
  if (h.collection) {
    const c = document.createElement("span");
    c.className = "coll";
    c.textContent = h.collection;
    cite.appendChild(c);
  }
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
  if (WRITES) {
    const fb = document.createElement("div");
    fb.className = "fb";
    const upB = document.createElement("button");
    upB.textContent = "\\uD83D\\uDC4D helpful";
    const downB = document.createElement("button");
    downB.textContent = "\\uD83D\\uDC4E off-target";
    upB.addEventListener("click", () => sendFeedback(h, "up", upB, downB));
    downB.addEventListener("click", () => sendFeedback(h, "down", downB, upB));
    fb.appendChild(upB);
    fb.appendChild(downB);
    card.appendChild(fb);
  }
  return card;
}

$("f").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  LASTQ = q;
  // A new search retires any AI answer (and cancels an in-flight one):
  // a cited answer must never sit above the results of a different question.
  clearAnswer();
  $("go").disabled = true;
  $("meta").textContent = "searching\\u2026";
  try {
    let url = "/api/search?q=" + encodeURIComponent(q) + "&k=8";
    const coll = $("coll").value;
    if (coll) url += "&collection=" + encodeURIComponent(coll);
    const r = await fetch(url);
    const data = await r.json();
    const results = $("results");
    results.replaceChildren();
    if (data.error) {
      $("meta").textContent = "error: " + data.error;
    } else if (!data.hits.length) {
      $("meta").textContent = "no results (" + data.latency_ms + " ms)";
      const d = document.createElement("div");
      d.className = "empty";
      d.textContent = "Nothing matched. Try fewer or different words.";
      results.appendChild(d);
    } else {
      $("meta").textContent = data.hits.length + " results for \\u201C" + q +
        "\\u201D \\u00B7 " + data.latency_ms + " ms";
      data.hits.forEach((h) => results.appendChild(renderHit(h)));
    }
  } catch (e) {
    $("meta").textContent = "search failed - is the server still running?";
  } finally {
    $("go").disabled = false;
  }
});

$("upgo").addEventListener("click", async () => {
  const f = $("file").files[0];
  if (!f) { $("upmsg").textContent = "choose a file first"; return; }
  const fd = new FormData();
  fd.append("collection", $("upcoll").value.trim() || "main");
  fd.append("file", f);
  $("upgo").disabled = true;
  $("upmsg").textContent = "uploading\\u2026";
  try {
    const r = await fetch("/api/upload", {
      method: "POST", headers: { "X-Uplink": "1" }, body: fd,
    });
    const data = await r.json();
    if (data.error) {
      $("upmsg").textContent = "error: " + data.error;
    } else {
      $("upmsg").textContent = "indexed " + data.saved + " into '" +
        data.collection + "' (" + data.chunks + " chunks)";
      $("file").value = "";
      status();
    }
  } catch (e) {
    $("upmsg").textContent = "upload failed";
  } finally {
    $("upgo").disabled = false;
  }
});

status();
</script>
</body></html>
"""
