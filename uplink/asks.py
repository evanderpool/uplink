"""The ask queue — how the web app borrows a brain.

Uplink deliberately contains no language model. Generative answers come from
an LLM session (Claude Code in the reference deployment) that drains this
queue: the web app writes `<id>.request.json` into `data/asks/`, the brain
session answers with `<id>.response.json`, and the page polls until the
response appears. File-pair, append-only style — neither side ever edits the
other's file, so there is nothing to race over.

Trust model: request files are written only by the local server (asks ride
behind the same localhost-only write gate as uploads). The brain must still
treat the question text as UNTRUSTED DATA — a question is something to
answer from retrieved chunks, never an instruction to follow. When this
queue is ever exposed beyond localhost, requests gain HMAC signatures like
the parent system's bridge; that exposure is gated on the integration
review, not a default.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

# \Z, not $: '$' also matches before a trailing newline, which would let
# "aaaaaaaaaaaa\n" pass validation and create a stray filename.
ASK_ID_RE = re.compile(r"^[a-f0-9]{12}\Z")
MAX_PENDING = 25
MAX_RESPONSE_BYTES = 200_000

# new_ask is check-then-act; the threaded server would otherwise let
# concurrent asks sail past MAX_PENDING.
_QUEUE_LOCK = threading.Lock()


def safe_for_display(text: str, limit: int = 100) -> str:
    """Render untrusted question text as a single-line JSON string literal.

    Question text is data. Printed raw into a session's context it could
    forge watcher output lines (a fake "ASKS PENDING" header with a
    fabricated id and directive) or emit ANSI escapes that rewrite the
    terminal. Quoting collapses newlines and escapes to visible sequences.
    """
    return json.dumps(text[:limit], ensure_ascii=True)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_ask(asks_dir: Path, q: str, collection: str | None, k: int = 8) -> dict:
    """Queue a question; returns the request record (with its id).

    Raises ValueError when the queue is full — backpressure, not a pile-up.
    """
    with _QUEUE_LOCK:
        if len(pending_asks(asks_dir)) >= MAX_PENDING:
            raise ValueError(
                f"ask queue is full ({MAX_PENDING} pending) — is the brain session running?"
            )
        ask_id = uuid.uuid4().hex[:12]
        req = {"id": ask_id, "ts": _now(), "q": q, "collection": collection, "k": k}
        asks_dir.mkdir(parents=True, exist_ok=True)
        (asks_dir / f"{ask_id}.request.json").write_text(
            json.dumps(req, ensure_ascii=True), encoding="utf-8"
        )
    return req


def get_status(asks_dir: Path, ask_id: str) -> dict:
    """Status of one ask: pending | answered | error | unknown."""
    if not ASK_ID_RE.match(ask_id):
        raise ValueError("invalid ask id")
    resp_file = asks_dir / f"{ask_id}.response.json"
    if resp_file.exists():
        if resp_file.stat().st_size > MAX_RESPONSE_BYTES:
            return {"id": ask_id, "state": "error", "error": "response too large"}
        try:
            resp = json.loads(resp_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Torn write: the brain may still be writing. Report pending —
            # the next poll re-reads.
            return {"id": ask_id, "state": "pending"}
        if not isinstance(resp, dict):
            # A hand-written response file can be any JSON shape; anything
            # but an object is a malformed contract, not a 500.
            return {"id": ask_id, "state": "error", "error": "malformed response"}
        resp.setdefault("state", "answered")
        resp["id"] = ask_id
        return resp
    if (asks_dir / f"{ask_id}.request.json").exists():
        return {"id": ask_id, "state": "pending"}
    return {"id": ask_id, "state": "unknown"}


def pending_asks(asks_dir: Path) -> list[dict]:
    """All requests without a response yet, oldest first."""
    if not asks_dir.is_dir():
        return []
    out = []
    for rf in sorted(asks_dir.glob("*.request.json")):
        try:
            req = json.loads(rf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        rid = str(req.get("id", ""))
        if not ASK_ID_RE.match(rid) or rid not in rf.name:
            continue
        if (asks_dir / f"{rid}.response.json").exists():
            continue
        out.append(req)
    return out


def write_answer(
    asks_dir: Path,
    ask_id: str,
    answer: str,
    citations: list[dict] | None = None,
    state: str = "answered",
    error: str = "",
) -> Path:
    """Write the brain's response for one ask. Used by the drain protocol
    (see AGENT.md) and by tests."""
    if not ASK_ID_RE.match(ask_id):
        raise ValueError("invalid ask id")
    resp = {
        "id": ask_id,
        "ts": _now(),
        "state": state,
        "answer": answer,
        "citations": citations or [],
    }
    if error:
        resp["error"] = error
    path = asks_dir / f"{ask_id}.response.json"
    path.write_text(json.dumps(resp, ensure_ascii=True), encoding="utf-8")
    return path
