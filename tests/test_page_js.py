"""The page's answer-rendering contract, executed as real JavaScript.

The Ask AI card is the one place where brain-written JSON reaches the DOM, so
its coercion rules are worth running rather than eyeballing. Node is used
only as a test tool (it ships on both CI runners); the app itself stays
stdlib-only and dependency-free. Skipped when node is absent.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap

import pytest

from uplink.webapp import PAGE

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

# A DOM small enough to be obviously correct, strict enough to catch the bugs:
# textContent is the ONLY way text enters a node, so an innerHTML regression
# in the page would fail to render at all.
DOM_SHIM = """
class El {
  constructor(tag) { this.tag = tag; this.childNodes = []; this._text = "";
                     this.className = ""; this.hidden = false; this.disabled = false;
                     this.title = ""; this.type = ""; this._on = {}; }
  set textContent(v) { this._text = String(v); this.childNodes = []; }
  get textContent() {
    return this._text + this.childNodes.map((c) => c.textContent).join("");
  }
  appendChild(c) { this.childNodes.push(c); return c; }
  replaceChildren(...kids) { this.childNodes = kids; this._text = ""; }
  addEventListener(ev, fn) { (this._on[ev] = this._on[ev] || []).push(fn); }
  click() { (this._on.click || []).forEach((f) => f()); }
  scrollIntoView() {}
  find(pred) {
    if (pred(this)) return this;
    for (const c of this.childNodes) { const hit = c.find(pred); if (hit) return hit; }
    return null;
  }
}
const NODES = { answer: new El("div"), ai: new El("button"), viewer: new El("div") };
const document = { createElement: (t) => new El(t) };
const $ = (id) => NODES[id];
let pollTimer = null;
const clearInterval = () => { CLEARED = true; };
let CLEARED = false;
let OPENED = null;
const openDoc = (path, collection, seq) => { OPENED = {call: "openDoc", path, collection, seq}; };
const openDocAt = (path, collection, start, citedSeq) =>
  { OPENED = {call: "openDocAt", path, collection, start, citedSeq}; };
"""


def _extract(name: str) -> str:
    """Pull one top-level `function name(...) {...}` out of the page source."""
    start = PAGE.index(f"function {name}(")
    depth = 0
    for i in range(start, len(PAGE)):
        if PAGE[i] == "{":
            depth += 1
        elif PAGE[i] == "}":
            depth -= 1
            if depth == 0:
                return PAGE[start:i + 1]
    raise AssertionError(f"unterminated function {name}")


def _run(script: str) -> dict:
    body = "\n".join([
        DOM_SHIM,
        _extract("closeViewer"),
        _extract("renderDoc"),
        _extract("renderAnswer"),
        _extract("clearAnswer"),
        script,
    ])
    proc = subprocess.run(
        [NODE, "-e", body], capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",  # node emits UTF-8; Windows defaults to cp1252
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    "citations",
    ['"runbook.md"', "[null]", "[42]", '[{"section": "no path"}]', "{}", "0", "null"],
)
def test_malformed_citations_still_render_the_answer(citations: str):
    """A bad citations shape must not throw — that blanked the card and left
    the Ask button permanently disabled mid-poll."""
    out = _run(textwrap.dedent(f"""
        const resp = {{answer: "The backup window is 0200.", citations: {citations}}};
        renderAnswer(resp, "when do backups run");
        console.log(JSON.stringify({{text: $("answer").textContent,
                                     kids: $("answer").childNodes.length}}));
    """))
    assert "The backup window is 0200." in out["text"]
    assert out["kids"] == 1


def test_answer_card_is_labeled_with_its_question():
    """A cited answer must never be readable as the answer to a later query."""
    out = _run(textwrap.dedent("""
        renderAnswer({answer: "A.", citations: [{path: "p.md", section: "S"}]},
                     "what is the escalation path");
        console.log(JSON.stringify({text: $("answer").textContent}));
    """))
    assert "what is the escalation path" in out["text"]
    assert "p.md > S" in out["text"]


def test_clear_answer_cancels_poll_and_reenables_button():
    out = _run(textwrap.dedent("""
        renderAnswer({answer: "stale answer", citations: []}, "old question");
        pollTimer = 123;
        $("ai").disabled = true;
        clearAnswer();
        console.log(JSON.stringify({kids: $("answer").childNodes.length,
                                    cleared: CLEARED, timer: pollTimer,
                                    disabled: $("ai").disabled}));
    """))
    assert out == {"kids": 0, "cleared": True, "timer": None, "disabled": False}


def test_non_string_answer_is_coerced():
    out = _run(textwrap.dedent("""
        renderAnswer({answer: {oops: 1}, citations: []}, "q");
        console.log(JSON.stringify({text: $("answer").textContent}));
    """))
    assert "[object Object]" in out["text"]  # coerced, not thrown


def test_citation_chip_opens_the_cited_chunk():
    """A citation you cannot open is a claim, not evidence: the chip must
    carry the exact index coordinates through to openDoc."""
    out = _run(textwrap.dedent("""
        renderAnswer({answer: "A.", citations: [
          {path: "nist.pdf", section: "Page 31", seq: 44, collection: "tech"}]}, "q");
        const chip = $("answer").find((n) => n.tag === "button");
        chip.click();
        console.log(JSON.stringify({label: chip.textContent, opened: OPENED}));
    """))
    assert out["label"] == "nist.pdf > Page 31"
    assert out["opened"] == {"call": "openDoc", "path": "nist.pdf",
                             "collection": "tech", "seq": 44}


def test_citation_without_seq_still_opens_the_document():
    """A prose section label loses the anchor but must not lose the link."""
    out = _run(textwrap.dedent("""
        renderAnswer({answer: "A.", citations: [
          {path: "apple.txt", section: "p. 21-22"}]}, "q");
        $("answer").find((n) => n.tag === "button").click();
        console.log(JSON.stringify(OPENED));
    """))
    assert out["path"] == "apple.txt"
    assert out["seq"] is None


def test_doc_viewer_renders_chunks_and_marks_the_cited_one():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", collection: "ops", total_chunks: 30, start: 18,
                   chunks: [{seq: 18, section: "S18", text: "eighteen"},
                            {seq: 19, section: "S19", text: "nineteen"}]}, 19);
        const cited = $("viewer").find((n) => n.className.indexOf("cited") >= 0);
        console.log(JSON.stringify({all: $("viewer").textContent,
                                    cited: cited ? cited.textContent : null}));
    """))
    assert "eighteen" in out["all"] and "nineteen" in out["all"]
    assert "showing 19–20 of 30" in out["all"]
    assert "nineteen" in out["cited"] and "eighteen" not in out["cited"]


def test_doc_viewer_survives_malformed_payload():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", chunks: "not-a-list"}, null);
        renderDoc({path: "b.md", chunks: [null, 42, {seq: 1, text: "ok"}]}, null);
        console.log(JSON.stringify({text: $("viewer").textContent}));
    """))
    assert "ok" in out["text"]


def test_doc_viewer_paging_buttons_bound_correctly():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", collection: "ops", total_chunks: 30, start: 0,
                   chunks: [{seq: 0, text: "x"}, {seq: 1, text: "y"}]}, 0);
        const btns = [];
        (function walk(n) { if (n.tag === "button") btns.push(n);
                            n.childNodes.forEach(walk); })($("viewer"));
        const prev = btns.find((b) => b.textContent.indexOf("earlier") >= 0);
        const next = btns.find((b) => b.textContent.indexOf("later") >= 0);
        const before = {prevDisabled: prev.disabled, nextDisabled: next.disabled};
        next.click();
        console.log(JSON.stringify({before: before, opened: OPENED}));
    """))
    assert out["before"] == {"prevDisabled": True, "nextDisabled": False}
    # Absolute offset, not a seq to re-centre on — centring skipped chunks.
    assert out["opened"]["call"] == "openDocAt"
    assert out["opened"]["start"] == 2


def test_paging_backward_uses_absolute_start():
    """The bug: 'earlier' passed a start into the seq parameter, so the
    server re-centred and silently skipped limit/2 chunks per click."""
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", collection: "ops", total_chunks: 60, start: 36,
                   chunks: Array.from({length: 9}, (_, i) => ({seq: 36 + i, text: "t"}))}, 40);
        const btns = [];
        (function walk(n) { if (n.tag === "button") btns.push(n);
                            n.childNodes.forEach(walk); })($("viewer"));
        btns.find((b) => b.textContent.indexOf("earlier") >= 0).click();
        console.log(JSON.stringify(OPENED));
    """))
    assert out["call"] == "openDocAt"
    assert out["start"] == 27          # 36 - 9, contiguous with the window above
    assert out["citedSeq"] == 40       # the cited chunk stays highlighted


def test_citation_without_seq_does_not_falsely_highlight_chunk_zero():
    """Number(null) === 0 marked the cover page as 'the cited passage'."""
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", total_chunks: 2, start: 0,
                   chunks: [{seq: 0, section: "Cover", text: "CHUNK-ZERO"},
                            {seq: 1, section: "Body", text: "CHUNK-ONE"}]}, null);
        const cited = $("viewer").find((n) => n.className.indexOf("cited") >= 0);
        console.log(JSON.stringify({cited: cited ? cited.textContent : null}));
    """))
    assert out["cited"] is None


def test_clear_answer_also_closes_the_viewer():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", total_chunks: 1, start: 0,
                   chunks: [{seq: 0, text: "stale source"}]}, 0);
        clearAnswer();
        console.log(JSON.stringify({viewer: $("viewer").childNodes.length}));
    """))
    assert out["viewer"] == 0


def test_search_submit_clears_the_answer_card():
    """Pin the call site: the submit handler must retire the AI answer."""
    submit = PAGE[PAGE.index('$("f").addEventListener'):]
    assert "clearAnswer();" in submit[:submit.index("$(\"go\").disabled = true")]
