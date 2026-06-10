// WikiLean live-wiki editor. Injected on article pages for logged-in users by
// the Worker (window.__WL_SLUG__, __WL_USER__, __WL_FULL_ANNOS__ are set first).
// Adapted from site/assets/review.js — same interaction model, but saves to the
// Worker (/api/article/:slug) instead of the localhost review server.
//
//   - Click a highlight (no text selected) → edit that annotation.
//   - Select text → "＋ Annotate" button → create a new annotation over it.
//   - Save POSTs the full annotation array; the server re-renders + records a
//     revision; the page reloads to show the result.
(function () {
  "use strict";
  const slug = window.__WL_SLUG__;
  const user = window.__WL_USER__ || { name: "you" };
  const model = window.__WL_FULL_ANNOS__ || { annotations: [] };
  const annos = model.annotations;
  if (!slug) return;

  let editingIndex = null;
  let pendingSel = null;

  // ---- top bar ----
  const bar = document.createElement("div");
  bar.id = "wlr-bar";
  const ret = encodeURIComponent("/" + slug);
  bar.innerHTML =
    '<b>WikiLean edit</b><span>' + escapeHtml(slug) + "</span>" +
    '<span>' + annos.length + " annotations</span>" +
    '<span style="opacity:.85">click a highlight to edit · select text to add</span>' +
    '<span class="wlr-spacer"></span>' +
    '<span style="opacity:.85">editing as ' + escapeHtml(user.name) + "</span>" +
    '<a href="/' + encodeURIComponent(slug) + '/history" style="color:#fff;margin-left:12px">history</a>' +
    '<a href="/logout?returnTo=' + ret + '" style="color:#fff;margin-left:12px">log out</a>';
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
    <button id="wlr-close" type="button" title="Close (Esc)" aria-label="Close editor">×</button>
    <h3 id="wlr-title">Edit annotation</h3>
    <div class="wlr-quote" id="wlr-quote"></div>
    <label>Status</label>
    <small class="wlr-help">Does Mathlib4 capture this statement?</small>
    <select id="wlr-f-status">
      <option value="formalized">formalized</option>
      <option value="partial">partial</option>
      <option value="not_formalized">not_formalized</option>
    </select>
    <label>Kind</label>
    <small class="wlr-help">definition · theorem · proposition · example · corollary · lemma</small>
    <input id="wlr-f-kind" placeholder="e.g. definition">
    <label>Label</label>
    <small class="wlr-help">Short display name shown at the top of the tooltip.</small>
    <input id="wlr-f-label" placeholder="e.g. Abelianization of a group">
    <label>Mathlib decl</label>
    <small class="wlr-help">Type 2+ chars for autocomplete (~4,600 known decls). Picking a suggestion auto-fills the module.</small>
    <input id="wlr-f-decl" placeholder="e.g. Ideal.IsPrime" list="wlr-decl-options" autocomplete="off">
    <datalist id="wlr-decl-options"></datalist>
    <label>Mathlib module</label>
    <small class="wlr-help">Dotted path, auto-filled from the decl autocomplete.</small>
    <input id="wlr-f-module" placeholder="e.g. Mathlib.RingTheory.Ideal.Prime">
    <label>match_kind</label>
    <small class="wlr-help">exact · generalization · special_case · invocation</small>
    <input id="wlr-f-match" placeholder="e.g. exact">
    <label>Note</label>
    <small class="wlr-help">Optional caveats or context for readers.</small>
    <textarea id="wlr-f-note"></textarea>
    <label>Edit summary (optional)</label>
    <small class="wlr-help">A one-line description shown in /recent-changes.</small>
    <input id="wlr-f-comment" placeholder="what changed + why">
    <div class="wlr-actions">
      <button id="wlr-save">Save</button>
      <button id="wlr-mark-reviewed" title="Set provenance to human-curated without changing any other fields">✓ Mark reviewed</button>
      <button id="wlr-del">Delete</button>
    </div>
    <div id="wlr-status-msg"></div>
    <p class="wlr-notice">Edits are public and attributed (like Wikipedia). Edit metadata may be analyzed for research on human + AI moderation.</p>`;
  document.body.appendChild(panel);

  const $ = (id) => document.getElementById(id);

  document.querySelectorAll(".anno").forEach((el) => {
    el.addEventListener("click", (e) => {
      const sel = window.getSelection();
      if (sel && !sel.isCollapsed && sel.toString().trim().length) return;
      if (e.target.closest("a")) return;
      e.stopPropagation();
      e.preventDefault();
      const raw = el.dataset.annoIndices || el.dataset.annoIndex;
      if (raw == null) return;
      const idxs = String(raw)
        .split(",")
        .map((s) => parseInt(s, 10))
        .filter((n) => !Number.isNaN(n) && annos[n]);
      if (idxs.length === 0) return;
      document.querySelectorAll(".anno.wlr-selected").forEach((n) => n.classList.remove("wlr-selected"));
      el.classList.add("wlr-selected");
      if (idxs.length === 1) {
        maybeShowIntro(() => openEditor(idxs[0]));
      } else {
        maybeShowIntro(() => showPicker(el, idxs));
      }
    });
  });

  // ---- first-edit onboarding overlay ----------------------------------------
  // Shown once per browser (localStorage flag). Skippable. The intro card
  // explains the schema in 30 seconds so a brand-new contributor isn't dropped
  // straight into seven cryptic fields.
  function maybeShowIntro(cb) {
    try {
      if (localStorage.getItem("wl_editor_intro_seen") === "1") {
        cb();
        return;
      }
    } catch (_) {
      /* localStorage blocked — fall through and show the intro every time */
    }
    const overlay = document.createElement("div");
    overlay.id = "wlr-intro";
    overlay.innerHTML =
      '<div class="card">' +
      "<h2>Welcome to the WikiLean editor</h2>" +
      "<p>Each annotation links a Wikipedia statement to a " +
      '<a href="https://leanprover-community.github.io/mathlib4_docs/" target="_blank" rel="noopener">Mathlib4</a> ' +
      "declaration. Key fields:</p>" +
      "<ul>" +
      '<li><b>Status</b> — <span style="color:#2da44e">formalized</span> if Mathlib captures it, ' +
      '<span style="color:#d29922">partial</span> for a related/weaker form, ' +
      '<span style="color:#cf222e">not_formalized</span> otherwise.</li>' +
      "<li><b>Mathlib decl</b> — declaration name, e.g. <code>Ideal.IsPrime</code>. " +
      "Type 2+ chars to autocomplete from ~4,600 known decls; picking one fills the module field.</li>" +
      "<li><b>match_kind</b> — how the decl relates: <code>exact</code> · <code>generalization</code> · " +
      "<code>special_case</code> · <code>invocation</code>.</li>" +
      "</ul>" +
      '<p class="footer">Every save flips this annotation\'s provenance to ' +
      '<span style="color:#2da44e">✓ human-curated</span> — readers see that you\'ve reviewed it. ' +
      "The <b>✓ Mark reviewed</b> button does the same without changing any field.</p>" +
      '<div class="actions"><button id="wlr-intro-ok">Got it</button></div>' +
      "</div>";
    document.body.appendChild(overlay);
    const dismiss = () => {
      try {
        localStorage.setItem("wl_editor_intro_seen", "1");
      } catch (_) { /* ok */ }
      overlay.remove();
      cb();
    };
    overlay.querySelector("#wlr-intro-ok").addEventListener("click", dismiss);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) dismiss();
    });
  }

  // ---- Mathlib decl autocomplete --------------------------------------------
  // The index (~315 KB JSON: 4,600 decls extracted from existing formalized
  // annotations) is fetched once on demand and cached in memory. The decl
  // input is wired to a native <datalist>; picking a suggestion auto-fills the
  // module field if it's empty.
  let mathlibIndex = null;
  let mathlibIndexLoading = null;
  function ensureMathlibIndex() {
    if (mathlibIndex !== null) return Promise.resolve(mathlibIndex);
    if (mathlibIndexLoading) return mathlibIndexLoading;
    mathlibIndexLoading = fetch("/assets/mathlib-index.json")
      .then((r) => (r.ok ? r.json() : []))
      .then((arr) => {
        mathlibIndex = Array.isArray(arr) ? arr : [];
        return mathlibIndex;
      })
      .catch(() => {
        mathlibIndex = [];
        return mathlibIndex;
      });
    return mathlibIndexLoading;
  }
  // Prefetch eagerly so the first keystroke is instant.
  ensureMathlibIndex();

  $("wlr-f-decl").addEventListener("input", async () => {
    const q = $("wlr-f-decl").value.trim().toLowerCase();
    const list = $("wlr-decl-options");
    if (q.length < 2) {
      list.innerHTML = "";
      return;
    }
    const idx = await ensureMathlibIndex();
    const starts = [];
    const contains = [];
    for (let i = 0; i < idx.length; i++) {
      const dl = idx[i][0].toLowerCase();
      if (dl.startsWith(q)) starts.push(idx[i]);
      else if (dl.includes(q)) contains.push(idx[i]);
      if (starts.length >= 30) break;
    }
    const matches = starts.concat(contains).slice(0, 30);
    list.innerHTML = matches
      .map(([d, m]) => '<option value="' + escapeHtml(d) + '">' + escapeHtml(m) + "</option>")
      .join("");
  });
  // When a suggestion is picked from the datalist, fill the module field if
  // the user hasn't typed one already.
  $("wlr-f-decl").addEventListener("change", () => {
    const v = $("wlr-f-decl").value.trim();
    if (!v || !mathlibIndex) return;
    const moduleInput = $("wlr-f-module");
    if (moduleInput.value.trim()) return;
    const match = mathlibIndex.find((row) => row[0] === v);
    if (match) moduleInput.value = match[1];
  });

  // ---- multi-annotation picker (when several annotations share one wrap) ----
  let pickerEl = null;
  function dismissPicker() {
    if (pickerEl) {
      pickerEl.remove();
      pickerEl = null;
    }
  }
  const STATUS_COLORS = {
    formalized: "#2da44e",
    partial: "#d29922",
    not_formalized: "#cf222e",
  };
  function showPicker(annoEl, idxs) {
    dismissPicker();
    pickerEl = document.createElement("div");
    pickerEl.id = "wlr-picker";
    pickerEl.style.cssText =
      "position:fixed;z-index:6500;background:#fff;border:1px solid #d0d7de;border-radius:8px;" +
      "box-shadow:0 4px 14px rgba(0,0,0,.2);padding:6px;font:13px -apple-system,sans-serif;" +
      "min-width:240px;max-width:380px";
    const hdr = document.createElement("div");
    hdr.style.cssText =
      "padding:4px 8px;font-size:11px;color:#57606a;text-transform:uppercase;letter-spacing:.04em";
    hdr.textContent = idxs.length + " annotations here — pick one to edit";
    pickerEl.appendChild(hdr);
    idxs.forEach((i) => {
      const a = annos[i];
      const row = document.createElement("button");
      row.style.cssText =
        "display:block;width:100%;text-align:left;padding:7px 10px;border:none;background:transparent;" +
        "font:inherit;cursor:pointer;border-radius:4px;color:#1f2328";
      row.onmouseenter = function () {
        row.style.background = "#f6f8fa";
      };
      row.onmouseleave = function () {
        row.style.background = "transparent";
      };
      const dot =
        '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' +
        (STATUS_COLORS[a.status] || "#9da6b0") +
        ';margin-right:8px;vertical-align:middle"></span>';
      const label = a.label || a.kind || "annotation #" + i;
      const metaBits = [];
      if (a.kind) metaBits.push(a.kind);
      if (a.status) metaBits.push(a.status.replace("_", " "));
      const meta = metaBits.length
        ? ' <span style="color:#8c959f;font-size:.85em;margin-left:6px">' +
          escapeHtml(metaBits.join(" · ")) +
          "</span>"
        : "";
      row.innerHTML = dot + escapeHtml(label) + meta;
      row.addEventListener("click", (ev) => {
        ev.stopPropagation();
        dismissPicker();
        openEditor(i);
      });
      pickerEl.appendChild(row);
    });
    document.body.appendChild(pickerEl);
    const r = annoEl.getBoundingClientRect();
    const left = Math.min(r.left, window.innerWidth - pickerEl.offsetWidth - 12);
    pickerEl.style.left = Math.max(8, left) + "px";
    pickerEl.style.top = r.bottom + 6 + "px";
  }
  document.addEventListener("click", (e) => {
    if (pickerEl && !pickerEl.contains(e.target)) dismissPicker();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      dismissPicker();
      if (panel.classList.contains("open")) closePanel();
    } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && panel.classList.contains("open")) {
      // Cmd/Ctrl+Enter saves, matching the Save button.
      e.preventDefault();
      save();
    }
  });

  document.addEventListener("mouseup", (e) => {
    if (e.target === fab) return;
    setTimeout(() => {
      const sel = window.getSelection();
      const text = sel ? sel.toString().trim() : "";
      if (!sel || sel.isCollapsed || text.length < 4) {
        fab.style.display = "none";
        return;
      }
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

  function mathlib(a) {
    return a.mathlib || {};
  }

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
    // Show the one-click endorsement only when there's something to flip.
    $("wlr-mark-reviewed").style.display = a.provenance !== "human" ? "" : "none";
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
    ["kind", "label", "decl", "module", "match"].forEach((f) => ($("wlr-f-" + f).value = ""));
    $("wlr-f-note").value = "";
    $("wlr-del").style.display = "none";
    // New annotations become human-curated on save, so nothing to mark separately.
    $("wlr-mark-reviewed").style.display = "none";
    $("wlr-status-msg").textContent = "";
    panel.classList.add("open");
  }

  // Close the edit panel and return to a clean state. Nothing else removed the
  // "open" class before, so a contributor who opened the panel was trapped.
  function closePanel() {
    panel.classList.remove("open");
    editingIndex = null;
    delete panel.dataset.newSnippet;
    delete panel.dataset.newSection;
    document.querySelectorAll(".anno.wlr-selected").forEach((n) => n.classList.remove("wlr-selected"));
    $("wlr-status-msg").textContent = "";
  }
  $("wlr-close").addEventListener("click", closePanel);

  function quoteOf(a) {
    const anc = a.anchor || {};
    return anc.snippet || anc.value || anc.from || "(" + (anc.section || "?") + ")";
  }

  function headingTextOf(el) {
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

  $("wlr-save").addEventListener("click", save);
  $("wlr-del").addEventListener("click", () => {
    if (editingIndex == null) return;
    if (!confirm("Delete this annotation?")) return;
    annos.splice(editingIndex, 1);
    persist("deleted");
  });
  $("wlr-mark-reviewed").addEventListener("click", () => {
    // One-click endorsement: flip provenance to "human" without touching any
    // other field. Useful when an AI annotation is already correct and the
    // editor just wants to signal "I've checked this."
    if (editingIndex == null) return;
    const a = annos[editingIndex];
    if (a.provenance === "human") return;
    a.provenance = "human";
    persist("marked reviewed");
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
      // Spread the original first so fields the form doesn't expose (proof_note,
      // id, formalizations, tombstone markers, moderation_flag, …) survive; the
      // form-derived fields in `built` win, so explicitly cleared fields (e.g. an
      // emptied note) are still written through.
      built.anchor = annos[editingIndex].anchor;
      annos[editingIndex] = { ...annos[editingIndex], ...built };
    }
    persist("saved");
  }

  function persist(verb) {
    $("wlr-status-msg").textContent = "saving…";
    fetch("/api/article/" + encodeURIComponent(slug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        annotations: annos,
        comment: $("wlr-f-comment").value.trim(),
        base_version: window.__WL_VERSION__,
      }),
    })
      .then((r) => Promise.all([r.status, r.json()]))
      .then(([status, res]) => {
        if (status === 409) {
          // The article changed under us. Don't clobber the newer revision —
          // make the contributor reload and re-apply their edit.
          $("wlr-status-msg").innerHTML =
            "This article was edited since you loaded it — reload to get the latest version, then re-apply your change. " +
            '<a href="#" onclick="location.reload();return false">reload</a>';
          return;
        }
        if (!res.ok) {
          $("wlr-status-msg").textContent = "error: " + (res.error || "save failed");
          return;
        }
        // Keep the in-page version in sync so a second save in the same session
        // doesn't spuriously 409.
        if (typeof res.version === "number") window.__WL_VERSION__ = res.version;
        const m = (res.matched || "").match(/(\d+)\/(\d+)/);
        if (m && m[1] !== m[2]) {
          $("wlr-status-msg").innerHTML =
            verb + ", but only " + res.matched + " anchored — an annotation's text didn't match the article. " +
            '<a href="#" onclick="location.reload();return false">reload anyway</a>';
        } else {
          $("wlr-status-msg").textContent = verb + " — reloading (" + (res.matched || "") + ")";
          setTimeout(() => location.reload(), 400);
        }
      })
      .catch((e) => {
        $("wlr-status-msg").textContent = "error: " + e;
      });
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
})();
