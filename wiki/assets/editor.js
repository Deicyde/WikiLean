// Pure, immutable editor operations. Kept in this served asset so the browser
// and regression tests exercise the same preservation logic.
function createCompanionEditorModel() {
  const profile = "urn:wikilean:anchor:v3";
  const clone = value => JSON.parse(JSON.stringify(value));
  const native = item => !!item && ("links" in item || "unlinked" in item);
  function entries(item) {
    return item.links.map((link, index) => ({ key: "link:" + link.id, kind: "link", index, occurrence: link.annotation, base: link.base_uri }))
      .concat(item.unlinked.map((occurrence, index) => ({ key: "unlinked:" + index, kind: "unlinked", index, occurrence, base: occurrence.base_uri })));
  }
  function selected(item, key) { return entries(item).find(entry => entry.key === key); }
  function makeItem(source, anchor) {
    if (!source) throw new Error("The article has no pinned source revision. A source revision is required before adding a location.");
    return { links: [], unlinked: [{ source: { type: "generic", uri: source }, location: { profile, anchor: clone(anchor) } }] };
  }
  function anchor(entry, source) {
    if (!entry || !source) return null;
    const occ = entry.occurrence;
    let uri;
    try { uri = entry.base ? new URL(occ.source.uri, entry.base).href : new URL(occ.source.uri).href; } catch (_) { return null; }
    return uri === source && occ.source.type === "generic" && occ.location && occ.location.profile === profile && occ.location.anchor
      ? occ.location.anchor : null;
  }
  function edit(item, key, change, newId) {
    const next = clone(item), entry = selected(next, key);
    if (!entry) throw new Error("Choose a source location first.");
    const occ = entry.occurrence;
    if (change.anchor) {
      if (change.reanchorSource) {
        occ.source = { type: "generic", uri: change.reanchorSource };
        occ.location = { profile, anchor: clone(change.anchor) };
      } else {
        if (occ.source.type !== "generic" || !occ.location || occ.location.profile !== profile)
          throw new Error("This location cannot be edited as Wikipedia text. Use a new article selection to replace it explicitly.");
        occ.location = { ...occ.location, anchor: { ...occ.location.anchor, ...clone(change.anchor) } };
      }
    }
    if ("target" in change && change.target !== (occ.target || "")) {
      const target = change.target.trim();
      if (target && /\s/.test(target)) throw new Error("The formal reference must not contain spaces; use an encoded URI.");
      if (entry.kind === "link") {
        if (target) occ.target = target;
        else {
          const unlinked = { source: clone(occ.source) };
          if ("location" in occ) unlinked.location = clone(occ.location);
          if (entry.base) unlinked.base_uri = entry.base;
          next.unlinked.push(unlinked);
          next.links.splice(entry.index, 1);
          return { item: next, key: "unlinked:" + (next.unlinked.length - 1) };
        }
      } else if (target) {
        const annotation = { source: clone(occ.source), target };
        if ("location" in occ) annotation.location = clone(occ.location);
        const link = { id: newId(), annotation };
        if (entry.base) link.base_uri = entry.base;
        next.links.push(link);
        next.unlinked.splice(entry.index, 1);
        return { item: next, key: "link:" + link.id };
      }
    }
    return { item: next, key };
  }
  function add(item, key, target, newId) {
    if (!target || /\s/.test(target)) throw new Error("Enter a formal reference without spaces.");
    const next = clone(item), entry = selected(next, key);
    if (!entry) throw new Error("Choose the source location for the additional link.");
    const annotation = { source: clone(entry.occurrence.source), target };
    if ("location" in entry.occurrence) annotation.location = clone(entry.occurrence.location);
    const link = { id: newId(), annotation };
    if (entry.base) link.base_uri = entry.base;
    next.links.push(link);
    return { item: next, key: "link:" + link.id };
  }
  function selectionAfterEdit(before, editedKey, requestedKey, after, resultKey) {
    if (requestedKey === editedKey) return resultKey;
    const requested = selected(before, requestedKey), edited = selected(before, editedKey);
    if (!requested || requested.kind === "link") return requestedKey;
    // Promoting an unlinked occurrence removes one array entry. Preserve the
    // source the contributor selected, including identical duplicate sources.
    const promoted = edited && edited.kind === "unlinked" && after.unlinked.length === before.unlinked.length - 1;
    return "unlinked:" + (requested.index - (promoted && edited.index < requested.index ? 1 : 0));
  }
  return { clone, native, entries, selected, makeItem, anchor, edit, add, selectionAfterEdit };
}
// ---- editor runtime ----
// WikiLean live-wiki editor. Injected on article pages for logged-in users by
// the Worker (window.__WL_SLUG__, __WL_USER__, __WL_FULL_ANNOS__ are set first).
// Adapted from site/assets/review.js — same interaction model, but saves to the
// Worker (/api/article/:slug) instead of the localhost review server.
//
//   - Click a highlight (no text selected) → edit that annotation.
//   - Alt-click a highlight → open its Mathlib docs page directly (when the
//     annotation carries a mathlib_url); plain click keeps the edit behavior.
//   - Select text → "＋ Annotate" button → create a new annotation over it.
//   - Orphaned annotations (anchor text no longer matches the article) are
//     reachable from the top bar's "N orphaned" button and re-anchored from a
//     fresh selection via the panel's Re-anchor button.
//   - Save POSTs the full annotation array; the server re-renders + records a
//     revision; the page reloads to show the result.
(function () {
  "use strict";
  const slug = window.__WL_SLUG__;
  const user = window.__WL_USER__ || { name: "you" };
  const model = window.__WL_FULL_ANNOS__ || { annotations: [] };
  const annos = model.annotations;
  const companions = createCompanionEditorModel();
  const nativeMode = window.__WL_SCHEMA_VERSION__ === 4 || annos.some(companions.native);
  let nativeDraft = null, nativeKey = "", nativeAnchorBaseline = "";
  const sourceUri = window.__WL_SOURCE__;

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
  // ---- orphaned-anchor detection (save-UX e) ----
  // An annotation renders no highlight for one of TWO reasons, and the editor
  // must not conflate them (12/773 corpus pages hit the second):
  //   rot        — the anchor no longer matches the article text
  //                (__WL_MATCHED__[i] === false). Re-anchor fixes it.
  //   suppressed — the anchor MATCHED but the wrap engine's partial-overlap
  //                protection dropped the highlight (matched true, yet the
  //                index appears in no .anno wrap). The anchor is healthy;
  //                Re-anchor would not restore the highlight and re-saving
  //                would only flip provenance to human. No Re-anchor offered.
  // The DOM is the wrap engine's real output for this page; __WL_MATCHED__ is
  // its real per-annotation match result (tombstones report true). On older
  // cached pages without __WL_MATCHED__ every unwrapped index degrades to the
  // pre-split behavior (treated as rot).
  const matchedArr = window.__WL_MATCHED__;
  const wrappedIdxs = new Set();
  document.querySelectorAll(".anno").forEach(function (el) {
    const raw = el.dataset.annoIndices || el.dataset.annoIndex;
    if (raw == null) return;
    String(raw).split(",").forEach(function (s) {
      const n = parseInt(s, 10);
      if (!Number.isNaN(n)) wrappedIdxs.add(n);
    });
  });
  const orphanIdxs = [];
  const suppressedIdxs = [];
  annos.forEach(function (a, i) {
    if (!a || a.status === "rejected" || (wrappedIdxs.has(i) && !(companions.native(a) && Array.isArray(matchedArr) && matchedArr[i] === false))) return;
    if (Array.isArray(matchedArr) && matchedArr[i] === true) suppressedIdxs.push(i);
    else orphanIdxs.push(i);
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
    // Orphaned = the anchor text no longer matches the article (anchor rot),
    // so no highlight renders. Each click cycles to the next one so it can be
    // re-anchored from a fresh selection (save-UX e).
    (orphanIdxs.length
      ? '<button id="wlr-orphans" type="button" title="These annotations&#39; anchor text no longer matches the article, so they render no highlight — open one, select the right text in the article, and click Re-anchor">' +
        orphanIdxs.length + " locations not displayed" + "</button>"
      : "") +
    // Suppressed = anchor healthy, highlight dropped by overlap protection.
    // Reachable for editing, but with truthful copy and no Re-anchor.
    (suppressedIdxs.length
      ? '<button id="wlr-suppressed" type="button" title="These annotations&#39; anchors match, but their highlights overlap another annotation&#39;s and were not drawn — the annotation itself is intact and editable">' +
        suppressedIdxs.length + " hidden by overlap" + "</button>"
      : "") +
    '<span style="opacity:.85">click a highlight to edit · select text to add · alt-click a highlight opens its formal reference</span>' +
    '<span class="wlr-spacer"></span>' +
    // P3: name links to /u/<id> when the id is known (older cached pages
    // without __WL_USER__.id fall back to plain text).
    '<span style="opacity:.85">editing as ' +
      (user.id
        ? '<a href="/u/' + encodeURIComponent(user.id) + '" style="color:#fff;text-decoration:underline">' + escapeHtml(user.name) + "</a>"
        : escapeHtml(user.name)) +
      "</span>" +
    // P3: ★ Watch / ★ Watching toggle. Initial state from window.__WL_WATCHING__.
    '<button id="wlr-watch" type="button" data-watching="' + (window.__WL_WATCHING__ ? "1" : "0") +
      '" style="margin-left:12px">' +
      (window.__WL_WATCHING__ ? "★ Watching" : "☆ Watch") + "</button>" +
    '<a href="/' + encodeURIComponent(slug) + '/history" style="color:#fff;margin-left:12px">history</a>' +
    '<a href="/logout?returnTo=' + ret + '" style="color:#fff;margin-left:12px">log out</a>';
  // Prepend (not append): on ≤640px review.css positions the bar sticky, which
  // only works from the top of the document flow (fix #5).
  document.body.prepend(bar);

  // ---- propose-then-approve: inline banner of pending AI proposals ----------
  // The AI may propose an update to a human annotation (docs/propose-then-
  // approve.md); the human approves/rejects here. Approve applies the delta
  // (keeping provenance:human); reject remembers it so it isn't re-proposed.
  renderProposalsBanner();
  function renderProposalsBanner() {
    var proposals = window.__WL_PROPOSALS__ || [];
    if (!proposals.length) return;
    injectProposalStyle();
    var byId = {};
    annos.forEach(function (a) { if (a && a.id) byId[a.id] = a; });
    var banner = document.createElement("div");
    banner.id = "wlr-proposals";
    banner.setAttribute("role", "region");
    banner.setAttribute("aria-label", "AI proposals to review");
    var head = document.createElement("div");
    head.className = "wlr-prop-head";
    head.textContent = "⚡ " + proposals.length + " AI proposal" + (proposals.length === 1 ? "" : "s") +
      " to update your annotations";
    banner.appendChild(head);
    proposals.forEach(function (p) {
      var target = byId[p.annotationId];
      var label = target && target.label ? target.label : p.annotationId;
      var row = document.createElement("div");
      row.className = "wlr-prop";
      row.dataset.pid = p.proposalId;
      row.innerHTML =
        '<div class="wlr-prop-body"><b>' + escapeHtml(String(label)) + "</b>: " +
          describeDelta(p.fields, target) +
          (p.reason ? '<div class="wlr-prop-reason">' + escapeHtml(String(p.reason)) + "</div>" : "") +
        "</div>" +
        '<div class="wlr-prop-actions">' +
          '<button type="button" class="wlr-prop-approve">✓ Approve</button>' +
          '<button type="button" class="wlr-prop-reject">✗ Reject</button>' +
        "</div>";
      row.querySelector(".wlr-prop-approve").addEventListener("click", function () {
        actProposal(row, p.proposalId, "approve_proposal");
      });
      row.querySelector(".wlr-prop-reject").addEventListener("click", function () {
        actProposal(row, p.proposalId, "reject_proposal");
      });
      banner.appendChild(row);
    });
    bar.insertAdjacentElement("afterend", banner);
  }
  function describeDelta(fields, current) {
    return Object.keys(fields || {}).map(function (k) {
      var from = current ? current[k] : undefined;
      var fromStr = from !== undefined && from !== null && typeof from !== "object"
        ? escapeHtml(String(from)) + " → " : "→ ";
      return "<code>" + escapeHtml(k) + "</code> " + fromStr + "<b>" + escapeHtml(fmtVal(fields[k])) + "</b>";
    }).join(" · ");
  }
  function fmtVal(v) {
    if (v === null || v === undefined) return "∅";
    if (typeof v === "object") return v.decl ? String(v.decl) : JSON.stringify(v);
    return String(v);
  }
  function actProposal(row, pid, action) {
    var btns = row.querySelectorAll("button");
    btns.forEach(function (b) { b.disabled = true; });
    var body = { action: action, proposal_id: pid };
    if (action === "approve_proposal") body.base_version = window.__WL_VERSION__;
    fetch("/api/article/" + encodeURIComponent(slug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function (res) { return res.json().then(function (j) { return { status: res.status, j: j }; }); })
      .then(function (r) {
        if (r.status === 200) {
          if (action === "approve_proposal") {
            window.location.reload(); // annotation content changed — show it applied
          } else {
            row.remove();
            var b = document.getElementById("wlr-proposals");
            if (b && !b.querySelector(".wlr-prop")) b.remove();
          }
        } else if (r.status === 409) {
          alert("This article changed since the page loaded — reloading.");
          window.location.reload();
        } else {
          alert("Could not " + (action === "approve_proposal" ? "approve" : "reject") +
            ": " + (r.j && r.j.error ? r.j.error : r.status));
          btns.forEach(function (b) { b.disabled = false; });
        }
      })
      .catch(function () {
        alert("Network error — try again.");
        btns.forEach(function (b) { b.disabled = false; });
      });
  }
  function injectProposalStyle() {
    if (document.getElementById("wlr-prop-style")) return;
    var st = document.createElement("style");
    st.id = "wlr-prop-style";
    st.textContent =
      "#wlr-proposals{background:#fbf3e0;border-bottom:1px solid #e6d5a8;padding:8px 16px;" +
        "font:14px -apple-system,BlinkMacSystemFont,sans-serif;color:#3d3223}" +
      "#wlr-proposals .wlr-prop-head{font-weight:600;margin-bottom:6px;color:#8a6d1f}" +
      "#wlr-proposals .wlr-prop{display:flex;align-items:flex-start;gap:12px;padding:6px 0;" +
        "border-top:1px solid #efe3c4}" +
      "#wlr-proposals .wlr-prop-body{flex:1;line-height:1.5}" +
      "#wlr-proposals .wlr-prop-reason{color:#6b5b3a;font-size:.85em;margin-top:2px}" +
      "#wlr-proposals code{background:#efe3c4;padding:1px 5px;border-radius:3px;font-size:.85em}" +
      "#wlr-proposals .wlr-prop-actions{display:flex;gap:6px;flex-shrink:0}" +
      "#wlr-proposals button{border:1px solid #d9c489;background:#fff;border-radius:6px;" +
        "padding:4px 10px;cursor:pointer;font:inherit}" +
      "#wlr-proposals .wlr-prop-approve{background:#2da44e;color:#fff;border-color:#238636}" +
      "#wlr-proposals .wlr-prop-reject{color:#a4432b}" +
      "#wlr-proposals button:disabled{opacity:.5;cursor:default}";
    document.head.appendChild(st);
  }
  if (rejectedIdxs.length) {
    // Each click opens the next hidden annotation in the editor panel,
    // cycling through all of them (fix #2).
    let hiddenCursor = 0;
    bar.querySelector("#wlr-hidden").addEventListener("click", function (e) {
      openEditor(rejectedIdxs[hiddenCursor % rejectedIdxs.length], e.currentTarget);
      hiddenCursor++;
    });
  }
  if (suppressedIdxs.length) {
    // Same cycling pattern for overlap-suppressed annotations.
    let suppressedCursor = 0;
    bar.querySelector("#wlr-suppressed").addEventListener("click", function (e) {
      openEditor(suppressedIdxs[suppressedCursor % suppressedIdxs.length], e.currentTarget);
      suppressedCursor++;
    });
  }
  if (orphanIdxs.length) {
    // Same cycling pattern for orphaned (anchor-rot) annotations.
    let orphanCursor = 0;
    bar.querySelector("#wlr-orphans").addEventListener("click", function (e) {
      openEditor(orphanIdxs[orphanCursor % orphanIdxs.length], e.currentTarget);
      orphanCursor++;
    });
  }

  // ---- P3: ★ Watch toggle ---------------------------------------------------
  const watchBtn = bar.querySelector("#wlr-watch");
  if (watchBtn) {
    watchBtn.addEventListener("click", async function () {
      const watching = watchBtn.dataset.watching === "1";
      watchBtn.disabled = true;
      try {
        const res = await fetch("/api/watch/" + encodeURIComponent(slug), {
          method: watching ? "DELETE" : "POST",
        });
        if (!res.ok) throw new Error("watch toggle failed: " + res.status);
        const next = !watching;
        watchBtn.dataset.watching = next ? "1" : "0";
        watchBtn.textContent = next ? "★ Watching" : "☆ Watch";
      } catch (e) {
        // Re-enable on failure; leave label as-is so the user can retry.
        console.warn(e);
      } finally {
        watchBtn.disabled = false;
      }
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
    <fieldset id="wlr-companions" hidden>
      <legend>Formal links</legend>
      <label for="wlr-f-occurrence">Source location</label>
      <select id="wlr-f-occurrence"></select>
      <small id="wlr-source-info" class="wlr-help"></small>
      <label for="wlr-f-target">Formal reference</label>
      <input id="wlr-f-target" placeholder="https://… or another formal URI" autocomplete="off">
      <small class="wlr-help">Each link connects this source location to one formal reference. Clearing the reference keeps the source location as work still to do.</small>
      <button id="wlr-add-link" type="button">Add another link at this location</button>
    </fieldset>
    <fieldset id="wlr-f-anchor-set" class="wlr-anchor-set">
      <legend>Highlight range
        <button type="button" id="wlr-f-anchor-pick" title="Replace this annotation's range with whatever text is currently selected in the article">Use selection</button>
        <button type="button" id="wlr-f-anchor-reanchor" style="display:none" title="This annotation's anchor no longer matches the article. Select the correct text in the article, then click to rewrite the anchor from that selection.">Re-anchor</button>
      </legend>
      <small class="wlr-help">What text in the article this annotation wraps. Edit to resize / re-anchor the box. Snippet must be a substring of the section's prose.</small>
      <label for="wlr-f-anchor-section">Section</label>
      <input id="wlr-f-anchor-section" placeholder="Section heading (e.g. 'Definition', or '(Lead)' for the lead)" autocomplete="off">
      <label for="wlr-f-anchor-snippet">Snippet</label>
      <textarea id="wlr-f-anchor-snippet" rows="2" placeholder="Text that appears verbatim in the section"></textarea>
      <small id="wlr-f-anchor-locked" class="wlr-help wlr-anchor-locked-msg" style="display:none">This annotation uses a typed anchor (<span id="wlr-f-anchor-type"></span>) — direct re-anchoring isn't supported in the editor yet. Delete and recreate to change its range.</small>
    </fieldset>
    <label for="wlr-f-status">Status</label>
    <small class="wlr-help">Does the formal result capture this statement?</small>
    <select id="wlr-f-status">
      <option value="formalized">formalized</option>
      <option value="partial">partial</option>
      <option value="not_formalized">not_formalized</option>
      <option value="rejected">rejected (hide)</option>
    </select>
    <label for="wlr-f-kind">Kind</label>
    <small class="wlr-help">What sort of statement this annotates.</small>
    <select id="wlr-f-kind">
      <option value="">—</option>
      <option value="definition">definition</option>
      <option value="theorem">theorem</option>
      <option value="proposition">proposition</option>
      <option value="example">example</option>
    </select>
    <label for="wlr-f-label">Label</label>
    <small class="wlr-help">Short display name shown at the top of the tooltip.</small>
    <input id="wlr-f-label" placeholder="e.g. Abelianization of a group">
    <div id="wlr-legacy-formal">
    <label for="wlr-f-decl">Mathlib decl</label>
    <small class="wlr-help">Type 2+ chars for autocomplete (full Mathlib index; decls already used on WikiLean rank first). Picking a suggestion auto-fills the module.</small>
    <input id="wlr-f-decl" placeholder="e.g. Ideal.IsPrime" list="wlr-decl-options" autocomplete="off">
    <datalist id="wlr-decl-options"></datalist>
    <div id="wlr-decl-check" aria-live="polite" style="font-size:12px;min-height:15px;margin:2px 0 4px"></div>
    <label for="wlr-f-module">Mathlib module</label>
    <small class="wlr-help">Dotted path, auto-filled from the decl autocomplete.</small>
    <input id="wlr-f-module" placeholder="e.g. Mathlib.RingTheory.Ideal.Prime">
    </div>
    <label for="wlr-f-match">match_kind</label>
    <small class="wlr-help">How the formal result relates to the statement.</small>
    <select id="wlr-f-match">
      <option value="">—</option>
      <option value="exact">exact</option>
      <option value="generalization">generalization</option>
      <option value="special_case">special_case</option>
      <option value="invocation">invocation</option>
    </select>
    <label for="wlr-f-note">Note</label>
    <small class="wlr-help">Review notes for editors.</small>
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

  // Dialog focus lifecycle. Capture the launch point before focus moves into
  // the panel, keep it stable while the panel is open, and restore it after a
  // confirmed close. Picker rows and the hidden FAB are deliberately excluded:
  // both are ephemeral launch controls, so their annotation/article target is
  // passed explicitly by those call sites instead.
  let dialogOpener = null;
  let dialogFallback = null;
  function isStableFocusTarget(el) {
    return el instanceof HTMLElement && el.isConnected && !panel.contains(el) &&
      !el.closest("#wlr-picker") && el !== fab && !el.closest("#wlr-intro");
  }
  function defaultDialogFallback() {
    return bar.querySelector("button:not([disabled]),a[href]") ||
      document.querySelector(".wl-article-body");
  }
  function rememberDialogOpener(preferred, fallback) {
    // Opening another annotation while the dialog is already open must not
    // replace the original launch point with the currently focused form field.
    if (panel.classList.contains("open")) return;
    const active = document.activeElement;
    dialogOpener = isStableFocusTarget(preferred)
      ? preferred
      : isStableFocusTarget(active)
        ? active
        : null;
    dialogFallback = isStableFocusTarget(fallback) ? fallback : defaultDialogFallback();
  }
  function focusProgrammatically(el) {
    if (!isStableFocusTarget(el)) return false;
    if (!el.matches("a[href],button,input,select,textarea,[tabindex]")) {
      el.setAttribute("tabindex", "-1");
    }
    try {
      el.focus({ preventScroll: true });
    } catch (_) {
      el.focus();
    }
    return true;
  }
  function restoreDialogFocus() {
    const target = isStableFocusTarget(dialogOpener) ? dialogOpener : dialogFallback;
    dialogOpener = null;
    dialogFallback = null;
    focusProgrammatically(target);
  }
  function focusableDialogControls() {
    const selector = 'a[href],button,input,select,textarea,[tabindex]:not([tabindex="-1"])';
    return Array.from(panel.querySelectorAll(selector)).filter((el) => {
      if (el.disabled || el.hidden || el.getAttribute("aria-hidden") === "true") return false;
      const style = window.getComputedStyle(el);
      return style.display !== "none" && style.visibility !== "hidden" && el.getClientRects().length > 0;
    });
  }
  function trapDialogTab(e) {
    const controls = focusableDialogControls();
    if (!controls.length) {
      e.preventDefault();
      panel.setAttribute("tabindex", "-1");
      panel.focus();
      return;
    }
    const first = controls[0];
    const last = controls[controls.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || !panel.contains(active))) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && (active === last || !panel.contains(active))) {
      e.preventDefault();
      first.focus();
    }
  }

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
      // Alt-click: jump straight to the Mathlib docs for this highlight (the
      // same mathlib_url the anonymous tooltip uses — computed server-side by
      // page.ts buildClientData and index-aligned with annos, so no URL
      // construction is forked here). Falls through to the normal editor when
      // no annotation under the click carries a docs link.
      if (e.altKey) {
        const cd = window.__WL_ANNOTATIONS__ || [];
        for (let k = 0; k < idxs.length; k++) {
          const c = cd[idxs[k]];
          if (c && c.mathlib_url) {
            window.open(c.mathlib_url, "_blank", "noopener");
            return;
          }
        }
      }
      document.querySelectorAll(".anno.wlr-selected").forEach((n) => n.classList.remove("wlr-selected"));
      el.classList.add("wlr-selected");
      if (idxs.length === 1) {
        maybeShowIntro(() => openEditor(idxs[0], el));
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
      (nativeMode ? "<p>A review item can contain several source locations and formal links. Choose a source location before editing its formal reference.</p><ul><li><b>Formal reference</b> — the address or URI of one formal result. Add another link when the same source relates to several results.</li><li><b>Source location</b> — the selected part of an informal resource. Locations this viewer cannot display are retained. Re-anchor deliberately replaces the selected location with article text.</li><li><b>Status</b> — your assessment of how fully the formal results capture this statement.</li></ul>" :
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
      "</ul>") +
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
    // Include the decl in the option's visible text so Safari/Firefox (which
    // display the inner text, not the `value` attribute, in the datalist
    // dropdown) show the decl first with the module as a hint. Clicking still
    // inserts only the `value` (just the decl); the module is auto-filled by
    // the change handler below.
    list.innerHTML = out
      .map(
        ([d, m]) =>
          '<option value="' + escapeHtml(d) + '">' + escapeHtml(d) + " — " + escapeHtml(m) + "</option>",
      )
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
        // The row is removed before the dialog opens, so return focus to the
        // stable annotation element rather than this ephemeral picker button.
        openEditor(i, annoEl);
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
    } else if (e.key === "Tab" && panel.classList.contains("open")) {
      trapDialogTab(e);
    } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && panel.classList.contains("open")) {
      // Cmd/Ctrl+Enter saves, matching the Save button.
      e.preventDefault();
      save();
    }
  });

  // Cached article-body element — selections must live inside this to count as
  // an "annotate this prose" gesture. Outside (top bar, edit panel, intro
  // overlay, page footer, etc.) selecting text should never pop the FAB.
  const articleBody = document.querySelector(".wl-article-body");

  // THE anchor-construction path from a live selection, shared by the
  // ＋Annotate FAB, "Use selection", and Re-anchor (save-UX e) — one code path
  // means one sig behavior (the parity harness pins section+snippet anchors).
  // Returns {text, section} for a usable in-article selection, {outside:true}
  // for a selection outside the article body, and null for no/too-short
  // selection.
  function selectionAnchor(minLen) {
    const sel = window.getSelection();
    const text = sel ? sel.toString().trim() : "";
    if (!sel || sel.isCollapsed || text.length < minLen) return null;
    // Both endpoints must be inside the article body. Without this, selecting
    // the editor panel's help text (e.g. "Status — Does Mathlib4 capture this
    // statement?") would count as an "annotate this prose" gesture.
    if (
      !articleBody ||
      !articleBody.contains(sel.anchorNode) ||
      !articleBody.contains(sel.focusNode)
    ) {
      return { outside: true };
    }
    return { text: text, section: nearestSection(sel.anchorNode) };
  }

  function maybeShowFab() {
    const got = selectionAnchor(4);
    if (!got || got.outside) {
      fab.style.display = "none";
      return;
    }
    const rect = window.getSelection().getRangeAt(0).getBoundingClientRect();
    pendingSel = got;
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
    // The FAB is hidden as the dialog opens; the article body is the stable,
    // relevant return point for an annotation created from a text selection.
    openAdder(pendingSel.text, pendingSel.section, articleBody);
  });

  function mathlib(a) {
    return a.mathlib || {};
  }

  // ---- form-state helpers (fixes #8/#9/#11c) --------------------------------

  // Set a <select>'s value, preserving any unknown stored value as an extra
  // option so old data round-trips through the dropdown byte-identically
  // (the corpus holds legacy kinds like "conjecture"/"remark" and match_kinds
  // like "related" that predate the canonical vocabulary). Extra options are
  // tagged data-extra and swept on the next populate so they never accumulate.
  function setSelectValue(sel, value) {
    sel.querySelectorAll("option[data-extra]").forEach(function (o) { o.remove(); });
    const v = value == null ? "" : String(value);
    let known = false;
    for (let i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === v) { known = true; break; }
    }
    if (!known) {
      const opt = document.createElement("option");
      opt.value = v;
      opt.textContent = v + " (stored value)";
      opt.setAttribute("data-extra", "1");
      sel.appendChild(opt);
    }
    sel.value = v;
  }

  // Panel title: the annotation's label (or its quoted anchor text), truncated
  // to ~40 chars — "Edit annotation #7" told a contributor nothing (save-UX c).
  function truncateTitle(s) {
    s = String(s == null ? "" : s).trim();
    return s.length > 40 ? s.slice(0, 39).replace(/\s+$/, "") + "…" : s;
  }

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
      // Include anchor fields so editing the highlight range counts as an
      // unsaved change (and the 409 draft-restore captures it).
      anchorSection: $("wlr-f-anchor-section").value,
      anchorSnippet: $("wlr-f-anchor-snippet").value,
      nativeDraft: nativeDraft, nativeKey: nativeKey,
      nativeTarget: $("wlr-f-target").value,
      nativeReanchor: panel.dataset.reanchor === "1",
      nativeAnchorBaseline: nativeAnchorBaseline,
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
    // Drafts stashed before the select conversion may hold free-typed values —
    // setSelectValue preserves them as extra options instead of dropping them.
    setSelectValue($("wlr-f-kind"), d.kind || "");
    $("wlr-f-label").value = d.label || "";
    $("wlr-f-decl").value = d.decl || "";
    $("wlr-f-module").value = d.module || "";
    setSelectValue($("wlr-f-match"), d.match || "");
    $("wlr-f-note").value = d.note || "";
    $("wlr-f-comment").value = d.comment || "";
    if (nativeMode && d.nativeDraft) {
      nativeDraft = d.nativeDraft;
      nativeKey = d.nativeKey;
      populateNative();
      $("wlr-f-target").value = d.nativeTarget || "";
      nativeAnchorBaseline = d.nativeAnchorBaseline || nativeAnchorBaseline;
      if (d.nativeReanchor) { populateAnchorFields({}); panel.dataset.reanchor = "1"; }
    }
    if (typeof d.anchorSection === "string") $("wlr-f-anchor-section").value = d.anchorSection;
    if (typeof d.anchorSnippet === "string") $("wlr-f-anchor-snippet").value = d.anchorSnippet;
    return true;
  }

  // Disable the action buttons while a request is in flight so a double-click
  // can't double-submit; every failure path re-enables them (fix #8).
  function setBusy(busy) {
    ["wlr-save", "wlr-mark-reviewed", "wlr-del"].forEach(function (id) {
      $(id).disabled = busy;
    });
  }

  function openEditor(idx, opener) {
    rememberDialogOpener(opener);
    editingIndex = idx;
    const a = annos[idx];
    const m = mathlib(a);
    // Title = label (preferred) or the anchor's quoted text, truncated. The
    // #index fallback only fires when an annotation has neither (textContent —
    // labels and snippets are hostile user text).
    const t = truncateTitle(a.label || quoteOf(a));
    $("wlr-title").textContent = t ? "Edit: " + t : "Edit annotation #" + idx;
    $("wlr-quote").textContent = quoteOf(a);
    $("wlr-f-status").value = a.status || "not_formalized";
    setSelectValue($("wlr-f-kind"), a.kind || "");
    $("wlr-f-label").value = a.label || "";
    $("wlr-f-decl").value = m.decl || a.decl || "";
    $("wlr-f-module").value = m.module || a.module || "";
    setSelectValue($("wlr-f-match"), m.match_kind || a.match_kind || "");
    $("wlr-f-note").value = a.note || "";
    populateAnchorFields(a.anchor || {});
    beginNative(a);
    setDeclCheck("", true);
    $("wlr-del").style.display = "";
    // Show the one-click endorsement only when there's something to flip.
    $("wlr-mark-reviewed").style.display = a.provenance !== "human" ? "" : "none";
    // Orphaned-anchor state (save-UX e): a fresh open never inherits a stale
    // re-anchor flag; the Re-anchor affordance shows only for orphans.
    delete panel.dataset.reanchor;
    const isOrphan = orphanIdxs.indexOf(idx) !== -1;
    const isSuppressed = suppressedIdxs.indexOf(idx) !== -1;
    $("wlr-f-anchor-reanchor").style.display = (isOrphan || nativeDraft) && sourceUri ? "" : "none";
    $("wlr-status-msg").textContent =
      a.status === "rejected"
        ? "This annotation is rejected (hidden from readers). Pick another status and save to restore it."
        : isOrphan
          ? "One or more source locations are not displayed here. Choose a location to inspect it. To replace it, select article text and use Re-anchor."
          : isSuppressed
            ? "This annotation's anchor matches, but its highlight overlaps another annotation's and is not drawn. The annotation itself is intact — no re-anchoring needed."
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

  function openAdder(text, section, opener) {
    rememberDialogOpener(opener);
    editingIndex = null;
    panel.dataset.newSnippet = text;
    panel.dataset.newSection = section;
    const t = truncateTitle(text);
    $("wlr-title").textContent = t ? "New: " + t : "New annotation";
    $("wlr-quote").textContent = text + "   [§ " + section + "]";
    $("wlr-f-status").value = "not_formalized";
    ["label", "decl", "module"].forEach((f) => ($("wlr-f-" + f).value = ""));
    setSelectValue($("wlr-f-kind"), "");
    setSelectValue($("wlr-f-match"), "");
    $("wlr-f-note").value = "";
    // New annotations always use the section+snippet anchor type, so the
    // fields are editable from the start.
    populateAnchorFields({ section, snippet: text });
    beginNative(null, { section, snippet: text });
    setDeclCheck("", true);
    $("wlr-del").style.display = "none";
    // New annotations become human-curated on save, so nothing to mark separately.
    $("wlr-mark-reviewed").style.display = "none";
    // Not an orphan flow — no Re-anchor button, no stale flag.
    delete panel.dataset.reanchor;
    $("wlr-f-anchor-reanchor").style.display = "none";
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
    delete panel.dataset.reanchor;
    document.querySelectorAll(".anno.wlr-selected").forEach((n) => n.classList.remove("wlr-selected"));
    $("wlr-status-msg").textContent = "";
    restoreDialogFocus();
  }
  // Escape/× with unsaved changes ask before discarding (fix #11c).
  function requestClose() {
    if (fieldSnapshot() !== openSnapshot && !confirm("Discard unsaved changes?")) return;
    closePanel();
  }
  $("wlr-close").addEventListener("click", requestClose);

  function quoteOf(a) {
    if (companions.native(a)) {
      const entry = companions.entries(a)[0];
      const anchor = companions.anchor(entry, sourceUri);
      return anchor ? anchor.snippet || anchor.value || anchor.from || entry.occurrence.source.uri : entry ? entry.occurrence.source.uri : "No source location";
    }
    const anc = a.anchor || {};
    return anc.snippet || anc.value || anc.from || "(" + (anc.section || "?") + ")";
  }

  function beginNative(item, anchor) {
    nativeDraft = null; nativeKey = "";
    $("wlr-companions").hidden = !nativeMode;
    $("wlr-legacy-formal").hidden = nativeMode;
    if (!nativeMode) return;
    try { nativeDraft = item ? companions.clone(item) : companions.makeItem(sourceUri, anchor); }
    catch (error) { $("wlr-status-msg").textContent = error.message; return; }
    populateNative();
  }
  function populateNative() {
    if (!nativeDraft) return;
    const entries = companions.entries(nativeDraft);
    const select = $("wlr-f-occurrence");
    select.replaceChildren();
    entries.forEach(function (entry, index) {
      const option = document.createElement("option");
      option.value = entry.key;
      option.textContent = (index + 1) + ". " + (entry.kind === "link" ? entry.occurrence.target : "No formal link yet");
      select.appendChild(option);
    });
    if (!entries.some(entry => entry.key === nativeKey)) nativeKey = entries[0] ? entries[0].key : "";
    select.value = nativeKey;
    const entry = companions.selected(nativeDraft, nativeKey);
    $("wlr-f-target").value = entry && entry.kind === "link" ? entry.occurrence.target : "";
    $("wlr-source-info").textContent = entry ? entry.occurrence.source.type + " · " + entry.occurrence.source.uri : "No source location. Select article text and use Re-anchor to add one.";
    const anchor = companions.anchor(entry, sourceUri);
    populateAnchorFields(anchor || { type: "source location retained; select article text to replace" });
    nativeAnchorBaseline = JSON.stringify([$("wlr-f-anchor-section").value, $("wlr-f-anchor-snippet").value]);
    delete panel.dataset.reanchor;
  }
  function flushNative() {
    if (!nativeMode) return;
    if (!nativeDraft) throw new Error("A pinned article source is required before creating this annotation.");
    const change = { target: $("wlr-f-target").value.trim() };
    const section = $("wlr-f-anchor-section").value, snippet = $("wlr-f-anchor-snippet").value;
    const reanchor = panel.dataset.reanchor === "1";
    if (reanchor || JSON.stringify([section, snippet]) !== nativeAnchorBaseline) {
      change.anchor = { section, snippet };
      if (reanchor) {
        if (!sourceUri) throw new Error("A pinned article source is required to replace a location.");
        change.reanchorSource = sourceUri;
      }
    }
    if (!nativeKey) {
      if (!reanchor && change.target) throw new Error("Select article text and use Re-anchor before adding a formal reference.");
      if (!reanchor) return; // An empty work item is valid and remains empty on metadata edits.
      nativeDraft.unlinked.push(companions.makeItem(sourceUri, change.anchor).unlinked[0]);
      nativeKey = "unlinked:" + (nativeDraft.unlinked.length - 1);
    }
    const result = companions.edit(nativeDraft, nativeKey, change, newLinkId);
    nativeDraft = result.item; nativeKey = result.key;
  }
  function newLinkId() { return "link:" + crypto.randomUUID(); }
  $("wlr-f-occurrence").addEventListener("change", function () {
    const nextKey = this.value;
    try {
      const before = nativeDraft, editedKey = nativeKey;
      flushNative();
      nativeKey = companions.selectionAfterEdit(before, editedKey, nextKey, nativeDraft, nativeKey);
      populateNative();
    }
    catch (error) { this.value = nativeKey; $("wlr-status-msg").textContent = error.message; }
  });
  $("wlr-add-link").addEventListener("click", function () {
    try {
      flushNative();
      const target = prompt("Formal reference for the additional link:");
      if (target === null) return;
      const result = companions.add(nativeDraft, nativeKey, target.trim(), newLinkId);
      nativeDraft = result.item; nativeKey = result.key; populateNative();
    } catch (error) { $("wlr-status-msg").textContent = error.message; }
  });

  // Populate the Section/Snippet inputs from an anchor. Section+snippet anchors
  // (the dominant case — ~99% of annotations) become editable; typed anchors
  // (math_alttext / theorem_box / prose_range) display the anchor's text but
  // lock the inputs and show a "not editable yet" note instead of risking a
  // malformed write to a typed-anchor payload.
  function populateAnchorFields(anc) {
    const secInput = $("wlr-f-anchor-section");
    const snipInput = $("wlr-f-anchor-snippet");
    const pickBtn = $("wlr-f-anchor-pick");
    const lockedMsg = $("wlr-f-anchor-locked");
    const isTyped = typeof anc.type === "string" && anc.type.length > 0;
    if (isTyped) {
      secInput.value = anc.section || "";
      snipInput.value = anc.snippet || anc.value || anc.from || "";
      secInput.disabled = true;
      snipInput.disabled = true;
      pickBtn.disabled = true;
      $("wlr-f-anchor-type").textContent = anc.type;
      lockedMsg.style.display = "";
    } else {
      secInput.value = anc.section || "";
      snipInput.value = anc.snippet || "";
      secInput.disabled = false;
      snipInput.disabled = false;
      pickBtn.disabled = false;
      lockedMsg.style.display = "none";
    }
  }

  // "Use selection" button: replace the snippet (and section, if we can infer
  // one) with whatever the user currently has selected in the article body.
  // The same article-body containment guard that the FAB uses applies here.
  $("wlr-f-anchor-pick").addEventListener("click", () => {
    if ($("wlr-f-anchor-pick").disabled) return;
    const got = selectionAnchor(2);
    if (!got) {
      $("wlr-status-msg").textContent =
        "Select the new range in the article first, then click Use selection.";
      return;
    }
    if (got.outside) {
      $("wlr-status-msg").textContent =
        "The selection must be inside the article body (not the editor panel).";
      return;
    }
    $("wlr-f-anchor-snippet").value = got.text;
    if (got.section) $("wlr-f-anchor-section").value = got.section;
    $("wlr-status-msg").textContent = "Snippet updated from selection — Save to apply.";
  });

  // Re-anchor (save-UX e): for an orphaned annotation, rewrite the anchor
  // wholesale from the current selection — the SAME {section, snippet}
  // construction a brand-new annotation gets (selectionAnchor above). Typed
  // anchors (math_alttext / theorem_box / prose_range) are deliberately
  // replaceable here: the old payload stopped matching, and the replacement is
  // a fresh plain anchor, not an in-place mutation of the typed shape. Save
  // then goes through the normal save path (the server stamps provenance
  // 'human' for the changed annotation — correct and expected).
  // preventDefault on mousedown so clicking the button can't collapse the very
  // article selection it is about to read.
  $("wlr-f-anchor-reanchor").addEventListener("mousedown", (e) => e.preventDefault());
  $("wlr-f-anchor-reanchor").addEventListener("click", () => {
    const got = selectionAnchor(2);
    if (!got || got.outside) {
      $("wlr-status-msg").textContent = got && got.outside
        ? "The selection must be inside the article body (not the editor panel)."
        : "Select the annotation's correct text in the article first, then click Re-anchor.";
      return;
    }
    // Unlocks the fields too when the old anchor was typed — the replacement
    // is an ordinary section+snippet anchor.
    populateAnchorFields({ section: got.section, snippet: got.text });
    panel.dataset.reanchor = "1";
    $("wlr-status-msg").textContent = "Anchor rewritten from selection — Save to apply.";
  });

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
    if (nativeMode) { saveNative(); return; }
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

    // Build the anchor from the form. For section+snippet anchors (new + the
    // ~99% case for edits) the inputs ARE the anchor; for typed anchors the
    // inputs are disabled and we preserve the original payload to avoid
    // corrupting the typed shape.
    const formSection = $("wlr-f-anchor-section").value.trim();
    const formSnippet = $("wlr-f-anchor-snippet").value.trim();

    if (editingIndex == null) {
      built.anchor = { section: formSection, snippet: formSnippet };
      // Stable annotation id (C1): stamp brand-new annotations client-side so
      // the id is known before the round-trip. 12 lowercase hex chars, same
      // contract as the server's lazy-heal (which backstops any client that
      // doesn't send one). Edits never touch ids — the {...prev, ...built}
      // spread below preserves them.
      built.id = newAnnotationId();
      unsavedNew.add(built);
      annos.push(built);
    } else {
      const prev = annos[editingIndex];
      const prevAnchor = prev.anchor || {};
      const isTyped = typeof prevAnchor.type === "string" && prevAnchor.type.length > 0;
      // For section+snippet anchors (or untyped legacy anchors), accept the
      // form values — this is how a contributor resizes the highlight box.
      // For typed anchors, preserve the original payload to avoid risking a
      // malformed write — UNLESS the user explicitly re-anchored an orphan,
      // which replaces the whole anchor with a fresh section+snippet one.
      const reanchored = panel.dataset.reanchor === "1";
      built.anchor = isTyped && !reanchored ? prevAnchor : { section: formSection, snippet: formSnippet };
      // A re-anchor also drops any v3 multi-anchor array: the wrap engine
      // prefers anchors[] over anchor, so leaving the stale array in place
      // would make the rewrite a no-op. (The undefined survives the spread
      // below and JSON.stringify omits it from the POST.)
      if (reanchored) built.anchors = undefined;
      // Spread the original first so fields the form doesn't expose (proof_note,
      // id, formalizations, tombstone markers, moderation_flag, …) survive; the
      // form-derived fields in `built` win, so explicitly cleared fields (e.g. an
      // emptied note) are still written through.
      annos[editingIndex] = { ...prev, ...built };
      // The merge creates a new object — carry over never-persisted tracking.
      if (unsavedNew.delete(prev)) unsavedNew.add(annos[editingIndex]);
    }
    persist("saved");
  }

  function saveNative() {
    try { flushNative(); } catch (error) { $("wlr-status-msg").textContent = error.message; return; }
    const built = { ...nativeDraft,
      status: $("wlr-f-status").value,
      kind: $("wlr-f-kind").value.trim() || undefined,
      label: $("wlr-f-label").value.trim() || undefined,
      note: $("wlr-f-note").value.trim(),
      match_kind: $("wlr-f-match").value.trim() || undefined,
      provenance: "human",
    };
    if (editingIndex == null) {
      built.id = newAnnotationId(); unsavedNew.add(built); annos.push(built);
      editingIndex = annos.length - 1;
    } else {
      const prev = annos[editingIndex]; annos[editingIndex] = built;
      if (unsavedNew.delete(prev)) unsavedNew.add(built);
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
        // The edit summary was consumed by THIS save — clear it so the next
        // edit in the session doesn't silently reuse a stale comment (save-UX
        // b). Failure/409 paths keep it (the draft may be retried as-is).
        $("wlr-f-comment").value = "";
        const m = (res.matched || "").match(/(\d+)\/(\d+)/);
        if (m && m[1] !== m[2]) {
          setBusy(false); // save DID land; allow further edits (fix #8)
          // The panel stays open: re-baseline the unsaved-changes snapshot so
          // Escape/× doesn't prompt to discard changes that DID persist.
          openSnapshot = fieldSnapshot();
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
