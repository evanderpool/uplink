"""Uplink CLI.

    python -m uplink index  <corpus_dir> [--db PATH]
    python -m uplink search "question"   [--db PATH] [--k N] [--json]
    python -m uplink eval   <fixtures>   [--db PATH] [--k N] [--json]
    python -m uplink status              [--db PATH]

`search` and `eval` open the database read-only. Stdout is reconfigured to
replace unencodable characters so corpus content with unicode never crashes
a piped Windows (cp1252) console.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_DB = Path("data") / "index.db"


def main(argv: list[str] | None = None) -> int:
    # Piped stdout on Windows defaults to cp1252; corpus text is arbitrary
    # unicode. Replace rather than crash.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    parser = argparse.ArgumentParser(prog="uplink", description="Local document retrieval.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="Index (or refresh) a corpus folder.")
    p_index.add_argument("corpus", help="Folder containing documents.")
    p_index.add_argument("--db", default=str(DEFAULT_DB))

    p_search = sub.add_parser("search", help="Search the index (read-only).")
    p_search.add_argument("query")
    p_search.add_argument("--db", default=str(DEFAULT_DB))
    p_search.add_argument("--k", type=int, default=8)
    p_search.add_argument("--json", action="store_true", dest="as_json")

    p_eval = sub.add_parser("eval", help="Run golden-question fixtures (read-only).")
    p_eval.add_argument("fixtures")
    p_eval.add_argument("--db", default=str(DEFAULT_DB))
    p_eval.add_argument("--k", type=int, default=5)
    p_eval.add_argument("--json", action="store_true", dest="as_json")

    p_status = sub.add_parser("status", help="Show index statistics (read-only).")
    p_status.add_argument("--db", default=str(DEFAULT_DB))

    args = parser.parse_args(argv)

    try:
        if args.cmd == "index":
            return _cmd_index(args)
        if args.cmd == "search":
            return _cmd_search(args)
        if args.cmd == "eval":
            return _cmd_eval(args)
        if args.cmd == "status":
            return _cmd_status(args)
    except (OSError, ValueError) as exc:
        # Covers missing corpus/db/fixture paths, corpus-root mismatch,
        # and malformed fixture JSON — clean message, no traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


def _cmd_index(args) -> int:
    from .indexer import index_folder

    stats = index_folder(args.corpus, args.db)
    print(stats.summary())
    return 1 if stats.errors else 0


def _cmd_search(args) -> int:
    from .search import hits_to_dicts, search

    hits = search(args.db, args.query, k=args.k)
    if args.as_json:
        print(json.dumps(hits_to_dicts(hits), ensure_ascii=True, indent=2))
        return 0
    if not hits:
        print("no results")
        return 0
    for i, h in enumerate(hits, 1):
        section = f" > {h.section}" if h.section else ""
        print(f"{i}. [{h.score}] {h.path}{section}")
        print(f"   {h.snippet}")
    return 0


def _cmd_eval(args) -> int:
    from .evaluate import run_eval

    result = run_eval(args.db, args.fixtures, k=args.k)
    if args.as_json:
        print(json.dumps(result.to_dict(), ensure_ascii=True, indent=2))
    else:
        print(result.summary())
    return 0


def _cmd_status(args) -> int:
    from . import db

    conn = db.connect_ro(args.db)
    try:
        docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        chunks = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        by_type = conn.execute(
            "SELECT filetype, COUNT(*) AS n FROM documents GROUP BY filetype ORDER BY n DESC"
        ).fetchall()
        latest = conn.execute("SELECT MAX(indexed_at) AS t FROM documents").fetchone()["t"]
    finally:
        conn.close()
    print(f"documents: {docs}")
    print(f"chunks:    {chunks}")
    print(f"last indexed: {latest or 'never (index is empty)'}")
    for row in by_type:
        print(f"  .{row['filetype']}: {row['n']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
