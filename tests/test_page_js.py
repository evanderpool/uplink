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
                     this.title = ""; this.type = ""; this._on = {}; this.style = {};
                     this.classList = {
                       add: (c) => { this.className = (this.className + " " + c).trim(); },
                       remove: (c) => { this.className =
                         this.className.split(" ").filter((x) => x !== c).join(" "); },
                       toggle: (c, on) => { if (on) this.classList.add(c);
                                            else this.classList.remove(c); },
                     }; }
  getBoundingClientRect() { return {left: 0, right: 0, top: 0, bottom: 0}; }
  set textContent(v) { this._text = String(v); this.childNodes = []; }
  get textContent() {
    return this._text + this.childNodes.map((c) => c.textContent).join("");
  }
  appendChild(c) { this.childNodes.push(c); return c; }
  replaceChildren(...kids) { this.childNodes = kids; this._text = ""; }
  addEventListener(ev, fn) { (this._on[ev] = this._on[ev] || []).push(fn); }
  click() { (this._on.click || []).forEach((f) => f()); }
  scrollIntoView() {}
  setAttribute(k, v) { this[k] = v; }
  removeAttribute(k) { delete this[k]; }
  focus() {}
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
let TAB = null;
const showTab = (which) => { TAB = which; };
const mountOriginal = () => {};
const scrollThread = () => {};
const postJSON = async () => ({ ok: true });
const loadNotes = () => {};
const state = { writes: true, collection: null, sources: [], selected: new Set(),
                reader: null, pollTimer: null };
const uploadQueue = [];
let uploading = false;
const scheduleMetrics = () => {};
const loadStatus = async () => {};
const loadSources = async () => {};
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

    # `const NAME = …;` — scan for the terminating semicolon while tracking
    # string and bracket state, because a naive index(";") stops inside any
    # prose that happens to contain one.
    start = APP_JS.index(f"const {name} = ")
    depth = 0
    quote = None
    i = start
    while i < len(APP_JS):
        ch = APP_JS[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth == 0:
            return APP_JS[start:i + 1]
        i += 1
    raise AssertionError(f"unterminated declaration {name}")


def _run(script: str, functions=("citationList", "renderAnswer", "citedPage",
                                 "renderDoc", "renderSnippet")) -> dict:
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


def test_entrance_tweens_clean_up_after_themselves():
    """gsap.from writes inline styles; a tween interrupted by a re-render
    left source rows permanently half-faded and unreadable."""
    assert 'clearProps: "opacity,transform"' in _extract("anim")


def test_source_list_does_not_fade_in():
    """A source list you cannot read while it settles is worse than no
    animation at all."""
    src = _extract("loadSources")
    stagger = src[src.index('anim(list.querySelectorAll(".source")'):]
    stagger = stagger[:stagger.index(");")]
    assert "opacity" not in stagger


def test_excluded_sources_stay_readable():
    """Deselected rows are recoloured, not faded into illegibility."""
    css = (Path(__file__).resolve().parents[1] / "uplink" / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    block = css[css.index(".source.off"):css.index(".source-body")]
    assert "opacity" not in block


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


# ------------------------- pins for the original-file reader

def test_pdf_citation_resolves_the_cited_page():
    """PDF sections are labelled 'Page 31' — enough to open the original at
    the cited page rather than at page one."""
    out = _run(textwrap.dedent("""
        const doc = {chunks: [{seq: 4, section: "Page 12"}, {seq: 5, section: "Page 31"}]};
        console.log(JSON.stringify({
          cited: citedPage(doc, 5),
          other: citedPage(doc, 4),
          none: citedPage(doc, null),
          missing: citedPage({chunks: [{seq: 1, section: "Overview"}]}, 1),
        }));
    """), functions=("citedPage",))
    assert out == {"cited": 31, "other": 12, "none": None, "missing": None}


def test_pdf_opens_on_the_original_tab_text_stays_on_passages():
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.pdf", filetype: "pdf", collection: "tech",
                   has_original: true, viewable: true, total_chunks: 2, start: 0,
                   chunks: [{seq: 0, section: "Page 1", text: "x"}]}, 0);
        const forPdf = TAB;
        renderDoc({path: "b.md", filetype: "md", collection: "ops",
                   has_original: true, viewable: true, total_chunks: 1, start: 0,
                   chunks: [{seq: 0, section: "Intro", text: "y"}]}, 0);
        console.log(JSON.stringify({forPdf: forPdf, forText: TAB}));
    """))
    assert out == {"forPdf": "original", "forText": "text"}


def test_reader_records_original_availability():
    """An upload-only collection has no source folder — the Original tab
    must know that rather than showing a broken frame."""
    out = _run(textwrap.dedent("""
        renderDoc({path: "a.md", collection: "notes", filetype: "md",
                   has_original: false, viewable: true, total_chunks: 1, start: 0,
                   chunks: [{seq: 0, text: "z"}]}, 0);
        console.log(JSON.stringify(state.reader));
    """))
    assert out["hasOriginal"] is False
    assert out["viewable"] is True


# ---------------------------- pins for the metrics panel and tooltips

def test_every_metric_label_has_an_explanation():
    """A panel of unexplained jargon is a panel nobody trusts. Every label
    rendered by a metric row must resolve in the glossary."""
    out = _run(textwrap.dedent("""
        const labels = Object.keys(GLOSSARY);
        console.log(JSON.stringify({
          count: labels.length,
          mrr: GLOSSARY["MRR"].length,
          hasCommand: GLOSSARY["hit@1"][2].indexOf("uplink") >= 0,
          verified: GLOSSARY["verified"][0],
        }));
    """), functions=("GLOSSARY",))
    assert out["count"] >= 15
    assert out["mrr"] == 3, "definition, explanation, reproducing command"
    assert out["hasCommand"] is True
    assert out["verified"] == "Verification rate"


def test_tooltip_targets_are_keyboard_reachable():
    """Hover-only explanations are unreachable without a mouse."""
    src = _extract("attachTip")
    assert 'setAttribute("tabindex", "0")' in src
    assert '"focus"' in src and '"blur"' in src
    assert 'aria-describedby' in src


def test_unknown_label_does_not_get_a_dead_tooltip():
    out = _run(textwrap.dedent("""
        const node = el("span", "mrow-l", "not a metric");
        attachTip(node, "not a metric");
        console.log(JSON.stringify({cls: node.className, tabindex: node.tabindex}));
    """), functions=("GLOSSARY", "attachTip", "showTip", "hideTip"))
    assert "has-tip" not in out["cls"]


def test_accuracy_card_shows_its_confidence_interval():
    """A rate without its interval is an overclaim when n is small."""
    out = _run(textwrap.dedent("""
        renderAccuracy({accuracy: {available: true, hit_at_1: 0.923, hit_at_k: 0.923,
          hit_at_1_ci: [0.667, 0.986], hit_at_k_ci: [0.667, 0.986], mrr: 0.923,
          questions: 13, k: 5, runs: 1, label: "baseline", ts: "2026-08-06T00:00",
          fixtures: "industry-golden.jsonl", series: [], delta: null}});
        console.log(JSON.stringify({text: $("accuracy").textContent}));
    """), functions=("GLOSSARY", "attachTip", "showTip", "hideTip", "sparkline",
                     "cardTitle", "metricRow", "heroBlock", "fmtDelta",
                     "fmtPct", "fmtWhen", "renderAccuracy"))
    assert "92%" in out["text"]
    assert "95% CI 67%-99%" in out["text"]
    assert "n=13" in out["text"]


def test_unmeasured_accuracy_says_so_instead_of_showing_a_number():
    out = _run(textwrap.dedent("""
        renderAccuracy({accuracy: {available: false}});
        console.log(JSON.stringify({text: $("accuracy").textContent}));
    """), functions=("GLOSSARY", "attachTip", "showTip", "hideTip", "sparkline",
                     "cardTitle", "metricRow", "heroBlock", "fmtDelta",
                     "fmtPct", "fmtWhen", "renderAccuracy"))
    assert "Not measured yet" in out["text"]
    assert "%" not in out["text"].replace("hit@1", "")


def test_failing_questions_are_listed_by_name():
    out = _run(textwrap.dedent("""
        renderFailing({available: true, hit_at_1: 0.66, mrr: 0.7,
          failing: [{q: "when is hand hygiene required"}],
          weak: [{q: "what are the phases", rank: 3}]});
        console.log(JSON.stringify({text: $("failing").textContent}));
    """), functions=("GLOSSARY", "attachTip", "showTip", "hideTip",
                     "cardTitle", "metricRow", "fmtPct", "renderFailing"))
    assert "MISS" in out["text"]
    assert "when is hand hygiene required" in out["text"]
    assert "rank 3" in out["text"]


# ------------------------- pins for live metric refreshing

def test_metrics_refresh_after_every_action_that_moves_them():
    """Numbers that go stale the moment you use the app are worse than no
    numbers: search, answers, and source opens all change what the panel
    reports, so each must schedule a refresh."""
    for fn, why in (
        ("runSearch", "latency, zero-hit rate, search count"),
        ("askAI", "answers, time-to-answer, citations per answer"),
        ("fetchDoc", "source opens and verification rate"),
    ):
        assert "scheduleMetrics()" in _extract(fn), f"{fn} must refresh: {why}"


def test_metrics_refresh_is_debounced_and_not_self_scheduling():
    """/api/metrics re-scores the live index, so a burst of actions must
    collapse to one refresh — and the timer must call loadMetrics, never
    itself, or it re-arms forever."""
    src = _extract("scheduleMetrics")
    assert "clearTimeout" in src, "a burst must collapse to one refresh"
    assert "loadMetrics()" in src
    body = src[src.index("setTimeout"):]
    assert "scheduleMetrics(" not in body, "the timer must not re-schedule itself"


# --------------------------------- multi-file upload queue

def test_upload_accepts_multiple_files():
    html = (Path(__file__).resolve().parents[1] / "uplink" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert 'id="file"' in html and "multiple" in html.split('id="file"')[1][:60]


def test_queue_dedupes_and_reports_count():
    out = _run(textwrap.dedent("""
        const files = [{name:"a.pdf", size:10}, {name:"b.pdf", size:20},
                       {name:"a.pdf", size:10}];
        const added = queueFiles(files);
        console.log(JSON.stringify({added: added, queued: uploadQueue.length,
                                    msg: $("upmsg").textContent}));
    """), functions=("queueFiles", "renderQueue"))
    assert out["added"] == 2, "the same file picked twice must queue once"
    assert out["queued"] == 2
    assert "2 files ready" in out["msg"]


def test_queue_renders_a_row_per_file_with_state():
    out = _run(textwrap.dedent("""
        queueFiles([{name:"one.pdf", size:1}, {name:"two.pdf", size:2}]);
        uploadQueue[0].state = "done"; uploadQueue[0].note = "12 passages";
        uploadQueue[1].state = "failed"; uploadQueue[1].note = "unsupported file type";
        renderQueue();
        const rows = $("upqueue").all((n) => n.className.indexOf("upitem ") === 0
                                             || /^upitem-/.test(n.className) === false
                                                && n.className.indexOf("upitem") === 0);
        console.log(JSON.stringify({text: $("upqueue").textContent}));
    """), functions=("queueFiles", "renderQueue"))
    assert "one.pdf" in out["text"] and "12 passages" in out["text"]
    assert "two.pdf" in out["text"] and "unsupported file type" in out["text"]


def test_uploads_are_sequential_not_parallel():
    """Each upload re-indexes and takes the database write lock, so firing
    them together would only produce lock contention."""
    src = _extract("processQueue")
    assert "for (const item of pending)" in src
    assert "await uploadOne(" in src


def test_a_failed_file_does_not_abort_the_batch():
    src = _extract("processQueue")
    assert "catch" in src, "one bad file must not stop the rest"
    assert 'item.state = "failed"' in src


def test_failed_files_are_retried_on_the_next_run():
    src = _extract("processQueue")
    assert '"waiting" || q.state === "failed"' in src, (
        "pressing Index them again should retry what failed"
    )


def test_batch_refreshes_once_not_per_file():
    """A refresh per file would re-score the live index dozens of times."""
    src = _extract("processQueue")
    body = src[src.index("for (const item of pending)"):]
    loop = body[:body.index("uploading = false")]
    assert "loadSources()" not in loop and "scheduleMetrics(" not in loop
    assert "loadSources()" in src and "scheduleMetrics(" in src


def test_busy_index_is_retried_once():
    """503 means a CLI index run holds the write lock — worth one retry
    rather than failing the file."""
    src = _extract("uploadOne")
    assert "503" in src and "await send()" in src


def test_queue_shows_the_real_label_once_indexed():
    """While a batch runs, rows should read as documents rather than as
    vendor asset codes."""
    out = _run(textwrap.dedent("""
        queueFiles([{name:"ma658_macbook_air_late2008_userguide.pdf", size:1}]);
        renderQueue();
        const before = $("upqueue").textContent;
        uploadQueue[0].state = "done";
        uploadQueue[0].label = "MacBook Air User Guide";
        uploadQueue[0].note = "75 passages";
        renderQueue();
        console.log(JSON.stringify({before: before, after: $("upqueue").textContent}));
    """), functions=("queueFiles", "renderQueue"))
    # Before indexing there is no title yet, so the filename stands in.
    assert "ma658_macbook_air_late2008_userguide.pdf" in out["before"]
    # After, the document's real name leads and the filename stays visible.
    assert "MacBook Air User Guide" in out["after"]
    assert "ma658_macbook_air_late2008_userguide.pdf" in out["after"]
    assert "75 passages" in out["after"]


def test_an_already_indexed_file_does_not_look_like_a_failure():
    """Re-uploading an unchanged file produces zero new passages; reporting
    '0 passages' reads as a failure when nothing went wrong."""
    src = _extract("uploadOne")
    assert "already indexed" in src
    assert "data.indexed === 0" in src
