"""Web UI: serves the page, answers search/status, and stays read-only."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from uplink.indexer import index_folder
from uplink.webapp import make_handler


@pytest.fixture
def server(tmp_path: Path):
    root = tmp_path / "corpus"
    root.mkdir()
    (root / "runbook.md").write_text(
        "# Runbook\n\n## Restarts\n\nRestart the bridge with run_bridge after config changes.",
        encoding="utf-8",
    )
    db_path = tmp_path / "index.db"
    index_folder(root, db_path)

    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "health.html").write_text("<!doctype html><title>h</title>ok", encoding="utf-8")

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db_path, reports))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as resp:
        return resp.status, resp.read()


def test_index_page_serves(server):
    status, body = _get(server + "/")
    assert status == 200
    assert b"Uplink" in body
    assert b"textContent" in body  # the no-innerHTML rendering contract


def test_api_search_returns_cited_hits(server):
    status, body = _get(server + "/api/search?q=restart%20bridge&k=5")
    assert status == 200
    data = json.loads(body)
    assert data["hits"], data
    hit = data["hits"][0]
    assert hit["path"] == "runbook.md"
    assert hit["section"] == "Restarts"
    assert "score" in hit and "snippet" in hit


def test_api_search_requires_query(server):
    try:
        _get(server + "/api/search")
        raise AssertionError("expected 400")
    except urllib.error.HTTPError as exc:
        assert exc.code == 400


def test_api_search_survives_hostile_query(server):
    status, body = _get(server + '/api/search?q=%22unclosed%20NEAR(a%20b)%20*')
    assert status == 200
    json.loads(body)


def test_api_status(server):
    status, body = _get(server + "/api/status")
    data = json.loads(body)
    assert status == 200
    assert data["documents"] == 1
    assert data["reports"] == ["health.html"]


def test_reports_whitelist_blocks_traversal(server):
    for path in ("/reports/../uplink/db.py", "/reports/secret.html", "/reports/%2e%2e/x"):
        try:
            code, _ = _get(server + path)
        except urllib.error.HTTPError as exc:
            code = exc.code
        assert code == 404, path
    code, body = _get(server + "/reports/health.html")
    assert code == 200 and b"ok" in body


def test_write_methods_rejected(server):
    req = urllib.request.Request(server + "/api/search?q=x", data=b"{}", method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("expected 405")
    except urllib.error.HTTPError as exc:
        assert exc.code == 405


def test_unknown_path_404(server):
    try:
        _get(server + "/admin")
        raise AssertionError("expected 404")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404
