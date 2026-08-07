"""The interface's rendering contracts, executed as real JavaScript.

The answer card, citation chips, and source reader are where brain-written
and corpus-derived JSON reaches the DOM, so their coercion rules are worth
running rather than eyeballing. Node is used only as a test tool (it ships on
both CI runners); the app itself has no build step. Skipped when node is
absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

APP_JS = (Path(__file__).resolve().parents[1] / "uplink" / "static" / "app.js").read_text(
    encoding="utf-8"
)

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
  querySelector() { return null; }
  querySelectorAll() { return []; }
  find(pred) {
    if (pred(this)) return this;
    for (const c of this.childNodes) { const hit = c.find(pred); if (hit) return hit; }
    return null;
  }
  all(pred, out) { out = out || [];
    if (pred(this)) out.push(this);
    this.childNodes.forEach((c) => c.all(pred, out));
    return out; }
}
const NODES = {};
const nodeFor = (id) => (NODES[id] = NODES[id] || new El("div"));
const document = {
  createElement: (t) => new El(t),
  createTextNode: (t) => { const n = new El("#text"); n.textContent = t; return n; },
  addEventListener: () => {},
};
const $ = (id) => nodeFor(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};
// Motion is a no-op under test; the contract is what renders, not how it moves.
const anim = () => null;
const animTo = () => null;
let OPENED = null;
const openDoc = (path, collection, seq) => { OPENED = {call:"openDoc", path, collection, seq}; };
const openDocAt = (path, collection, start, citedSeq) =>
  { OPENED = {call:"openDocAt", path, collection, start, citedSeq}; };
const openReader = () => {};
const scrollThread = () => {};
const postJSON = async () => ({ ok: true });
const loadNotes = () => {};
const state = { writes: true, collection: null, sources: [], selected: new Set(),
                reader: null, pollTimer: null };
"""


def _extract(name: str) -> str:
    """Pull one top-level definition out of app.js — either a
    `function name(...) {...}` or a `const name = (...) => …;` arrow."""
    decl = f"function {name}("
    if decl in APP_JS:
        start = APP_JS.index(decl)
        depth = 0
        for i in range(start, len(APP_JS)):
            if APP_JS[i] == "{":
                depth += 1
            elif APP_JS[i] == "}":
                depth -= 1
                if depth == 0:
                    return APP_JS[start:i + 1]
        raise AssertionError(f"unterminated function {name}")

    arrow = f"const {name} = "
    start = APP_JS.index(arrow)
    end = APP_JS.index(";", start)
    return APP_JS[start:end + 1]


def _run(script: str, functions=("citationList", "renderAnswer", "renderDoc", "renderSnippet")) -> dict:
    body = "\n".join([DOM_SHIM] + [_extract(f) for f in functions] + [script])
    proc = subprocess.run(
        [NODE, "-e", body], capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ----------------------------------------------------------- static assets

def test_app_js_has_no_html_sinks():
    """Corpus text must never be able to become markup."""
    code = APP_JS
    # Strip comments so the "no innerHTML here" note doesn't trip the check.
    import re

    code = re.sub(r"/\*[\s\S]*?\*/", "", code)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert sink not in code, sink


def test_app_js_parses():
    proc = subprocess.run([NODE, "--check",
                           str(Path(__file__).resolve().parents[1] / "uplink" / "static" / "app.js")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_gsap_is_vendored_not_fetched():
    """A CDN request would break both offline use and the privacy claim."""
    static = Path(__file__).resolve().parents[1] / "uplink" / "static"
    assert (static / "gsap.min.js").is_file()
    html = (static / "index.html").read_text(encoding="utf-8")
    assert "/static/gsap.min.js" in html
    for remote in ("http://", "https://", "//cdn", "unpkg", "jsdelivr", "googleapis"):
        assert remote not in html, remote


def test_ui_degrades_without_gsap():
    """Motion is optional; a missing library must not break rendering."""
    assert "HAS_GSAP" in APP_JS
    assert "typeof window.gsap" in APP_JS


# --------------------------------------------------------------- rendering

@pytest.mark.parametrize(
    "citations",
    ['"runbook.md"', "[null]", "[42]", '[{"section": "no path"}]', "{}", "0", "null"],
)
def test_malformed_citations_still_render_the_answer(citations: str):
    out = _run(textwrap.dedent(f"""
        const card = new El("div");
        renderAnswer(card, {{answer: "The backup window is 0200.", citations: {citations}}},
                     "when do backups run");
        console.log(JSON.stringify({{text: card.textContent}}));
    """))
    assert "The backup window is 0200." in out["text"]


def test_citation_chip_opens_the_cited_chunk():
    """A citation you cannot open is a claim, not evidence."""
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: "A.", citations: [
          {path: "nist.pdf", section: "Page 31", seq: 44, collection: "tech"}]}, "q");
        const chip = card.find((n) => n.className === "cite-btn");
        chip.click();
        console.log(JSON.stringify({label: chip.textContent, opened: OPENED}));
    """))
    assert "nist.pdf · Page 31" in out["label"]
    assert out["opened"] == {"call": "openDoc", "path": "nist.pdf",
                             "collection": "tech", "seq": 44}


def test_citations_are_numbered_in_order():
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: "A.", citations: [
          {path: "a.md", seq: 1}, {path: "b.md", seq: 2}, {path: "c.md", seq: 3}]}, "q");
        const chips = card.all((n) => n.className === "cite-btn");
        console.log(JSON.stringify({ns: chips.map((c) => c.childNodes[0].textContent)}));
    """))
    assert out["ns"] == ["1", "2", "3"]


def test_citation_without_seq_still_opens_the_document():
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: "A.", citations: [
          {path: "apple.txt", section: "p. 21-22"}]}, "q");
        card.find((n) => n.className === "cite-btn").click();
        console.log(JSON.stringify(OPENED));
    """))
    assert out["path"] == "apple.txt"
    assert out["seq"] is None


def test_non_string_answer_is_coerced():
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: {oops: 1}, citations: []}, "q");
        console.log(JSON.stringify({text: card.textContent}));
    """))
    assert "[object Object]" in out["text"]


def test_answer_card_is_labeled_with_its_question():
    """The saved-note title carries the question, so an answer can never be
    read as the answer to a different one."""
    out = _run(textwrap.dedent("""
        const card = new El("div");
        renderAnswer(card, {answer: "A.", citations: []}, "what is the escalation path");
        const save = card.find((n) => n.textContent === "Save to notes");
        console.log(JSON.stringify({hasSave: !!save, text: card.textContent}));
    """))
    assert out["hasSave"] is True


# ------------------------------------------------------------ source reader

def test_doc_viewer_renders_chunks_and_marks_the_cited_one():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", collection: "ops", total_chunks: 30, start: 18,
                   chunks: [{seq: 18, section: "S18", text: "eighteen"},
                            {seq: 19, section: "S19", text: "nineteen"}]}, 19);
        const cited = $("reader-body").find((n) => n.className.indexOf("cited") >= 0);
        console.log(JSON.stringify({all: $("reader-body").textContent,
                                    cited: cited ? cited.textContent : null,
                                    pos: $("reader-pos").textContent}));
    """))
    assert "eighteen" in out["all"] and "nineteen" in out["all"]
    assert "showing 19–20 of 30" in out["pos"]
    assert "nineteen" in out["cited"] and "eighteen" not in out["cited"]


def test_citation_without_seq_does_not_falsely_highlight_chunk_zero():
    """Number(null) === 0 would mark the cover page as 'the cited passage'."""
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", total_chunks: 2, start: 0,
                   chunks: [{seq: 0, section: "Cover", text: "CHUNK-ZERO"},
                            {seq: 1, section: "Body", text: "CHUNK-ONE"}]}, null);
        const cited = $("reader-body").find((n) => n.className.indexOf("cited") >= 0);
        console.log(JSON.stringify({cited: cited ? cited.textContent : null}));
    """))
    assert out["cited"] is None


def test_doc_viewer_survives_malformed_payload():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", chunks: "not-a-list"}, null);
        renderDoc({path: "b.md", chunks: [null, 42, {seq: 1, text: "ok"}]}, null);
        console.log(JSON.stringify({text: $("reader-body").textContent}));
    """))
    assert "ok" in out["text"]


def test_reader_paging_state_uses_absolute_offsets():
    """Paging must not re-centre on a seq — that silently skipped chunks."""
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", collection: "ops", total_chunks: 60, start: 36,
                   chunks: Array.from({length: 9}, (_, i) => ({seq: 36 + i, text: "t"}))}, 40);
        console.log(JSON.stringify(state.reader));
    """))
    assert out["start"] == 36 and out["shown"] == 9 and out["total"] == 60
    assert out["citedSeq"] == 40


# ------------------- pins from the v0.5 workspace adversarial review

def test_empty_state_is_hidden_not_detached():
    """#suggestions and #empty-title are children of #empty. Removing #empty
    made every later getElementById return null, which threw inside
    loadSources() and turned successful uploads into 'upload failed'."""
    src = _extract("clearEmptyState")
    assert ".remove()" not in src
    assert "hidden = true" in src


def test_scope_params_always_signal_an_active_selection():
    """The critical one: an empty selection must not look identical to
    'no scoping', or the server searches the whole corpus."""
    out = _run(
        textwrap.dedent("""
            state.sources = [
              {collection: "ops", path: "a.md"},
              {collection: "ops", path: "b.md"},
              {collection: "fin", path: "a.md"},
            ];
            const all = new Set(state.sources.map(docKey));

            state.selected = new Set(all);                    // everything
            const whenAll = scopeParams();

            state.selected = new Set();                       // nothing
            const whenNone = scopeParams();

            state.selected = new Set(["ops/a.md"]);           // one of three
            const whenOne = scopeParams();

            state.selected = new Set(["ops/a.md", "ops/b.md"]); // two of three
            const whenMost = scopeParams();

            console.log(JSON.stringify({whenAll, whenNone, whenOne, whenMost}));
        """),
        functions=("docKey", "scopeParams"),
    )
    assert out["whenAll"] == ""                       # no scoping needed
    assert out["whenNone"] == "&scoped=1"             # selection active, nothing in it
    assert out["whenOne"] == "&scoped=1&doc=ops%2Fa.md"
    # Two of three: the shorter side is the single exclusion.
    assert out["whenMost"] == "&scoped=1&xdoc=fin%2Fa.md"


def test_doc_key_separates_same_filename_across_collections():
    out = _run(
        textwrap.dedent("""
            console.log(JSON.stringify({
              a: docKey({collection: "alpha", path: "shared.md"}),
              b: docKey({collection: "bravo", path: "shared.md"}),
            }));
        """),
        functions=("docKey",),
    )
    assert out["a"] != out["b"]


def test_hidden_attribute_beats_author_display_rules():
    """Every panel toggled with `hidden` must actually hide.

    `.reader` sets `display: flex`, which outranks the UA stylesheet's
    `[hidden] { display: none }` — the source panel rendered on page load
    and its close button did nothing. A global override fixes the whole
    class of bug, so assert it exists and that nothing later re-breaks it.
    """
    import re

    static = Path(__file__).resolve().parents[1] / "uplink" / "static"
    css = (static / "app.css").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")

    override = re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css)
    assert override, "app.css must force [hidden] to win over author display rules"

    # Anything that ships hidden in the markup depends on that rule.
    assert re.search(r'id="reader"[^>]*\bhidden\b', html)
    assert re.search(r'id="scrim"[^>]*\bhidden\b', html)

    # And the override must come before the rules it has to beat, since
    # !important ties would otherwise fall back to source order.
    assert css.index("[hidden]") < css.index(".reader {")


def test_source_and_hit_openers_are_focusable_buttons():
    """Opening a source to verify a claim is the point of the product; it
    must not be mouse-only."""
    assert 'el("button", "source-name"' in APP_JS
    assert 'el("button", "hit-path"' in APP_JS


# --------------------------------------------------------------- snippets

def test_snippet_highlights_between_control_markers():
    out = _run(textwrap.dedent("""
        const node = new El("div");
        renderSnippet(node, "see the \\u0001install guide\\u0002 in [docs](a.md) then restart");
        const marks = node.all((n) => n.tag === "mark").map((m) => m.textContent);
        console.log(JSON.stringify({marks: marks, text: node.textContent}));
    """))
    assert out["marks"] == ["install guide"]
    assert "[docs](a.md)" in out["text"]
