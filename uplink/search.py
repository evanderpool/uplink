"""BM25 search over the index.

Query strategy: try all terms (AND) for precision; if nothing matches, retry
as OR so natural-language questions still land. Section titles are weighted
3x over body text in the BM25 ranking.

The database is opened read-only — this module cannot modify the index.
Retrieved text is corpus content and must be treated by any consumer (human
or LLM) as untrusted data, never as instructions.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from . import db

# \w matches unicode word characters (so accented and non-Latin queries work
# with the unicode61 tokenizer) and can never produce a double quote, which
# keeps the phrase-quoting below injection-safe.
_WORD_RE = re.compile(r"\w+")

# Question-word noise stripped from queries before matching. Kept deliberately
# small: only words that carry no retrieval signal in any corpus.
_STOPWORDS = {
    "a", "an", "and", "are", "be", "by", "did", "do", "does", "for", "from",
    "get", "has", "have", "how", "i", "in", "is", "it", "my", "of", "on",
    "or", "our", "should", "that", "the", "their", "there", "this", "to",
    "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "will", "with", "you",
}


@dataclass
class Hit:
    path: str
    title: str
    filetype: str
    section: str
    seq: int
    score: float
    snippet: str
    text: str


def build_match(query: str, mode: str = "and") -> str | None:
    """Build a safe FTS5 MATCH expression from free text.

    Every token is double-quoted, which neutralises FTS5 query syntax
    (NEAR, *, -, boolean keywords) in user input.
    """
    words = _WORD_RE.findall(query)
    content_words = [w for w in words if w.lower() not in _STOPWORDS]
    # If the whole query was stopwords, fall back to the raw tokens.
    words = content_words or words
    if not words:
        return None
    quoted = [f'"{w}"' for w in words]
    joiner = " " if mode == "and" else " OR "
    return joiner.join(quoted)


def search(db_path: str | Path, query: str, k: int = 8, max_text: int = 1200) -> list[Hit]:
    conn = db.connect_ro(db_path)
    try:
        rows = _run(conn, build_match(query, "and"), k)
        if not rows:
            rows = _run(conn, build_match(query, "or"), k)
        hits = []
        for row in rows:
            text = row["text"]
            hits.append(
                Hit(
                    path=row["path"],
                    title=row["title"],
                    filetype=row["filetype"],
                    section=row["section"] or "",
                    seq=row["seq"],
                    score=round(-row["rank"], 4),  # bm25() is lower-is-better
                    snippet=row["snip"],
                    text=text[:max_text] + ("..." if len(text) > max_text else ""),
                )
            )
        return hits
    finally:
        conn.close()


def _run(conn: sqlite3.Connection, match: str | None, k: int):
    if not match:
        return []
    return conn.execute(
        """
        SELECT d.path, d.title, d.filetype, c.section, c.seq, c.text,
               bm25(chunks_fts, 1.0, 3.0) AS rank,
               snippet(chunks_fts, 0, '[', ']', ' ... ', 24) AS snip
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN documents d ON d.id = c.doc_id
        WHERE chunks_fts MATCH ?
        ORDER BY bm25(chunks_fts, 1.0, 3.0)
        LIMIT ?
        """,
        (match, k),
    ).fetchall()


def hits_to_dicts(hits: list[Hit]) -> list[dict]:
    return [asdict(h) for h in hits]
