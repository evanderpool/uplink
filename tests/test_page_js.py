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
                     this.className = ""; this.hidden = false; this.disabled = false; }
  set textContent(v) { this._text = String(v); this.childNodes = []; }
  get textContent() {
    return this._text + this.childNodes.map((c) => c.textContent).join("");
  }
  appendChild(c) { this.childNodes.push(c); return c; }
  replaceChildren(...kids) { this.childNodes = kids; this._text = ""; }
}
const NODES = { answer: new El("div"), ai: new El("button") };
const document = { createElement: (t) => new El(t) };
const $ = (id) => NODES[id];
let pollTimer = null;
const clearInterval = () => { CLEARED = true; };
let CLEARED = false;
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
        _extract("renderAnswer"),
        _extract("clearAnswer"),
        script,
    ])
    proc = subprocess.run(
        [NODE, "-e", body], capture_output=True, text=True, timeout=60
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


def test_search_submit_clears_the_answer_card():
    """Pin the call site: the submit handler must retire the AI answer."""
    submit = PAGE[PAGE.index('$("f").addEventListener'):]
    assert "clearAnswer();" in submit[:submit.index("$(\"go\").disabled = true")]
