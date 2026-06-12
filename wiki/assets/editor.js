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
  // Annotations created in this session that the server has never seen (their
  // save failed or 409'd before succeeding). Deleting one of these just drops
  // it — a tombstone is only meaningful for annotations that were persisted.
  // Cleared on every successful persist.
  const unsavedNew = new Set();

  // ---- top bar ----
  // Tombstoned (status "rejected") annotations render no highlight, so without
  // the "N hidden" button there is no way to reach them and "pick another
  // status and save to restore" is unreachable (fix #2).
  const rejectedIdxs = [];
  annos.forEach(function (a, i) {
    if (a && a.status === "rejected") rejectedIdxs.push(i);
  });
  const bar = document.createElement("div");
  bar.id = "wlr-bar";
  const ret = encodeURIComponent("/" + slug);
  bar.innerHTML =
    '<b>WikiLean edit</b><span>' + escapeHtml(slug) + "</span>" +
    '<span>' + annos.length + " annotations</span>" +
    (rejectedIdxs.length
      ? '<button id="wlr-hidden" type="button" title="Rejected annotations are hidden from readers — open one, pick another status, and save to restore it">' +
        rejectedIdxs.length + " hidden annotation" + (rejectedIdxs.length === 1 ? "" : "s") + "</button>"
      : "") +
    '<span style="opacity:.85">click a highlight to edit · select text to add</span>' +
    '<span class="wlr-spacer"></span>' +
    '<span style="opacity:.85">editing as ' + escapeHtml(user.name) + "</span>" +
    '<a href="/' + encodeURIComponent(slug) + '/history" style="color:#fff;margin-left:12px">history</a>' +
    '<a href="/logout?returnTo=' + ret + '" style="color:#fff;margin-left:12px">log out</a>';
  // Prepend (not append): on ≤640px review.css positions the bar sticky, which
  // only works from the top of the document flow (fix #5).
  document.body.prepend(bar);
  if (rejectedIdxs.length) {
    // Each click opens the next hidden annotation in the editor panel,
    // cycling through all of them (fix #2).
    let hiddenCursor = 0;
    bar.querySelector("#wlr-hidden").addEventListener("click", function () {
      openEditor(rejectedIdxs[hiddenCursor % rejectedIdxs.length]);
      hiddenCursor++;
    });
  }

  // ---- floating "annotate selection" button ----
  const fab = document.createElement("button");
  fab.id = "wlr-fab";
  fab.textContent = "＋ Annotate";
  fab.style.cssText =
    "position:fixed;z-index:6000;display:none;background:#1a4b8c;color:#fff;" +
    "border:none;border-radius:6px;padding:5px 10px;font:13px -apple-system,sans-serif;" +
    "cursor:pointer;box-shadow:0 2px 8px rgba(0,0,0,.25)";
  document.body.appendChild(fab);

  // ---- edit panel ----
  const panel = document.createElement("div");
  panel.id = "wlr-panel";
  // Dialog semantics + label/for on every field for assistive tech (fix #11c).
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-labelledby", "wlr-title");
  panel.innerHTML = `
    <button id="wlr-close" type="button" title="Close (Esc)" aria-label="Close editor">×</button>
    <h3 id="wlr-title">Edit annotation</h3>
    <div class="wlr-quote" id="wlr-quote"></div>
    <label for="wlr-f-status">Status</label>
    <small class="wlr-help">Does Mathlib4 capture this statement?</small>
    <select id="wlr-f-status">
      <option value="formalized">formalized</option>
      <option value="partial">partial</option>
      <option value="not_formalized">not_formalized</option>
      <option value="rejected">rejected (hide)</option>
    </select>
    <label for="wlr-f-kind">Kind</label>
    <small class="wlr-help">definition · theorem · proposition · example · corollary · lemma</small>
    <input id="wlr-f-kind" placeholder="e.g. definition">
    <label for="wlr-f-label">Label</label>
    <small class="wlr-help">Short display name shown at the top of the tooltip.</small>
    <input id="wlr-f-label" placeholder="e.g. Abelianization of a group">
    <label for="wlr-f-decl">Mathlib decl</label>
    <small class="wlr-help">Type 2+ chars for autocomplete (full Mathlib index; decls already used on WikiLean rank first). Picking a suggestion auto-fills the module.</small>
    <input id="wlr-f-decl" placeholder="e.g. Ideal.IsPrime" list="wlr-decl-options" autocomplete="off">
    <datalist id="wlr-decl-options"></datalist>
    <div id="wlr-decl-check" aria-live="polite" style="font-size:12px;min-height:15px;margin:2px 0 4px"></div>
    <label for="wlr-f-module">Mathlib module</label>
    <small class="wlr-help">Dotted path, auto-filled from the decl autocomplete.</small>
    <input id="wlr-f-module" placeholder="e.g. Mathlib.RingTheory.Ideal.Prime">
    <label for="wlr-f-match">match_kind</label>
    <small class="wlr-help">exact · generalization · special_case · invocation</small>
    <input id="wlr-f-match" placeholder="e.g. exact">
    <label for="wlr-f-note">Note</label>
    <small class="wlr-help">Optional caveats or context for readers.</small>
    <textarea id="wlr-f-note"></textarea>
    <label for="wlr-f-comment">Edit summary (optional)</label>
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
      '<li><b>Status</b> — <span style="color:#266844">formalized</span> if Mathlib captures it, ' +
      '<span style="color:#7d5a10">partial</span> for a related/weaker form, ' +
      '<span style="color:#9c2f28">not_formalized</span> otherwise.</li>' +
      "<li><b>Mathlib decl</b> — declaration name, e.g. <code>Ideal.IsPrime</code>. " +
      "Type 2+ chars to autocomplete from the full Mathlib index; picking one fills the module field.</li>" +
      "<li><b>match_kind</b> — how the decl relates: <code>exact</code> · <code>generalization</code> · " +
      "<code>special_case</code> · <code>invocation</code>.</li>" +
      "</ul>" +
      '<p class="footer">Every save flips this annotation\'s provenance to ' +
      '<span style="color:#266844">✓ human-curated</span> — readers see that you\'ve reviewed it. ' +
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
  // Two tiers feed the decl <datalist>:
  //   1. Curated boost tier (/assets/mathlib-index.json, ~4,600 decls already
  //      used in WikiLean annotations) — fetched once, ranked first.
  //   2. Full Mathlib index (/assets/decl-index/, ~411k decls from doc-gen4's
  //      declaration-data) — prefix-sharded; the manifest maps a typed prefix
  //      to shard files, fetched on demand and cached in memory per page. It
  //      also powers the on-blur existence check (subtle ✓ / "not found"
  //      hint — purely informational, saving is NEVER blocked).
  // Every full-index failure (manifest 404, shard fetch error) degrades
  // silently to curated-only behavior.
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

  // Full-index manifest: undefined = not requested yet, null = unavailable
  // (degrade silently), object = loaded. Shard promises resolve to the
  // [decl, module] array or null on any failure (cached either way — no
  // retry storms while someone types).
  let declManifest;
  let declManifestLoading = null;
  const declShards = {};
  function ensureDeclManifest() {
    if (declManifest !== undefined) return Promise.resolve(declManifest);
    if (declManifestLoading) return declManifestLoading;
    declManifestLoading = fetch("/assets/decl-index/manifest.json")
      .then((r) => (r.ok ? r.json() : null))
      .then((m) => {
        declManifest = m && m.shards && m.scheme ? m : null;
        return declManifest;
      })
      .catch(() => {
        declManifest = null;
        return null;
      });
    return declManifestLoading;
  }
  // Shard-key normalization — must mirror scripts/build-decl-index.ts exactly:
  // lowercase [a-z0-9], everything else "_", pad short names with "_".
  function declShardKey(name, len) {
    let k = "";
    for (let i = 0; i < len; i++) {
      if (i < name.length) {
        const l = name[i].toLowerCase();
        k += /[a-z0-9]/.test(l) ? l : "_";
      } else {
        k += "_";
      }
    }
    return k;
  }
  // The unique shard holding a full decl name (leaf keys are prefix-free):
  // the longest manifest key that prefixes the padded normalized name.
  function declShardFor(m, name) {
    const maxLen = (m.scheme && m.scheme.max_len) || 2;
    for (let len = Math.min(maxLen, Math.max(name.length, 2)); len >= 2; len--) {
      const k = declShardKey(name, len);
      if (m.shards[k] !== undefined) return k;
    }
    // Names shorter than every key under them pad upward, so retry padded
    // lengths too (e.g. name "Na" when the leaf is "na_").
    for (let len = Math.max(name.length, 2) + 1; len <= maxLen; len++) {
      const k = declShardKey(name, len);
      if (m.shards[k] !== undefined) return k;
    }
    return null;
  }
  // Shards worth consulting for a typed prefix: either the one leaf that
  // contains the whole range, or — when the prefix sits above a split — its
  // children, capped at 4 fetches (past that the full-index tier sits out
  // for this keystroke; another character narrows it).
  function declShardsForQuery(m, q) {
    const qn = declShardKey(q, Math.max(q.length, 2));
    const children = [];
    for (const k in m.shards) {
      if (qn.startsWith(k)) return [k];
      if (k.startsWith(qn)) {
        children.push(k);
        if (children.length > 4) return [];
      }
    }
    return children;
  }
  function fetchDeclShard(key) {
    if (!(key in declShards)) {
      declShards[key] = fetch("/assets/decl-index/" + key + ".json")
        .then((r) => (r.ok ? r.json() : null))
        .then((a) => (Array.isArray(a) ? a : null))
        .catch(() => null);
    }
    return declShards[key];
  }
  // Full-index candidates for a query: prefix matches first, then contains
  // matches within the fetched shards. {starts:[], contains:[]} on any failure.
  async function fullIndexMatches(rawQuery) {
    const none = { starts: [], contains: [] };
    const m = await ensureDeclManifest();
    if (!m) return none;
    const keys = declShardsForQuery(m, rawQuery);
    if (!keys.length) return none;
    const shards = await Promise.all(keys.map(fetchDeclShard));
    const q = rawQuery.toLowerCase();
    const starts = [];
    const contains = [];
    for (const arr of shards) {
      if (!arr) continue;
      for (const row of arr) {
        const dl = row[0].toLowerCase();
        if (dl.startsWith(q)) starts.push(row);
        else if (dl.includes(q)) contains.push(row);
      }
    }
    // Shards arrive per-child; re-sort so suggestions are stable.
    starts.sort((a, b) => (a[0] < b[0] ? -1 : 1));
    contains.sort((a, b) => (a[0] < b[0] ? -1 : 1));
    return { starts, contains };
  }
  // Exact full-index lookup: [decl, module] on a hit, null = definitively
  // absent, undefined = couldn't check (index unavailable — stay silent).
  async function lookupDecl(name) {
    const m = await ensureDeclManifest();
    if (!m) return undefined;
    const key = declShardFor(m, name);
    if (key === null) return null;
    const arr = await fetchDeclShard(key);
    if (!arr) return undefined;
    for (const row of arr) if (row[0] === name) return row;
    return null;
  }
  // Prefetch both tiers' entry points so the first keystroke is instant.
  ensureMathlibIndex();
  ensureDeclManifest();

  function setDeclCheck(msg, ok) {
    const el = $("wlr-decl-check");
    el.textContent = msg;
    el.style.color = ok ? "#2f7d4f" : "#7d5a10";
  }

  let declQuerySeq = 0;
  $("wlr-f-decl").addEventListener("input", async () => {
    setDeclCheck("", true); // typing invalidates the last on-blur verdict
    const raw = $("wlr-f-decl").value.trim();
    const q = raw.toLowerCase();
    const list = $("wlr-decl-options");
    if (q.length < 2) {
      list.innerHTML = "";
      return;
    }
    const seq = ++declQuerySeq;
    const results = await Promise.all([ensureMathlibIndex(), fullIndexMatches(raw)]);
    if (seq !== declQuerySeq) return; // superseded by a newer keystroke
    const curated = results[0];
    const full = results[1];
    const cStarts = [];
    const cContains = [];
    for (let i = 0; i < curated.length; i++) {
      const dl = curated[i][0].toLowerCase();
      if (dl.startsWith(q)) cStarts.push(curated[i]);
      else if (dl.includes(q)) cContains.push(curated[i]);
    }
    // Merge: curated tier first ("previously used in WikiLean"), then
    // full-index prefix matches, then contains matches; dedupe; cap 30.
    const seen = new Set();
    const out = [];
    const take = (rows) => {
      for (const row of rows) {
        if (out.length >= 30) return;
        if (seen.has(row[0])) continue;
        seen.add(row[0]);
        out.push(row);
      }
    };
    take(cStarts);
    take(cContains);
    take(full.starts);
    take(full.contains);
    list.innerHTML = out
      .map(([d, m]) => '<option value="' + escapeHtml(d) + '">' + escapeHtml(m) + "</option>")
      .join("");
  });
  // When a suggestion is picked from the datalist, fill the module field if
  // the user hasn't typed one already — curated tier first, then full index.
  $("wlr-f-decl").addEventListener("change", async () => {
    const v = $("wlr-f-decl").value.trim();
    if (!v) return;
    const moduleInput = $("wlr-f-module");
    if (moduleInput.value.trim()) return;
    let match = (mathlibIndex || []).find((row) => row[0] === v) || null;
    if (!match) match = (await lookupDecl(v)) || null;
    if (match && !moduleInput.value.trim()) moduleInput.value = match[1];
  });
  // On-blur existence check against the full index. Informational only:
  // a miss NEVER blocks saving (partial/informal references are legitimate),
  // and an unavailable index says nothing at all.
  $("wlr-f-decl").addEventListener("blur", async () => {
    const v = $("wlr-f-decl").value.trim();
    if (!v) {
      setDeclCheck("", true);
      return;
    }
    const hit = await lookupDecl(v);
    if ($("wlr-f-decl").value.trim() !== v) return; // field changed meanwhile
    if (hit === undefined) return; // index unavailable — silent
    if (hit) {
      setDeclCheck("✓ found in Mathlib", true);
      const moduleInput = $("wlr-f-module");
      if (!moduleInput.value.trim()) moduleInput.value = hit[1];
    } else {
      setDeclCheck("not found in Mathlib — check spelling (informal or partial references are still fine to save)", false);
    }
  });

  // ---- multi-annotation picker (when several annotations share one wrap) ----
  let pickerEl = null;
  function dismissPicker() {
    if (pickerEl) {
      pickerEl.remove();
      pickerEl = null;
    }
  }
  // Status dots in the warm palette's trio; rejected gets the untouched gray (fix #6d).
  const STATUS_COLORS = {
    formalized: "#2f7d4f",
    partial: "#b08020",
    not_formalized: "#b3372f",
    rejected: "#9a9183",
  };
  function showPicker(annoEl, idxs) {
    dismissPicker();
    pickerEl = document.createElement("div");
    pickerEl.id = "wlr-picker";
    pickerEl.style.cssText =
      "position:fixed;z-index:6500;background:#fffdf9;border:1px solid #d8d0bd;border-radius:8px;" +
      "box-shadow:0 4px 14px rgba(0,0,0,.2);padding:6px;font:13px -apple-system,sans-serif;" +
      "min-width:240px;max-width:380px";
    const hdr = document.createElement("div");
    hdr.style.cssText =
      "padding:4px 8px;font-size:11px;color:#5f594e;text-transform:uppercase;letter-spacing:.04em";
    hdr.textContent = idxs.length + " annotations here — pick one to edit";
    pickerEl.appendChild(hdr);
    idxs.forEach((i) => {
      const a = annos[i];
      const row = document.createElement("button");
      row.style.cssText =
        "display:block;width:100%;text-align:left;padding:7px 10px;border:none;background:transparent;" +
        "font:inherit;cursor:pointer;border-radius:4px;color:#1f1d1a";
      row.onmouseenter = function () {
        row.style.background = "#f7f4ee";
      };
      row.onmouseleave = function () {
        row.style.background = "transparent";
      };
      const dot =
        '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' +
        (STATUS_COLORS[a.status] || "#9a9183") +
        ';margin-right:8px;vertical-align:middle"></span>';
      const label = a.label || a.kind || "annotation #" + i;
      const metaBits = [];
      if (a.kind) metaBits.push(a.kind);
      if (a.status) {
        metaBits.push(a.status === "rejected" ? "rejected (hidden from readers)" : a.status.replace("_", " "));
      }
      const meta = metaBits.length
        ? ' <span style="color:#5f594e;font-size:.85em;margin-left:6px">' +
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
      if (panel.classList.contains("open")) requestClose();
    } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && panel.classList.contains("open")) {
      // Cmd/Ctrl+Enter saves, matching the Save button.
      e.preventDefault();
      save();
    }
  });

  function maybeShowFab() {
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
  }

  document.addEventListener("mouseup", (e) => {
    if (e.target === fab) return;
    setTimeout(maybeShowFab, 0);
  });
  // iOS long-press selections don't reliably fire mouseup, so also react to
  // touchend and (debounced) selectionchange — the latter covers dragging the
  // native selection handles, which fires no mouse/touch events at all (fix #11f).
  document.addEventListener("touchend", (e) => {
    if (e.target === fab) return;
    setTimeout(maybeShowFab, 0);
  });
  let selDebounce = null;
  document.addEventListener("selectionchange", () => {
    clearTimeout(selDebounce);
    selDebounce = setTimeout(() => {
      // Caret movement inside the panel's fields also fires selectionchange —
      // don't flicker the FAB while someone is typing.
      const ae = document.activeElement;
      if (ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName)) return;
      maybeShowFab();
    }, 250);
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

  // ---- form-state helpers (fixes #8/#9/#11c) --------------------------------

  function fieldValues() {
    return {
      status: $("wlr-f-status").value,
      kind: $("wlr-f-kind").value,
      label: $("wlr-f-label").value,
      decl: $("wlr-f-decl").value,
      module: $("wlr-f-module").value,
      match: $("wlr-f-match").value,
      note: $("wlr-f-note").value,
      comment: $("wlr-f-comment").value,
    };
  }

  // Snapshot taken when the panel opens; Escape/× compare against it so
  // unsaved changes prompt before being discarded (fix #11c).
  let openSnapshot = "";
  function fieldSnapshot() {
    return JSON.stringify(fieldValues());
  }

  // 409 draft preservation (fix #9): the typed fields are stashed in
  // sessionStorage keyed by slug + annotation identity, so reopening the same
  // annotation — even after a reload to rebase — restores the draft.
  function draftKey() {
    let ident;
    if (editingIndex != null) {
      const a = annos[editingIndex];
      ident = a && a.id ? "id:" + a.id : "idx:" + editingIndex;
    } else {
      ident = "new:" + (panel.dataset.newSection || "") + ":" + (panel.dataset.newSnippet || "");
    }
    return "wl_draft:" + slug + ":" + ident;
  }
  function stashDraft() {
    try {
      sessionStorage.setItem(draftKey(), JSON.stringify(fieldValues()));
    } catch (_) { /* storage blocked — the panel keeps the fields anyway */ }
  }
  function restoreDraft() {
    let d = null;
    try {
      const raw = sessionStorage.getItem(draftKey());
      if (raw) {
        sessionStorage.removeItem(draftKey());
        d = JSON.parse(raw);
      }
    } catch (_) { /* ignore */ }
    if (!d) return false;
    $("wlr-f-status").value = d.status || $("wlr-f-status").value;
    $("wlr-f-kind").value = d.kind || "";
    $("wlr-f-label").value = d.label || "";
    $("wlr-f-decl").value = d.decl || "";
    $("wlr-f-module").value = d.module || "";
    $("wlr-f-match").value = d.match || "";
    $("wlr-f-note").value = d.note || "";
    $("wlr-f-comment").value = d.comment || "";
    return true;
  }

  // Disable the action buttons while a request is in flight so a double-click
  // can't double-submit; every failure path re-enables them (fix #8).
  function setBusy(busy) {
    ["wlr-save", "wlr-mark-reviewed", "wlr-del"].forEach(function (id) {
      $(id).disabled = busy;
    });
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
    setDeclCheck("", true);
    $("wlr-del").style.display = "";
    // Show the one-click endorsement only when there's something to flip.
    $("wlr-mark-reviewed").style.display = a.provenance !== "human" ? "" : "none";
    $("wlr-status-msg").textContent =
      a.status === "rejected"
        ? "This annotation is rejected (hidden from readers). Pick another status and save to restore it."
        : "";
    // Snapshot the canonical values first, THEN overlay any stashed 409 draft —
    // a restored draft must count as unsaved changes (fixes #9/#11c).
    setBusy(false);
    openSnapshot = fieldSnapshot();
    if (restoreDraft()) {
      $("wlr-status-msg").textContent = "restored your unsaved draft — review and save to re-apply it";
    }
    panel.classList.add("open");
    $("wlr-f-status").focus();
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
    setDeclCheck("", true);
    $("wlr-del").style.display = "none";
    // New annotations become human-curated on save, so nothing to mark separately.
    $("wlr-mark-reviewed").style.display = "none";
    $("wlr-status-msg").textContent = "";
    // Same snapshot-then-restore dance as openEditor (fixes #9/#11c); the
    // draft key includes section+snippet, so reselecting the same text after
    // a 409 reload brings the draft back.
    setBusy(false);
    openSnapshot = fieldSnapshot();
    if (restoreDraft()) {
      $("wlr-status-msg").textContent = "restored your unsaved draft — review and save to re-apply it";
    }
    panel.classList.add("open");
    $("wlr-f-status").focus();
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
  // Escape/× with unsaved changes ask before discarding (fix #11c).
  function requestClose() {
    if (fieldSnapshot() !== openSnapshot && !confirm("Discard unsaved changes?")) return;
    closePanel();
  }
  $("wlr-close").addEventListener("click", requestClose);

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
    const a = annos[editingIndex];
    if (unsavedNew.has(a)) {
      // Never persisted — nothing to veto, so just drop it client-side.
      unsavedNew.delete(a);
      annos.splice(editingIndex, 1);
    } else {
      // Tombstone, not splice: keep id/anchor/all other fields, flip status to
      // "rejected" with provenance "human". The wrap engines skip it (readers
      // never see it) and the AI moderation pass reads it as a human veto
      // instead of an uncovered statement it would happily re-annotate.
      annos[editingIndex] = { ...a, status: "rejected", provenance: "human" };
    }
    persist("deleted");
  });
  $("wlr-mark-reviewed").addEventListener("click", () => {
    // One-click endorsement: ask the server to flip provenance to "human"
    // without touching any other field (D-C2 {action:'endorse'}). The old
    // client-side provenance-flip-and-save no longer works — stampProvenance
    // keeps stored provenance for unchanged annotations by design, so a bare
    // flip in a full save silently reverts.
    if (editingIndex == null) return;
    const a = annos[editingIndex];
    if (a.provenance === "human") return;
    if (!a.id) {
      // Legacy tab loaded before the id backfill: no stable id to endorse by.
      // Fall back to the old full-save path (the flip won't stick server-side,
      // but the save still records the review attempt) and say so.
      console.warn("WikiLean: annotation has no id — endorsing via legacy full save");
      a.provenance = "human";
      persist("marked reviewed");
      return;
    }
    endorse(editingIndex, a);
  });

  function endorse(idx, a) {
    setBusy(true); // (fix #8)
    $("wlr-status-msg").textContent = "endorsing…";
    fetch("/api/article/" + encodeURIComponent(slug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "endorse",
        annotation_id: a.id,
        base_version: window.__WL_VERSION__,
      }),
    })
      .then((r) => Promise.all([r.status, r.json()]))
      .then(([status, res]) => {
        setBusy(false); // endorse never reloads — re-enable on every outcome
        if (status === 409) {
          showStaleMessage();
          return;
        }
        if (status === 404) {
          // The annotation vanished server-side (deleted/recreated under us).
          console.warn("WikiLean: endorse target not found server-side — reloading");
          $("wlr-status-msg").innerHTML =
            'That annotation no longer exists on the server — <a href="#" onclick="location.reload();return false">reload</a> to resync.';
          return;
        }
        if (!res.ok) {
          $("wlr-status-msg").textContent = "error: " + (res.error || "endorse failed");
          return;
        }
        if (typeof res.version === "number") window.__WL_VERSION__ = res.version;
        // Cheap local refresh — no reload needed: update the model, the
        // human-curated underline (wrap.ts uses representative provenance:
        // any human annotation in a group marks the whole wrap), and the
        // panel (the button only shows when there's something to flip).
        a.provenance = "human";
        document.querySelectorAll(".anno").forEach((el) => {
          const raw = el.dataset.annoIndices || el.dataset.annoIndex;
          if (raw == null) return;
          const hit = String(raw)
            .split(",")
            .some((s) => parseInt(s, 10) === idx);
          if (hit) el.dataset.provenance = "human";
        });
        $("wlr-mark-reviewed").style.display = "none";
        $("wlr-status-msg").textContent = "endorsed ✓ — now marked human-curated";
      })
      .catch((e) => {
        setBusy(false); // (fix #8)
        $("wlr-status-msg").textContent = "error: " + e;
      });
  }

  // Shared 409 handling: the article changed under us — never clobber the
  // newer revision, and never discard the typed work: the panel stays open
  // with the fields intact, and the draft is stashed in sessionStorage so a
  // reload-to-rebase can restore it (fix #9).
  function showStaleMessage() {
    stashDraft();
    $("wlr-status-msg").innerHTML =
      "This article changed since you loaded it — your draft is preserved; " +
      '<a href="#" onclick="location.reload();return false">reload</a> to rebase, then re-apply.';
  }

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
      // Stable annotation id (C1): stamp brand-new annotations client-side so
      // the id is known before the round-trip. 12 lowercase hex chars, same
      // contract as the server's lazy-heal (which backstops any client that
      // doesn't send one). Edits never touch ids — the {...prev, ...built}
      // spread below preserves them.
      built.id = newAnnotationId();
      unsavedNew.add(built);
      annos.push(built);
    } else {
      // Spread the original first so fields the form doesn't expose (proof_note,
      // id, formalizations, tombstone markers, moderation_flag, …) survive; the
      // form-derived fields in `built` win, so explicitly cleared fields (e.g. an
      // emptied note) are still written through.
      built.anchor = annos[editingIndex].anchor;
      const prev = annos[editingIndex];
      annos[editingIndex] = { ...prev, ...built };
      // The merge creates a new object — carry over never-persisted tracking.
      if (unsavedNew.delete(prev)) unsavedNew.add(annos[editingIndex]);
    }
    persist("saved");
  }

  function persist(verb) {
    setBusy(true); // no double-submit while the request is in flight (fix #8)
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
          setBusy(false); // (fix #8)
          showStaleMessage();
          return;
        }
        if (!res.ok) {
          setBusy(false); // (fix #8)
          $("wlr-status-msg").textContent = "error: " + (res.error || "save failed");
          return;
        }
        // Keep the in-page version in sync so a second save in the same session
        // doesn't spuriously 409.
        if (typeof res.version === "number") window.__WL_VERSION__ = res.version;
        // Everything in `annos` has now been persisted — deleting any of them
        // from here on must tombstone, not splice.
        unsavedNew.clear();
        const m = (res.matched || "").match(/(\d+)\/(\d+)/);
        if (m && m[1] !== m[2]) {
          setBusy(false); // save DID land; allow further edits (fix #8)
          $("wlr-status-msg").innerHTML =
            verb + ", but only " + res.matched + " anchored — an annotation's text didn't match the article. " +
            '<a href="#" onclick="location.reload();return false">reload anyway</a>';
        } else {
          // Stay disabled — the page is about to reload.
          $("wlr-status-msg").textContent = verb + " — reloading (" + (res.matched || "") + ")";
          setTimeout(() => location.reload(), 400);
        }
      })
      .catch((e) => {
        setBusy(false); // (fix #8)
        $("wlr-status-msg").textContent = "error: " + e;
      });
  }

  function escapeHtml(s) {
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  // 12-char lowercase-hex annotation id (6 crypto-random bytes), collision-
  // checked against the ids already on this article.
  function newAnnotationId() {
    for (;;) {
      const bytes = new Uint8Array(6);
      crypto.getRandomValues(bytes);
      let id = "";
      for (let i = 0; i < bytes.length; i++) id += bytes[i].toString(16).padStart(2, "0");
      if (!annos.some((a) => a && a.id === id)) return id;
    }
  }
})();
