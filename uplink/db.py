"""SQLite storage layer for Uplink.

One database holds two things: a `documents` table (one row per source file,
with a content hash for incremental re-indexing) and a `chunks` table whose
text is mirrored into an FTS5 external-content index for BM25 ranking.

Search paths open the database in read-only mode (`mode=ro` URI) so the
retrieval layer is mechanically incapable of writing — a security property,
not a convention.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    filetype    TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    title       TEXT,
    indexed_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id       INTEGER PRIMARY KEY,
    doc_id   INTEGER NOT NULL REFERENCES documents(id),
    seq      INTEGER NOT NULL,
    section  TEXT,
    text     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    section,
    content='chunks',
    content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text, section)
    VALUES (new.id, new.text, new.section);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section)
    VALUES ('delete', old.id, old.text, old.section);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text, section)
    VALUES ('delete', old.id, old.text, old.section);
    INSERT INTO chunks_fts(rowid, text, section)
    VALUES (new.id, new.text, new.section);
END;
"""


def connect_rw(db_path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the index database for writing."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


def connect_ro(db_path: str | Path) -> sqlite3.Connection:
    """Open the index database read-only. Raises if it does not exist."""
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(
            f"Index database not found: {path}. Run `python -m uplink index <folder>` first."
        )
    # as_uri() percent-encodes '#', '%', and spaces — without it, a '#' in the
    # path silently drops '?mode=ro' and the "read-only" guarantee with it.
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
