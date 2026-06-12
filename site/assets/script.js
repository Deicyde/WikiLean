// WikiLean rendered-article interactivity.
// window.__WL_ANNOTATIONS__ is injected by render.py before this script runs.

(function () {
  "use strict";

  const annotations = window.__WL_ANNOTATIONS__ || [];
  const tooltip = document.getElementById("wl-tooltip");

  // ---- Toggle buttons ----
  document.querySelectorAll(".wl-toggles button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mode = btn.dataset.mode;
      document.querySelectorAll(".wl-toggles button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      document.body.classList.remove(
        "show-all",
        "show-formalized",
        "show-not_formalized",
        "show-dim"
      );
      document.body.classList.add("show-" + mode);
    });
  });

  // ---- Hover tooltips ----
  // The tooltip pins in place once shown and accepts pointer events, so the
  // user can move into it to click the Mathlib link. A short hide-delay timer
  // covers the gap between the anno and the tooltip.

  const HIDE_DELAY_MS = 220;
  let hideTimer = null;

  // The annotations currently shown in the tooltip (set by setTooltipContent)
  // so the flag micro-form can find the tapped annotation by its data-i index.
  let currentAnnos = [];
  // While the report form is open, mouseleave must not dismiss the tooltip
  // mid-typing; Escape / tap-outside / Cancel still close it.
  let flagFormOpen = false;

  function renderOneAnno(a, i) {
    const parts = [];
    if (a.label) {
      parts.push('<div class="tt-label">' + escapeHtml(a.label) + '</div>');
    }
    const chips = [];
    if (a.kind) chips.push(a.kind);
    chips.push(a.status.replace("_", " "));
    if (a.match_kind) chips.push(a.match_kind);
    parts.push(
      '<div class="tt-status ' + escapeHtml(a.status) + '">' + chips.map(escapeHtml).join(" · ") + '</div>'
    );
    if (a.decl) {
      parts.push('<div class="tt-row">Mathlib: <code>' + escapeHtml(a.decl) + "</code></div>");
    }
    if (a.mathlib_url) {
      parts.push(
        '<div class="tt-row"><a href="' +
          escapeHtml(a.mathlib_url) +
          '" target="_blank" rel="noopener">view in Mathlib docs ↗</a></div>'
      );
    }
    if (a.note) {
      parts.push('<div class="tt-row">' + escapeHtml(a.note) + "</div>");
    }
    if (a.proof_note) {
      parts.push(
        '<div class="tt-row tt-proof">⚠ Mathlib proof differs: ' +
          escapeHtml(a.proof_note) +
          '</div>'
      );
    }
    // Provenance — signal whether this annotation is AI-generated or a person
    // has actually reviewed it. Drives reader trust and invites contribution.
    const prov = a.provenance === "human" ? "human" : "ai";
    const provLabel = prov === "human"
      ? "✓ Human-curated"
      : "⚙ AI-generated · not yet reviewed";
    parts.push('<div class="tt-prov tt-prov-' + prov + '">' + provLabel + '</div>');
    // Footer affordance: anyone (no login) can report a problem with this
    // annotation. Tapping it swaps the tooltip content for the flag form.
    parts.push(
      '<div class="tt-flag-row"><a href="#" class="tt-flag-link" data-i="' + i + '">⚑ Report a problem</a></div>'
    );
    return parts.join("");
  }

  function setTooltipContent(annos) {
    // annos is an array — possibly more than one when several defs/props
    // share a block (e.g. all stated in one paragraph).
    currentAnnos = annos;
    flagFormOpen = false;
    tooltip.innerHTML = annos
      .map(renderOneAnno)
      .join('<div class="tt-divider"></div>');
  }

  // ---- "Report a problem" micro-form (anonymous flags) ----
  // POSTs to /api/flag/:slug — no auth required; the server rate-limits by IP
  // and caps open flags per target. annotation_id is included only when the
  // client payload carries an id (older cached pages may not ship ids yet).

  const FLAG_REASONS = [
    ["wrong_decl", "wrong Mathlib decl"],
    ["wrong_status", "wrong status"],
    ["irrelevant", "not relevant"],
    ["other", "other"],
  ];

  // The article slug is the (already URL-encoded) path: pages serve at /:slug.
  const flagSlug = location.pathname.replace(/^\//, "");

  function openFlagForm(i) {
    const a = currentAnnos[i];
    if (!a) return;
    flagFormOpen = true;
    tooltip.innerHTML =
      '<div class="tt-flag-form" data-i="' + i + '">' +
      '<div class="tt-label">Report a problem</div>' +
      '<div class="tt-chips">' +
      FLAG_REASONS.map(function (r) {
        return '<button type="button" class="tt-chip" data-reason="' + r[0] + '">' + escapeHtml(r[1]) + "</button>";
      }).join("") +
      "</div>" +
      '<input type="text" class="tt-flag-comment" maxlength="500" placeholder="optional details">' +
      '<div class="tt-flag-actions">' +
      '<button type="button" class="tt-flag-submit" disabled>Submit</button>' +
      '<button type="button" class="tt-flag-cancel">Cancel</button>' +
      "</div>" +
      '<div class="tt-flag-msg"></div>' +
      "</div>";
    if (activeEl) positionTooltip(activeEl);
  }

  function closeFlagForm() {
    flagFormOpen = false;
    if (currentAnnos.length) {
      setTooltipContent(currentAnnos);
      if (activeEl) positionTooltip(activeEl);
    } else {
      hideTooltip();
    }
  }

  function submitFlag(form) {
    const chip = form.querySelector(".tt-chip.selected");
    if (!chip) return;
    const a = currentAnnos[parseInt(form.dataset.i, 10)];
    const payload = { reason: chip.dataset.reason };
    const comment = form.querySelector(".tt-flag-comment").value.trim();
    if (comment) payload.comment = comment;
    // ids are 12-hex server-minted; only send one if this payload has it.
    if (a && typeof a.id === "string" && a.id) payload.annotation_id = a.id;
    const btn = form.querySelector(".tt-flag-submit");
    btn.disabled = true;
    btn.textContent = "sending…";
    fetch("/api/flag/" + flagSlug, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        if (!r.ok) throw new Error("status " + r.status);
        return r.json();
      })
      .then(function () {
        flagFormOpen = false;
        tooltip.innerHTML = '<div class="tt-flag-thanks">✓ thanks — a moderator will review</div>';
        if (activeEl) positionTooltip(activeEl);
      })
      .catch(function () {
        btn.disabled = false;
        btn.textContent = "Submit";
        form.querySelector(".tt-flag-msg").textContent = "could not send — please try again in a minute";
      });
  }

  // One delegated listener handles the flag link, reason chips, cancel, and
  // submit — the tooltip's content is replaced wholesale on each state change.
  tooltip.addEventListener("click", function (e) {
    const link = e.target.closest(".tt-flag-link");
    if (link) {
      e.preventDefault();
      e.stopPropagation();
      openFlagForm(parseInt(link.dataset.i, 10));
      return;
    }
    const chip = e.target.closest(".tt-chip");
    if (chip) {
      const form = chip.closest(".tt-flag-form");
      form.querySelectorAll(".tt-chip").forEach(function (c) {
        c.classList.toggle("selected", c === chip);
      });
      form.querySelector(".tt-flag-submit").disabled = false;
      return;
    }
    if (e.target.closest(".tt-flag-cancel")) {
      closeFlagForm();
      return;
    }
    const submit = e.target.closest(".tt-flag-submit");
    if (submit && !submit.disabled) {
      submitFlag(submit.closest(".tt-flag-form"));
    }
  });

  function positionTooltip(annoEl) {
    // Render off-screen first so we can measure dimensions without flashing at
    // (0,0) before the real placement is computed.
    tooltip.hidden = false;
    tooltip.style.visibility = "hidden";
    tooltip.style.left = "-9999px";
    tooltip.style.top = "0px";

    const r = annoEl.getBoundingClientRect();
    const tw = tooltip.offsetWidth;
    const th = tooltip.offsetHeight;
    const m = 8;
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    // Place the tooltip in the empty MARGIN beside the article body — not just
    // to the right of the annotation text, which would land inside the article
    // column on top of Wikipedia infoboxes / floated thumbnails.
    const articleBody = document.querySelector(".wl-article-body");
    const articleRect = articleBody ? articleBody.getBoundingClientRect() : null;

    let left, top, placement;
    if (articleRect && articleRect.right + m + tw <= vw - m) {
      left = articleRect.right + m;
      top = r.top;
      placement = "right";
    } else if (articleRect && articleRect.left - tw - m >= m) {
      left = articleRect.left - tw - m;
      top = r.top;
      placement = "left";
    } else {
      // Viewport too narrow for either side margin: place below the annotation,
      // or above if there isn't room below.
      left = Math.max(m, Math.min(r.left, vw - tw - m));
      top = r.bottom + m;
      placement = "below";
      if (top + th > vh - m) {
        top = Math.max(m, r.top - th - m);
        placement = "above";
      }
    }

    // Clamp to viewport.
    top = Math.max(m, Math.min(top, vh - th - m));
    if (left + tw > vw - m) left = vw - tw - m;
    if (left < m) left = m;

    tooltip.style.left = left + "px";
    tooltip.style.top = top + "px";
    tooltip.dataset.placement = placement;
    tooltip.style.visibility = "visible";
  }

  let activeEl = null;

  function showTooltip(annoEl, annos) {
    // A half-typed report shouldn't be lost because the cursor grazed another
    // annotation (or re-entered this one); Escape / Cancel / tap-outside
    // still dismiss the form.
    if (flagFormOpen) {
      clearTimeout(hideTimer);
      return;
    }
    clearTimeout(hideTimer);
    setTooltipContent(annos);
    positionTooltip(annoEl);
    activeEl = annoEl;
  }

  function hideTooltip() {
    clearTimeout(hideTimer);
    tooltip.hidden = true;
    activeEl = null;
    flagFormOpen = false;
  }

  function scheduleHide() {
    // Don't dismiss while someone is filling in the report form (mouseleave
    // fires constantly while typing); explicit close paths still work.
    if (flagFormOpen) return;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(hideTooltip, HIDE_DELAY_MS);
  }

  function cancelHide() {
    clearTimeout(hideTimer);
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // Touch / no-hover devices can't trigger mouseenter, so a tap must reveal the
  // tooltip (and not just follow the wrapped Wikipedia link). On hover-capable
  // devices we leave clicks alone so inner links still navigate normally.
  const noHover = window.matchMedia("(hover: none), (pointer: coarse)").matches;

  document.querySelectorAll(".anno").forEach((el) => {
    // data-anno-indices is comma-separated (1 or more indices when several
    // annotations share the same anchor range).
    const raw = el.dataset.annoIndices || el.dataset.annoIndex || "";
    const idxs = raw
      .split(",")
      .map((s) => parseInt(s, 10))
      .filter((n) => !isNaN(n));
    const annos = idxs.map((i) => annotations[i]).filter(Boolean);
    if (!annos.length) return;

    // Make each annotation reachable and announced for keyboard / screen-reader
    // users. aria-label includes the term text so the accessible name is not
    // lost when we override it with the status summary.
    el.setAttribute("tabindex", "0");
    el.setAttribute("role", "button");
    const a0 = annos[0];
    let aria = (el.textContent || "").trim() + ": " + a0.status.replace("_", " ");
    if (a0.decl) aria += ", Mathlib " + a0.decl;
    if (annos.length > 1) aria += " (+" + (annos.length - 1) + " more)";
    el.setAttribute("aria-label", aria);

    el.addEventListener("mouseenter", () => showTooltip(el, annos));
    el.addEventListener("mouseleave", scheduleHide);
    el.addEventListener("focus", () => showTooltip(el, annos));
    el.addEventListener("blur", scheduleHide);
    el.addEventListener("click", (e) => {
      if (!noHover) return; // hover devices: let the wrapped link navigate
      e.preventDefault();
      e.stopPropagation();
      if (activeEl === el && !tooltip.hidden) hideTooltip();
      else showTooltip(el, annos);
    });
  });

  // Keep the tooltip visible while the cursor is over it (so the link is clickable).
  tooltip.addEventListener("mouseenter", cancelHide);
  tooltip.addEventListener("mouseleave", scheduleHide);

  // Escape closes; tapping outside (on touch) dismisses an open tooltip.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideTooltip();
  });
  document.addEventListener("click", (e) => {
    if (!noHover || !activeEl) return;
    if (!activeEl.contains(e.target) && !tooltip.contains(e.target)) hideTooltip();
  });
})();
