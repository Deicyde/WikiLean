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

  function renderOneAnno(a) {
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
    return parts.join("");
  }

  function setTooltipContent(annos) {
    // annos is an array — possibly more than one when several defs/props
    // share a block (e.g. all stated in one paragraph).
    tooltip.innerHTML = annos
      .map(renderOneAnno)
      .join('<div class="tt-divider"></div>');
  }

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
    clearTimeout(hideTimer);
    setTooltipContent(annos);
    positionTooltip(annoEl);
    activeEl = annoEl;
  }

  function hideTooltip() {
    clearTimeout(hideTimer);
    tooltip.hidden = true;
    activeEl = null;
  }

  function scheduleHide() {
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
