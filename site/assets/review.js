// WikiLean review-mode editor. Injected by serve_review.py on top of a
// rendered article. Edits write back to annotations/<slug>.json via /api/save.
//
// Interaction model:
//   - Plain click on a highlight (no text selected) → edit that annotation.
//   - Select any text (even inside an existing highlight) → a floating
//     "＋ Annotate" button appears → click it to create a new annotation over
//     the selection. This lets you annotate sub-spans of existing highlights.
(function () {
  "use strict";
  const slug = window.__WL_SLUG__;
  const model = window.__WL_FULL_ANNOS__ || { annotations: [] };
  const annos = model.annotations;

  let editingIndex = null;            // index into annos, or null when adding
  let pendingSel = null;              // {text, section} captured on selection

  // ---- top bar ----
  const bar = document.createElement("div");
  bar.id = "wlr-bar";
  bar.innerHTML =
    '<b>WikiLean review</b><span>' + escapeHtml(slug) + "</span>" +
    '<span>' + annos.length + " annotations</span>" +
    '<span class="wlr-spacer"></span>' +
    '<span style="opacity:.85">click a highlight to edit · select text to add</span>' +
    '<a href="/" style="color:#fff;margin-left:12px">all articles</a>';
  document.body.appendChild(bar);

  // ---- floating "annotate selection" button ----
  const fab = document.createElement("button");
  fab.id = "wlr-fab";
  fab.textContent = "＋ Annotate";
  fab.style.cssText =
    "position:fixed;z-index:6000;display:none;background:#0969da;color:#fff;" +
    "border:none;border-radius:6px;padding:5px 10px;font:13px -apple-system,sans-serif;" +
    "cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.25)";
  document.body.appendChild(fab);

  // ---- edit panel ----
  const panel = document.createElement("div");
  panel.id = "wlr-panel";
  panel.innerHTML = `
    <h3 id="wlr-title">Edit annotation</h3>
    <div class="wlr-quote" id="wlr-quote"></div>
    <label>Status</label>
    <select id="wlr-f-status">
      <option value="formalized">formalized</option>
      <option value="partial">partial</option>
      <option value="not_formalized">not_formalized</option>
    </select>
    <label>Kind</label>
    <input id="wlr-f-kind" placeholder="definition / proposition / theorem / example">
    <label>Label</label>
    <input id="wlr-f-label">
    <label>Mathlib decl</label>
    <input id="wlr-f-decl" placeholder="e.g. Ideal.IsPrime">
    <label>Mathlib module</label>
    <input id="wlr-f-module" placeholder="e.g. Mathlib.RingTheory.Ideal.Prime">
    <label>match_kind</label>
    <input id="wlr-f-match" placeholder="exact / generalization / special_case / invocation">
    <label>Note</label>
    <textarea id="wlr-f-note"></textarea>
    <div class="wlr-actions">
      <button id="wlr-save">Save</button>
      <button id="wlr-del">Delete</button>
    </div>
    <div id="wlr-status-msg"></div>`;
  document.body.appendChild(panel);

  const $ = (id) => document.getElementById(id);

  // ---- highlight click → edit (suppressed while text is selected) ----
  document.querySelectorAll(".anno").forEach((el) => {
    el.addEventListener("click", (e) => {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim().length) return; // selecting, not clicking
      // Let wiki links work: a click on (or inside) an <a> navigates natively
      // (plain click → same tab, ⌘/Ctrl-click → new tab). Don't hijack it.
      if (e.target.closest("a")) return;
      // Stacked annotations nest in the DOM (a bullet's <span class=anno> sits
      // inside the colon-intro <div class=anno>). Stop here so the INNERMOST
      // (event target) wins; clicking the outer's non-nested text still edits it.
      e.stopPropagation();
      e.preventDefault();
      const raw = el.dataset.annoIndices || el.dataset.annoIndex;
      if (raw == null) return;
      const idx = parseInt(String(raw).split(",")[0], 10);
      if (Number.isNaN(idx) || !annos[idx]) return;
      document.querySelectorAll(".anno.wlr-selected").forEach(n => n.classList.remove("wlr-selected"));
      el.classList.add("wlr-selected");
      openEditor(idx);
    });
  });

  // ---- selection → show the floating annotate button ----
  document.addEventListener("mouseup", (e) => {
    if (e.target === fab) return;
    setTimeout(() => {
      const sel = window.getSelection();
      const text = sel ? sel.toString().trim() : "";
      if (!sel || sel.isCollapsed || text.length < 4) { fab.style.display = "none"; return; }
      const rect = sel.getRangeAt(0).getBoundingClientRect();
      pendingSel = { text, section: nearestSection(sel.anchorNode) };
      fab.style.left = Math.max(8, rect.left) + "px";
      fab.style.top = Math.max(44, rect.top - 34) + "px";
      fab.style.display = "block";
    }, 0);
  });

  document.addEventListener("mousedown", (e) => {
    if (e.target !== fab) fab.style.display = "none";
  });

  fab.addEventListener("click", () => {
    if (!pendingSel) return;
    fab.style.display = "none";
    openAdder(pendingSel.text, pendingSel.section);
  });

  function mathlib(a) { return a.mathlib || {}; }

  function openEditor(idx) {
    editingIndex = idx;
    const a = annos[idx];
    const m = mathlib(a);
    $("wlr-title").textContent = "Edit annotation #" + idx;
    $("wlr-quote").textContent = quoteOf(a);
    $("wlr-f-status").value = a.status || "not_formalized";
    $("wlr-f-kind").value = a.kind || "";
    $("wlr-f-label").value = a.label || "";
    $("wlr-f-decl").value = m.decl || a.decl || "";
    $("wlr-f-module").value = m.module || a.module || "";
    $("wlr-f-match").value = m.match_kind || a.match_kind || "";
    $("wlr-f-note").value = a.note || "";
    $("wlr-del").style.display = "";
    $("wlr-status-msg").textContent = "";
    panel.classList.add("open");
  }

  function openAdder(text, section) {
    editingIndex = null;
    panel.dataset.newSnippet = text;
    panel.dataset.newSection = section;
    $("wlr-title").textContent = "New annotation";
    $("wlr-quote").textContent = text + "   [§ " + section + "]";
    $("wlr-f-status").value = "not_formalized";
    ["kind", "label", "decl", "module", "match"].forEach(f => $("wlr-f-" + f).value = "");
    $("wlr-f-note").value = "";
    $("wlr-del").style.display = "none";
    $("wlr-status-msg").textContent = "";
    panel.classList.add("open");
  }

  function quoteOf(a) {
    const anc = a.anchor || {};
    return anc.snippet || anc.value || anc.from || "(" + (anc.section || "?") + ")";
  }

  function headingTextOf(el) {
    // MediaWiki wraps headings: <div class="mw-heading"><h3>Title</h3>…</div>.
    // Match a bare <h1-4> OR a wrapper that contains one, and return just the
    // heading's own text (the <h3>.textContent excludes the [edit] span).
    if (/^H[1-4]$/.test(el.tagName)) return el.textContent.replace(/\[edit\]/g, "").trim();
    const h = el.querySelector && el.querySelector("h1,h2,h3,h4");
    if (h) return h.textContent.replace(/\[edit\]/g, "").trim();
    return null;
  }

  function nearestSection(node) {
    let el = node && node.nodeType === 3 ? node.parentElement : node;
    while (el && el !== document.body) {
      let p = el.previousElementSibling;
      while (p) {
        const ht = headingTextOf(p);
        if (ht) return ht;
        p = p.previousElementSibling;
      }
      el = el.parentElement;
    }
    return "(Lead)";
  }

  // ---- save / delete ----
  $("wlr-save").addEventListener("click", save);
  $("wlr-del").addEventListener("click", () => {
    if (editingIndex == null) return;
    if (!confirm("Delete this annotation?")) return;
    annos.splice(editingIndex, 1);
    persist("deleted");
  });

  function save() {
    const built = {
      status: $("wlr-f-status").value,
      kind: $("wlr-f-kind").value.trim() || undefined,
      label: $("wlr-f-label").value.trim() || undefined,
      note: $("wlr-f-note").value.trim() || undefined,
    };
    const decl = $("wlr-f-decl").value.trim();
    const module = $("wlr-f-module").value.trim();
    const match = $("wlr-f-match").value.trim();
    if (decl || module || match) {
      built.mathlib = { decl: decl || null, module: module || null, match_kind: match || null };
    }
    built.provenance = "human";

    if (editingIndex == null) {
      built.anchor = { section: panel.dataset.newSection, snippet: panel.dataset.newSnippet };
      annos.push(built);
    } else {
      built.anchor = annos[editingIndex].anchor; // keep existing anchor
      annos[editingIndex] = built;
    }
    persist("saved");
  }

  function persist(verb) {
    $("wlr-status-msg").textContent = "saving…";
    fetch("/api/save/" + encodeURIComponent(slug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ annotations: annos }),
    }).then(r => r.json()).then(res => {
      if (!res.ok) {
        $("wlr-status-msg").textContent = "error: " + (res.error || res.render_output || "save failed");
        return;
      }
      // Warn (and don't auto-reload) if some annotation failed to anchor —
      // e.g. the snippet/section didn't match. The work is saved either way.
      const m = (res.matched || "").match(/(\d+)\/(\d+)/);
      if (m && m[1] !== m[2]) {
        $("wlr-status-msg").innerHTML =
          verb + ", but only " + res.matched + " — an annotation's text didn't " +
          "anchor (check the snippet matches the article). " +
          '<a href="#" onclick="location.reload();return false">reload anyway</a>';
      } else {
        $("wlr-status-msg").textContent = verb + " — reloading (" + (res.matched || "") + ")";
        setTimeout(() => location.reload(), 400);
      }
    }).catch(e => { $("wlr-status-msg").textContent = "error: " + e; });
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
})();
