/* Uplink workspace.
 *
 * Rendering rule, unchanged from the first version and non-negotiable:
 * document text reaches the DOM through textContent only. There is no
 * innerHTML anywhere in this file, so corpus content can never become markup.
 *
 * GSAP drives motion only. If it fails to load, every function below still
 * works — animations are wrapped so a missing library degrades to no
 * animation rather than a broken page.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};

/* -------------------------------------------------------------- formatting */

function fmtBytes(n) {
  const b = Number(n) || 0;
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return Math.round(b / 1024) + " KB";
  return (b / (1024 * 1024)).toFixed(1) + " MB";
}
function fmtPct(x) {
  return x === null || x === undefined ? "—" : Math.round(Number(x) * 100) + "%";
}
function fmtMs(x) {
  return x === null || x === undefined ? "—" : Number(x).toFixed(1) + " ms";
}
function fmtWhen(ts) {
  return String(ts || "").slice(0, 16).replace("T", " ");
}

/* ------------------------------------------------------------------ motion */

const MOTION = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const HAS_GSAP = typeof window.gsap !== "undefined";

function anim(targets, vars) {
  if (!MOTION || !HAS_GSAP || !targets) return null;
  try { return window.gsap.from(targets, vars); } catch (e) { return null; }
}
function animTo(targets, vars) {
  if (!MOTION || !HAS_GSAP || !targets) return null;
  try { return window.gsap.to(targets, vars); } catch (e) { return null; }
}

function countUp(node, value) {
  const target = Number(value) || 0;
  if (!MOTION || !HAS_GSAP) { node.textContent = String(target); return; }
  const state = { n: Number(node.textContent.replace(/[^0-9]/g, "")) || 0 };
  window.gsap.to(state, {
    n: target, duration: 0.8, ease: "power2.out",
    onUpdate: () => { node.textContent = String(Math.round(state.n)); },
  });
}

/* ------------------------------------------------------------------- state */

const state = {
  collection: "",
  sources: [],          // [{path, title, chunks, filetype, collection}]
  selected: new Set(),  // docKey()s currently checked
  writes: false,
  reports: [],
  pollTimer: null,
  reader: null,         // {path, collection, citedSeq, start, shown, total}
  returnFocus: null,    // element to restore when the reader closes
};

/* --------------------------------------------------------------- API calls */

async function api(path, opts) {
  const r = await fetch(path, opts);
  const data = await r.json().catch(() => ({ error: "bad response" }));
  if (!r.ok && !data.error) data.error = "request failed (" + r.status + ")";
  return data;
}

const postJSON = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Uplink": "1" },
    body: JSON.stringify(body),
  });

/* ------------------------------------------------------------------ status */

async function loadStatus() {
  const s = await api("/api/status");
  if (s.error) { $("index-meta").textContent = "index unavailable"; return; }
  state.writes = !!s.writes;
  state.reports = Array.isArray(s.reports) ? s.reports : [];

  countUp($("stat-docs"), s.documents);
  countUp($("stat-chunks"), s.chunks);
  $("index-meta").textContent =
    "last indexed " + (s.last_indexed || "never") +
    (state.writes ? " · writes enabled (localhost)" : " · read-only bind");

  const pill = $("privacy-pill");
  $("privacy-text").textContent = state.writes ? "local & private" : "read-only";
  pill.classList.toggle("pill-warn", !state.writes);

  // Write-gated affordances appear only when the server actually accepts them.
  $("add-source").hidden = !state.writes;
  $("ai").hidden = !state.writes;

  const sel = $("coll");
  const keep = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  (s.collections || []).forEach((c) => {
    const o = document.createElement("option");
    o.value = c.name;
    o.textContent = c.name + " (" + c.documents + ")";
    sel.appendChild(o);
  });
  sel.value = keep;

  const links = $("report-links");
  links.replaceChildren();
  if (!state.reports.length) {
    links.appendChild(el("p", "micro", "Run `uplink report all` to generate reports."));
  } else {
    state.reports.forEach((name) => {
      const a = document.createElement("a");
      a.href = "/reports/" + name;
      a.textContent = name.replace(".html", "");
      links.appendChild(a);
    });
  }
}

/* ----------------------------------------------------------------- sources */

// A document is (collection, path): the schema keys them that way and the
// same filename legitimately exists in several collections. Keying the
// selection on path alone made one checkbox silently control two documents.
const docKey = (src) => String(src.collection || "") + "/" + String(src.path || "");

async function loadSources() {
  const q = state.collection ? "?collection=" + encodeURIComponent(state.collection) : "";
  const data = await api("/api/sources" + q);
  const list = $("source-list");
  list.replaceChildren();
  if (data.error) {
    list.appendChild(el("p", "micro", "sources unavailable"));
    return;
  }
  state.sources = data.sources || [];
  state.selected = new Set(state.sources.map(docKey));

  countUp($("source-count"), state.sources.length);

  if (!state.sources.length) {
    list.appendChild(el("p", "micro",
      state.writes ? "No documents yet — add one below."
                   : "No documents in this collection."));
  }

  state.sources.forEach((src) => {
    const row = el("div", "source");

    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = true;
    box.setAttribute("aria-label", "Include " + src.path);
    const key = docKey(src);
    box.addEventListener("change", () => {
      if (box.checked) state.selected.add(key);
      else state.selected.delete(key);
      row.classList.toggle("off", !box.checked);
      syncScope();
    });
    row.appendChild(box);

    const body = el("div", "source-body");
    const name = el("button", "source-name", src.label || src.title || src.path);
    name.type = "button";
    name.title = "Open " + src.path;
    name.addEventListener("click", () => openDoc(src.path, src.collection, null));
    body.appendChild(name);

    // The real filename stays visible under the readable label: the label
    // is for finding it, the filename is for citing it.
    body.appendChild(el("div", "source-file", src.filename || src.path));

    const sub = el("div", "source-sub");
    sub.appendChild(el("span", "ftype", (src.filetype || "doc").toUpperCase()));
    sub.appendChild(el("span", null, fmtBytes(src.size)));
    sub.appendChild(el("span", null, (src.chunks || 0) + " passages"));
    if (!state.collection && src.collection) {
      sub.appendChild(el("span", "tag-mini", src.collection));
    }
    body.appendChild(sub);
    row.appendChild(body);
    list.appendChild(row);
  });

  $("select-all").checked = true;
  syncScope();
  renderSuggestions(data.suggestions || []);

  anim(list.querySelectorAll(".source"),
       { opacity: 0, y: 14, duration: 0.42, stagger: 0.035, ease: "power3.out" });
}

function syncScope() {
  const total = state.sources.length;
  const on = state.selected.size;
  const note = $("scope-note");
  $("select-all-label").textContent =
    on === total ? "All sources" : on + " of " + total + " selected";
  if (on === total || total === 0) {
    note.hidden = true;
  } else {
    note.hidden = false;
    note.textContent = on === 0
      ? "No sources selected — searches will return nothing."
      : "Retrieval is limited to " + on + " of " + total + " documents.";
  }
}

$("select-all").addEventListener("change", (ev) => {
  const on = ev.target.checked;
  state.selected = on ? new Set(state.sources.map((s) => s.path)) : new Set();
  $("source-list").querySelectorAll(".source").forEach((row) => {
    const box = row.querySelector("input[type=checkbox]");
    if (box) box.checked = on;
    row.classList.toggle("off", !on);
  });
  syncScope();
});

function scopeParams() {
  // Nothing to scope against yet, or everything is selected: search all.
  if (!state.sources.length || state.selected.size === state.sources.length) return "";

  const chosen = Array.from(state.selected);
  const dropped = state.sources.map(docKey).filter((k) => !state.selected.has(k));

  // `scoped=1` must always ride along: without it an EMPTY selection looks
  // identical to "no scoping" and the server would search everything —
  // the exact opposite of what the checkboxes promise.
  if (dropped.length && dropped.length < chosen.length) {
    // Fewer exclusions than inclusions: send the short side, which keeps the
    // request line sane on large corpora.
    return "&scoped=1" + dropped.map((k) => "&xdoc=" + encodeURIComponent(k)).join("");
  }
  return "&scoped=1" + chosen.map((k) => "&doc=" + encodeURIComponent(k)).join("");
}

/* ------------------------------------------------------------- suggestions */

function renderSuggestions(list) {
  const box = $("suggestions");
  box.replaceChildren();
  (Array.isArray(list) ? list : []).forEach((q) => {
    const b = el("button", "chip", q);
    b.type = "button";
    b.addEventListener("click", () => {
      $("q").value = q;
      if (state.writes) askAI(); else runSearch();
    });
    box.appendChild(b);
  });
  const title = $("empty-title");
  title.textContent = state.collection
    ? "Ask the " + state.collection + " collection"
    : "Ask your documents anything";
  anim(box.querySelectorAll(".chip"),
       { opacity: 0, y: 12, scale: 0.94, duration: 0.4, stagger: 0.05, ease: "back.out(1.6)" });
}

/* --------------------------------------------------------- chat history */

// The thread survives a reload. Stored locally in this browser (never sent
// anywhere) and capped, so a long working session cannot fill the quota.
const HISTORY_KEY = "uplink-thread-v1";
const HISTORY_MAX = 40;

function loadHistory() {
  try {
    const raw = localStorage.getItem(HISTORY_KEY);
    const rows = raw ? JSON.parse(raw) : [];
    return Array.isArray(rows) ? rows.slice(-HISTORY_MAX) : [];
  } catch (e) { return []; }
}

function saveHistory(rows) {
  try {
    localStorage.setItem(HISTORY_KEY, JSON.stringify(rows.slice(-HISTORY_MAX)));
  } catch (e) { /* quota or private mode: history is a convenience, not state */ }
}

function recordTurn(entry) {
  const rows = loadHistory();
  rows.push(entry);
  saveHistory(rows);
}

function restoreHistory() {
  const rows = loadHistory();
  if (!rows.length) return;
  rows.forEach((row) => {
    if (!row || typeof row !== "object" || !row.q) return;
    const turn = addTurn(String(row.q), true);
    const card = el("div", "card");
    turn.appendChild(card);
    if (row.kind === "answer" && row.answer) {
      renderAnswer(card, { answer: row.answer, citations: row.citations }, String(row.q));
    } else if (row.kind === "search" && Array.isArray(row.hits)) {
      renderHits(card, { hits: row.hits, latency_ms: row.latency_ms || 0 }, String(row.q));
    } else {
      card.appendChild(el("p", "micro", "(result not stored)"));
    }
  });
  $("thread").appendChild(historyDivider());
  scrollThread();
}

function historyDivider() {
  const d = el("div", "divider");
  d.appendChild(el("span", null, "earlier in this session"));
  return d;
}

function clearHistory() {
  try { localStorage.removeItem(HISTORY_KEY); } catch (e) { /* ignore */ }
  $("thread").querySelectorAll(".turn, .divider").forEach((n) => n.remove());
  const empty = $("empty");
  if (empty) empty.hidden = false;
}

/* -------------------------------------------------------------- the thread */

function clearEmptyState() {
  // Hide, never remove: #suggestions and #empty-title are its children, and
  // detaching them makes every later getElementById return null — which
  // threw inside loadSources() and turned successful uploads into
  // "upload failed".
  const empty = $("empty");
  if (empty) empty.hidden = true;
}

function addTurn(question, restoring) {
  clearEmptyState();
  const turn = el("div", "turn");
  turn.appendChild(el("div", "bubble-q", question));
  $("thread").appendChild(turn);
  anim(turn.querySelector(".bubble-q"),
       { opacity: 0, y: 10, scale: 0.96, duration: 0.36, ease: "back.out(1.5)" });
  scrollThread();
  return turn;
}

function scrollThread() {
  const t = $("thread");
  t.scrollTop = t.scrollHeight;
}

function waitingCard(turn, text) {
  const card = el("div", "card");
  const w = el("div", "waiting");
  w.appendChild(el("span", "pulse"));
  w.appendChild(el("span", null, text));
  card.appendChild(w);
  turn.appendChild(card);
  animTo(card.querySelector(".pulse"),
         { scale: 1.6, opacity: 0.35, duration: 0.7, repeat: -1, yoyo: true, ease: "sine.inOut" });
  scrollThread();
  return card;
}

/* --------------------------------------------------------------- rendering */

function citationList(container, citations, onOpen) {
  const list = Array.isArray(citations) ? citations : [];
  if (!list.length) return;
  const wrap = el("div", "cites");
  let n = 0;
  list.forEach((c) => {
    if (!c || typeof c !== "object") return;
    const path = String(c.path || "");
    if (!path) return;
    n += 1;
    const b = el("button", "cite-btn");
    b.type = "button";
    b.appendChild(el("span", "n", n));
    b.appendChild(el("span", null, path + (c.section ? " · " + String(c.section) : "")));
    b.title = "Open this passage";
    const seq = Number.isInteger(c.seq) ? c.seq : null;
    b.addEventListener("click", () => onOpen(path, c.collection || null, seq));
    wrap.appendChild(b);
  });
  if (n) {
    container.appendChild(wrap);
    anim(wrap.querySelectorAll(".cite-btn"),
         { opacity: 0, scale: 0.8, duration: 0.34, stagger: 0.04, ease: "back.out(2)" });
  }
}

function renderAnswer(card, resp, question) {
  card.replaceChildren();
  const label = el("div", "card-label");
  label.appendChild(el("span", "spark"));
  label.appendChild(el("span", null, "answer from your documents"));
  card.appendChild(label);
  card.appendChild(el("div", "answer-text", String(resp.answer || "")));
  citationList(card, resp.citations, openDoc);

  const actions = el("div", "card-actions");
  const save = el("button", "mini", "Save to notes");
  save.type = "button";
  save.addEventListener("click", async () => {
    save.disabled = true;
    const out = await postJSON("/api/notes", {
      title: question,
      body: String(resp.answer || ""),
      citations: resp.citations,
      collection: state.collection || null,
    });
    save.textContent = out.error ? "could not save" : "Saved ✓";
    if (!out.error) { save.classList.add("on-good"); loadNotes(); }
  });
  actions.appendChild(save);
  card.appendChild(actions);

  anim(card, { opacity: 0, y: 16, duration: 0.45, ease: "power3.out" });
  scrollThread();
}

function renderHits(card, data, question) {
  card.replaceChildren();
  const label = el("div", "card-label");
  label.appendChild(el("span", "spark"));
  label.appendChild(el("span", null,
    data.hits.length + " passages · " + data.latency_ms + " ms"));
  card.appendChild(label);

  data.hits.forEach((h) => {
    const hit = el("div", "hit");
    const head = el("div", "hit-head");
    const p = el("button", "hit-path", h.path);
    p.type = "button";
    p.title = "Open this passage";
    p.addEventListener("click", () => openDoc(h.path, h.collection, h.seq));
    head.appendChild(p);
    if (h.collection) head.appendChild(el("span", "tag", h.collection));
    if (h.section) head.appendChild(el("span", "micro", h.section));
    head.appendChild(el("span", "hit-score", Number(h.score).toFixed(2)));
    hit.appendChild(head);

    const snip = el("div", "snippet");
    renderSnippet(snip, String(h.snippet || ""));
    hit.appendChild(snip);

    const actions = el("div", "card-actions");
    const up = el("button", "mini", "👍 helpful");
    const down = el("button", "mini", "👎 off-target");
    if (state.writes) {
      up.addEventListener("click", () => vote(h, "up", up, down, question));
      down.addEventListener("click", () => vote(h, "down", down, up, question));
      actions.appendChild(up);
      actions.appendChild(down);
      hit.appendChild(actions);
    }
    card.appendChild(hit);
  });

  anim(card.querySelectorAll(".hit"),
       { opacity: 0, y: 14, duration: 0.4, stagger: 0.05, ease: "power3.out" });
  scrollThread();
}

function renderSnippet(node, snippet) {
  // Matches arrive delimited by U+0001 / U+0002 - — non-printable, so corpus
  // text containing [ ] survives intact.
  snippet.split(/[\u0001\u0002]/).forEach((part, i) => {
    if (!part) return;
    if (i % 2 === 1) node.appendChild(el("mark", null, part));
    else node.appendChild(document.createTextNode(part));
  });
}

async function vote(hit, verdict, btn, other, question) {
  const out = await postJSON("/api/feedback", {
    q: question, path: hit.path, seq: hit.seq,
    vote: verdict, collection: hit.collection,
  });
  if (!out.error) {
    btn.className = "mini " + (verdict === "up" ? "on-good" : "on-bad");
    other.className = "mini";
    anim(btn, { scale: 0.82, duration: 0.3, ease: "back.out(3)" });
  }
}

/* ------------------------------------------------------------ search / ask */

async function runSearch() {
  const q = $("q").value.trim();
  if (!q) return;
  const turn = addTurn(q);
  const card = waitingCard(turn, "searching…");
  $("go").disabled = true;
  $("meta").textContent = "";
  try {
    const url = "/api/search?q=" + encodeURIComponent(q) + "&k=8" +
      (state.collection ? "&collection=" + encodeURIComponent(state.collection) : "") +
      scopeParams();
    const data = await api(url);
    if (data.error) {
      card.replaceChildren(el("p", "err", "error: " + data.error));
    } else if (!data.hits || !data.hits.length) {
      card.replaceChildren(el("p", "micro",
        "Nothing matched. Try fewer or different words" +
        (state.selected.size < state.sources.length ? " — or re-select sources." : ".")));
    } else {
      renderHits(card, data, q);
      recordTurn({
        kind: "search", q: q, ts: Date.now(),
        latency_ms: data.latency_ms,
        // Keep the thread light: the citation coordinates are what make a
        // restored turn still clickable.
        hits: data.hits.slice(0, 8).map((h) => ({
          path: h.path, section: h.section, seq: h.seq,
          collection: h.collection, score: h.score, snippet: h.snippet,
        })),
      });
    }
  } catch (e) {
    card.replaceChildren(el("p", "err", "search failed — is the server running?"));
  } finally {
    $("go").disabled = false;
    $("q").value = "";
  }
}

async function askAI() {
  const q = $("q").value.trim();
  if (!q) return;
  const turn = addTurn(q);
  const card = waitingCard(turn, "queued — waiting for the brain session…");
  $("ai").disabled = true;
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  $("q").value = "";
  try {
    const started = await postJSON("/api/ask", {
      q: q, collection: state.collection || null,
    });
    if (started.error) {
      card.replaceChildren(el("p", "err", "error: " + started.error));
      $("ai").disabled = false;
      return;
    }
    const t0 = Date.now();
    state.pollTimer = setInterval(async () => {
      const secs = Math.round((Date.now() - t0) / 1000);
      let st;
      try { st = await api("/api/ask/" + started.id); } catch (e) { return; }
      if (st.state === "answered") {
        clearInterval(state.pollTimer); state.pollTimer = null;
        try {
          renderAnswer(card, st, q);
          recordTurn({ kind: "answer", q: q, ts: Date.now(),
                       answer: st.answer, citations: st.citations });
        } finally { $("ai").disabled = false; }
      } else if (st.state === "error") {
        clearInterval(state.pollTimer); state.pollTimer = null;
        card.replaceChildren(el("p", "err", "brain error: " + String(st.error || "unknown")));
        $("ai").disabled = false;
      } else if (secs > 180) {
        clearInterval(state.pollTimer); state.pollTimer = null;
        card.replaceChildren(el("p", "micro",
          "No answer after 3 minutes — is a brain session running with its watcher armed?"));
        $("ai").disabled = false;
      } else {
        const w = card.querySelector(".waiting span:last-child");
        if (w) w.textContent = "waiting for the brain session… " + secs + "s";
      }
    }, 2000);
  } catch (e) {
    card.replaceChildren(el("p", "err", "could not reach the server"));
    $("ai").disabled = false;
  }
}

$("composer").addEventListener("submit", (ev) => { ev.preventDefault(); runSearch(); });
$("ai").addEventListener("click", askAI);
$("clear-thread").addEventListener("click", clearHistory);

/* ------------------------------------------------------------ source reader */

function openReader() {
  state.returnFocus = document.activeElement;
  $("reader").hidden = false;
  $("scrim").hidden = false;
  $("reader-close").focus();
  anim($("reader"), { x: 60, opacity: 0, duration: 0.42, ease: "power3.out" });
  anim($("scrim"), { opacity: 0, duration: 0.3 });
}
function closeReader() {
  $("reader").hidden = true;
  $("scrim").hidden = true;
  state.reader = null;
  // Send focus back where it came from, so keyboard users are not dumped
  // at the top of the document after checking a citation.
  const back = state.returnFocus;
  state.returnFocus = null;
  if (back && typeof back.focus === "function") back.focus();
}
$("reader-close").addEventListener("click", closeReader);
$("tab-original").addEventListener("click", () => showTab("original"));
$("tab-text").addEventListener("click", () => showTab("text"));
$("scrim").addEventListener("click", closeReader);
document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape" && !$("reader").hidden) closeReader();
});

async function fetchDoc(path, collection, params, citedSeq) {
  openReader();
  $("reader-path").textContent = String(path);
  $("reader-meta").textContent = "loading…";
  $("reader-body").replaceChildren();
  let url = "/api/doc?path=" + encodeURIComponent(path);
  if (collection) url += "&collection=" + encodeURIComponent(collection);
  url += params;
  const doc = await api(url);
  if (doc.error) {
    let msg = String(doc.error);
    if (Array.isArray(doc.collections) && doc.collections.length) {
      msg += " (" + doc.collections.map(String).join(", ") + ")";
    }
    $("reader-meta").textContent = msg;
    return;
  }
  renderDoc(doc, citedSeq);
}

function openDoc(path, collection, seq) {
  const anchored = Number.isInteger(seq);
  return fetchDoc(path, collection,
    anchored ? "&seq=" + encodeURIComponent(seq) : "",
    anchored ? seq : null);
}
function openDocAt(path, collection, start, citedSeq) {
  return fetchDoc(path, collection, "&start=" + encodeURIComponent(start), citedSeq);
}

function citedPage(doc, citedSeq) {
  // PDF sections are labelled "Page 31" — enough to open the original at
  // the cited page rather than at page one.
  if (!Number.isInteger(citedSeq)) return null;
  const chunks = Array.isArray(doc.chunks) ? doc.chunks : [];
  const hit = chunks.find((c) => c && Number(c.seq) === Number(citedSeq));
  const m = hit && /page\s*(\d+)/i.exec(String(hit.section || ""));
  return m ? Number(m[1]) : null;
}

function showTab(which) {
  const original = which === "original";
  $("reader-frame-wrap").hidden = !original;
  $("reader-body").hidden = original;
  $("reader-nav").hidden = original;
  $("tab-original").className = "tab" + (original ? " is-on" : "");
  $("tab-text").className = "tab" + (original ? "" : " is-on");
  if (original && state.reader) mountOriginal();
}

function mountOriginal() {
  const r = state.reader;
  if (!r) return;
  const frame = $("reader-frame");
  const note = $("reader-frame-note");
  if (!r.hasOriginal) {
    frame.removeAttribute("src");
    note.textContent =
      "This collection has no source folder on disk (uploaded documents), " +
      "so the original file cannot be shown. The indexed text is complete.";
    return;
  }
  let src = "/api/file?collection=" + encodeURIComponent(r.collection) +
            "&path=" + encodeURIComponent(r.path);
  if (r.viewable) {
    if (r.page) src += "#page=" + r.page;
    frame.src = src;
    note.textContent = r.page
      ? "Original file, opened at page " + r.page + "."
      : "Original file as stored on disk.";
  } else {
    frame.removeAttribute("src");
    note.textContent =
      "Browsers cannot display this file type inline — use Download to open " +
      "it in its own application. The indexed text tab shows its contents.";
  }
}

function renderDoc(doc, citedSeq) {
  const total = Number(doc.total_chunks) || 0;
  const start = Number(doc.start) || 0;
  const chunks = Array.isArray(doc.chunks) ? doc.chunks : [];

  state.reader = {
    path: doc.path, collection: doc.collection,
    citedSeq: citedSeq, start: start, shown: chunks.length, total: total,
    viewable: !!doc.viewable, hasOriginal: !!doc.has_original,
    page: citedPage(doc, citedSeq),
  };

  $("reader-path").textContent = String(doc.label || doc.path || "");
  $("reader-meta").textContent =
    String(doc.path || "") + " · " + String(doc.collection || "") +
    " · " + total + " passages";

  const dl = $("reader-download");
  if (doc.has_original) {
    dl.hidden = false;
    dl.href = "/api/file?collection=" + encodeURIComponent(doc.collection) +
              "&path=" + encodeURIComponent(doc.path);
  } else {
    dl.hidden = true;
    dl.removeAttribute("href");
  }
  $("tab-original").disabled = !doc.has_original;

  const body = $("reader-body");
  body.replaceChildren();
  chunks.forEach((c) => {
    if (!c || typeof c !== "object") return;
    const cited = Number.isInteger(citedSeq) && Number(c.seq) === Number(citedSeq);
    const div = el("div", "chunk" + (cited ? " cited" : ""));
    div.appendChild(el("div", "chunk-head",
      "#" + String(c.seq) + (c.section ? " · " + String(c.section) : "") +
      (cited ? "  — cited" : "")));
    div.appendChild(el("div", "chunk-text", String(c.text || "")));
    body.appendChild(div);
  });

  $("reader-pos").textContent = chunks.length
    ? "showing " + (start + 1) + "–" + (start + chunks.length) + " of " + total
    : "no text";
  $("reader-prev").disabled = start <= 0;
  $("reader-next").disabled = start + chunks.length >= total;

  // A PDF is much more readable as itself; text-shaped files read fine as
  // indexed passages and keep the cited highlight.
  showTab(doc.has_original && doc.viewable && doc.filetype === "pdf"
          ? "original" : "text");

  const anchor = body.querySelector(".chunk.cited");
  if (anchor) anchor.scrollIntoView({ block: "center" });
  anim(body.querySelectorAll(".chunk"),
       { opacity: 0, y: 10, duration: 0.34, stagger: 0.03, ease: "power2.out" });
}

$("reader-prev").addEventListener("click", () => {
  const r = state.reader;
  if (!r) return;
  openDocAt(r.path, r.collection, Math.max(0, r.start - r.shown), r.citedSeq);
});
$("reader-next").addEventListener("click", () => {
  const r = state.reader;
  if (!r) return;
  openDocAt(r.path, r.collection, r.start + r.shown, r.citedSeq);
});

/* ----------------------------------------------------------------- metrics */

// A sparkline built from SVG DOM nodes — no library, no innerHTML.
function sparkline(values, width, height) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 " + width + " " + height);
  svg.setAttribute("class", "spark-svg");
  svg.setAttribute("aria-hidden", "true");
  const nums = (values || []).map(Number).filter((n) => !Number.isNaN(n));
  if (nums.length < 2) return svg;
  const lo = Math.min(...nums), hi = Math.max(...nums);
  const span = hi - lo || 1;
  const step = width / (nums.length - 1);
  const pts = nums.map((v, i) =>
    [i * step, height - ((v - lo) / span) * (height - 4) - 2]);
  const path = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  path.setAttribute("points", pts.map((p) => p[0].toFixed(1) + "," + p[1].toFixed(1)).join(" "));
  path.setAttribute("class", "spark-line");
  svg.appendChild(path);
  const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  dot.setAttribute("cx", pts[pts.length - 1][0].toFixed(1));
  dot.setAttribute("cy", pts[pts.length - 1][1].toFixed(1));
  dot.setAttribute("r", "2.6");
  dot.setAttribute("class", "spark-dot");
  svg.appendChild(dot);
  return svg;
}

function metricRow(label, value, tone) {
  const row = el("div", "mrow");
  row.appendChild(el("span", "mrow-l", label));
  row.appendChild(el("span", "mrow-v" + (tone ? " " + tone : ""), value));
  return row;
}

async function loadMetrics() {
  const m = await api("/api/metrics");
  if (m.error) return;

  // ---- accuracy: the honest one. Missing means missing.
  const acc = $("accuracy");
  acc.replaceChildren();
  if (!m.accuracy || !m.accuracy.available) {
    acc.appendChild(el("p", "micro",
      "No eval runs logged yet. Run `uplink eval fixtures/golden.jsonl --log` " +
      "to record a measured baseline."));
  } else {
    const a = m.accuracy;
    const big = el("div", "metric-hero");
    big.appendChild(el("span", "metric-hero-n", fmtPct(a.hit_at_1)));
    big.appendChild(el("span", "metric-hero-l", "hit@1"));
    acc.appendChild(big);
    acc.appendChild(metricRow("hit@" + a.k, fmtPct(a.hit_at_k)));
    acc.appendChild(metricRow("MRR", Number(a.mrr).toFixed(3)));
    acc.appendChild(metricRow("questions", String(a.questions)));
    if (a.series && a.series.length > 1) {
      const wrap = el("div", "spark-wrap");
      wrap.appendChild(sparkline(a.series.map((s) => s.mrr), 200, 30));
      wrap.appendChild(el("span", "micro", "MRR over " + a.runs + " logged runs"));
      acc.appendChild(wrap);
    }
    acc.appendChild(el("p", "micro",
      (a.label || "unlabeled run") + " · " + fmtWhen(a.ts) +
      (a.fixtures ? " · " + a.fixtures : "")));
  }

  // ---- performance
  const perf = $("performance");
  perf.replaceChildren();
  const p = m.performance || {};
  perf.appendChild(metricRow("median latency", fmtMs(p.median_ms), "good"));
  perf.appendChild(metricRow("p95 latency", fmtMs(p.p95_ms)));
  perf.appendChild(metricRow("searches logged", String(p.searches || 0)));
  perf.appendChild(metricRow("sources opened", String(p.source_opens || 0)));
  perf.appendChild(metricRow("zero-hit rate", fmtPct(p.zero_hit_rate),
                             (p.zero_hit_rate || 0) > 0.25 ? "warn" : ""));
  if (p.recent && p.recent.length > 1) {
    const wrap = el("div", "spark-wrap");
    wrap.appendChild(sparkline(p.recent.map((r) => r.latency_ms), 200, 26));
    wrap.appendChild(el("span", "micro", "latency, last " + p.recent.length + " searches"));
    perf.appendChild(wrap);
  }

  // ---- human feedback loop
  const fb = $("feedback-metrics");
  fb.replaceChildren();
  const f = m.feedback || {};
  if (!f.total) {
    fb.appendChild(el("p", "micro",
      "No votes yet. Rate a result 👍/👎 — upvotes become eval fixtures via " +
      "`uplink promote`, which is how the system improves itself."));
  } else {
    fb.appendChild(metricRow("helpful", String(f.up), "good"));
    fb.appendChild(metricRow("off-target", String(f.down), f.down ? "warn" : ""));
    fb.appendChild(metricRow("helpful rate", fmtPct(f.helpful_rate)));
    fb.appendChild(metricRow("promoted to fixtures", String(f.promoted_fixtures)));
    if (f.pending_promotion) {
      fb.appendChild(metricRow("awaiting promotion", String(f.pending_promotion), "warn"));
    }
    (f.by_document || []).forEach((d) => {
      const row = el("div", "mrow mrow-sub");
      row.appendChild(el("span", "mrow-l", d.path));
      row.appendChild(el("span", "mrow-v", "+" + d.up + " / -" + d.down));
      fb.appendChild(row);
    });
  }

  // ---- corpus health
  const h = $("health-metrics");
  h.replaceChildren();
  const hh = m.health || {};
  h.appendChild(metricRow("corpus size", fmtBytes(hh.bytes)));
  h.appendChild(metricRow("avg passages/doc", String(hh.avg_chunks_per_doc || 0)));
  if (hh.documents_without_chunks) {
    h.appendChild(metricRow("unsearchable docs",
                            String(hh.documents_without_chunks), "warn"));
  }
  (hh.by_type || []).forEach((r) => {
    h.appendChild(metricRow("." + r.filetype, String(r.docs) + " docs"));
  });
  h.appendChild(el("p", "micro", "last indexed " + (hh.last_indexed || "never")));
}

/* ------------------------------------------------------------------- notes */

async function loadNotes() {
  const q = state.collection ? "?collection=" + encodeURIComponent(state.collection) : "";
  const data = await api("/api/notes" + q);
  const box = $("notes");
  box.replaceChildren();
  const notes = (data && Array.isArray(data.notes)) ? data.notes : [];
  countUp($("note-count"), notes.length);
  $("notes-empty").hidden = notes.length > 0;

  notes.forEach((n) => {
    const card = el("div", "note");
    card.appendChild(el("div", "note-title", n.title || "(untitled)"));
    card.appendChild(el("div", "note-body", n.body || ""));
    const foot = el("div", "note-foot");
    foot.appendChild(el("span", "micro", String(n.ts || "").slice(0, 16).replace("T", " ")));
    const cites = Array.isArray(n.citations) ? n.citations.length : 0;
    if (cites) {
      const open = el("button", "mini", cites + " source" + (cites > 1 ? "s" : ""));
      open.type = "button";
      open.addEventListener("click", () => {
        const c = n.citations[0];
        openDoc(String(c.path), c.collection || null,
                Number.isInteger(c.seq) ? c.seq : null);
      });
      foot.appendChild(open);
    }
    if (state.writes) {
      const del = el("button", "note-del", "remove");
      del.type = "button";
      del.addEventListener("click", async () => {
        await postJSON("/api/notes/delete", { id: n.id });
        animTo(card, { opacity: 0, x: 20, height: 0, duration: 0.28,
                       ease: "power2.in", onComplete: loadNotes }) || loadNotes();
      });
      foot.appendChild(del);
    }
    card.appendChild(foot);
    box.appendChild(card);
  });
  anim(box.querySelectorAll(".note"),
       { opacity: 0, y: 10, duration: 0.36, stagger: 0.04, ease: "power3.out" });
}

/* ------------------------------------------------------------------ upload */

$("add-source").addEventListener("click", () => {
  const form = $("upload-form");
  form.hidden = !form.hidden;
  if (!form.hidden) anim(form, { opacity: 0, y: -8, duration: 0.32, ease: "power2.out" });
});

$("drop").addEventListener("dragover", (ev) => {
  ev.preventDefault();
  $("drop").classList.add("hot");
});
$("drop").addEventListener("dragleave", () => $("drop").classList.remove("hot"));
$("drop").addEventListener("drop", (ev) => {
  ev.preventDefault();
  $("drop").classList.remove("hot");
  if (ev.dataTransfer && ev.dataTransfer.files.length) {
    $("file").files = ev.dataTransfer.files;
    $("upmsg").textContent = ev.dataTransfer.files[0].name + " ready — press Index it";
  }
});
$("file").addEventListener("change", () => {
  const f = $("file").files[0];
  $("upmsg").textContent = f ? f.name + " ready — press Index it" : "";
});

$("upgo").addEventListener("click", async () => {
  const f = $("file").files[0];
  if (!f) { $("upmsg").textContent = "choose a file first"; return; }
  const fd = new FormData();
  fd.append("collection", $("upcoll").value.trim() || state.collection || "main");
  fd.append("file", f);
  $("upgo").disabled = true;
  $("upmsg").textContent = "indexing…";
  try {
    const r = await fetch("/api/upload", {
      method: "POST", headers: { "X-Uplink": "1" }, body: fd,
    });
    const data = await r.json();
    $("upmsg").textContent = data.error
      ? "error: " + data.error
      : "indexed " + data.saved + " → " + data.collection + " (" + data.chunks + " chunks)";
    if (!data.error) {
      $("file").value = "";
      await loadStatus();
      await loadSources();
      await loadMetrics();
    }
  } catch (e) {
    $("upmsg").textContent = "upload failed";
  } finally {
    $("upgo").disabled = false;
  }
});

/* ------------------------------------------------------------------- chrome */

$("coll").addEventListener("change", async (ev) => {
  state.collection = ev.target.value;
  await loadSources();
  await loadNotes();
});

const THEME_KEY = "uplink-theme";
function applyTheme(mode) {
  if (mode) document.documentElement.setAttribute("data-theme", mode);
  else document.documentElement.removeAttribute("data-theme");
}
$("theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "dark" ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* private mode */ }
  anim($("theme-toggle"), { rotate: -180, duration: 0.5, ease: "back.out(2)" });
});
try {
  const saved = localStorage.getItem(THEME_KEY);
  if (saved) applyTheme(saved);
} catch (e) { /* ignore */ }

/* ------------------------------------------------------------------- start */

function driftOrbs() {
  if (!MOTION || !HAS_GSAP) return;
  const g = window.gsap;
  g.to(".orb-a", { x: 60, y: 40, duration: 18, repeat: -1, yoyo: true, ease: "sine.inOut" });
  g.to(".orb-b", { x: -50, y: 60, duration: 22, repeat: -1, yoyo: true, ease: "sine.inOut" });
  g.to(".orb-c", { x: 40, y: -50, duration: 26, repeat: -1, yoyo: true, ease: "sine.inOut" });
}

function introduce() {
  anim(".topbar", { opacity: 0, y: -14, duration: 0.5, ease: "power3.out" });
  anim(".panel", { opacity: 0, y: 22, duration: 0.6, stagger: 0.08, ease: "power3.out" });
}

async function boot() {
  driftOrbs();
  introduce();
  await loadStatus();
  await loadSources();
  await loadNotes();
  await loadMetrics();
  restoreHistory();
}

boot();
