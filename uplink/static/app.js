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
    const name = el("button", "source-name", src.title || src.path);
    name.type = "button";
    name.title = "Open " + src.path;
    name.addEventListener("click", () => openDoc(src.path, src.collection, null));
    body.appendChild(name);

    const sub = el("div", "source-sub");
    sub.appendChild(el("span", "ftype", src.filetype || "doc"));
    sub.appendChild(el("span", null, (src.chunks || 0) + " chunks"));
    if (!state.collection && src.collection) {
      sub.appendChild(el("span", null, src.collection));
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

/* -------------------------------------------------------------- the thread */

function clearEmptyState() {
  // Hide, never remove: #suggestions and #empty-title are its children, and
  // detaching them makes every later getElementById return null — which
  // threw inside loadSources() and turned successful uploads into
  // "upload failed".
  const empty = $("empty");
  if (empty) empty.hidden = true;
}

function addTurn(question) {
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
        try { renderAnswer(card, st, q); } finally { $("ai").disabled = false; }
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

function renderDoc(doc, citedSeq) {
  const total = Number(doc.total_chunks) || 0;
  const start = Number(doc.start) || 0;
  const chunks = Array.isArray(doc.chunks) ? doc.chunks : [];

  state.reader = {
    path: doc.path, collection: doc.collection,
    citedSeq: citedSeq, start: start, shown: chunks.length, total: total,
  };

  $("reader-path").textContent = String(doc.path || "");
  $("reader-meta").textContent =
    "source text from the index · " + String(doc.collection || "") + " · " + total + " chunks";

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
}

boot();
