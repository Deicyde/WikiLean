// Articles-plane pages (docs/ARTICLES.md, "UI"): a small enhancement over
// server-rendered HTML that already works without it. It wires the theme
// toggle, the status legend and the library chips that filter an article's
// passages, the per-location Confirm / Report controls, and the article list's
// live filter and sortable columns.
//
// XSS: every string from the page or from the API is written with
// textContent or setAttribute; nothing is parsed as HTML.

(function () {
  "use strict";

  // ---- theme toggle (the plane's shell carries no inline handlers) ----
  var toggle = document.getElementById("wl-theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem("wl-theme", next); } catch (e) { /* private mode */ }
    });
  }

  // ---- a formula wider than its passage (a phone) scrolls sideways inside its own box;
  // such a box takes keyboard focus, so a keyboard user can scroll it too ----
  function focusWide() {
    var boxes = document.querySelectorAll(".articles-math-display, .articles-math .m-env");
    for (var i = 0; i < boxes.length; i++) {
      var box = boxes[i];
      if (box.scrollWidth > box.clientWidth) box.setAttribute("tabindex", "0");
      else box.removeAttribute("tabindex");
    }
  }
  focusWide();
  var wideTimer = null;
  function focusWideSoon() {
    clearTimeout(wideTimer);
    wideTimer = setTimeout(focusWide, 150);
  }
  window.addEventListener("load", focusWide);
  window.addEventListener("resize", focusWideSoon);
  // A phone may lay the page out again at its own width after this script ran.
  if (typeof ResizeObserver === "function" && document.body) new ResizeObserver(focusWideSoon).observe(document.body);

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // ---- article coverage page ----
  var article = document.getElementById("articles-article");
  if (article) {
    var passages = Array.prototype.slice.call(article.querySelectorAll(".articles-passage"));
    var chips = Array.prototype.slice.call(article.querySelectorAll(".articles-chip[data-filter]"));
    var note = article.querySelector(".articles-filter-text");
    var clear = article.querySelector(".articles-clear");
    // What this page calls its passages: "statements" on a page nothing has checked
    // (articles-render.ts filterNote writes data-noun only there).
    var noteBox = article.querySelector(".articles-filter-note");
    var noun = (noteBox && noteBox.getAttribute("data-noun")) || "passages";

    // Two independent facets: at most one status and at most one library,
    // combined with AND ("Statement only, in Mathlib"). A second chip in the
    // same row replaces the first; pressing a pressed chip clears its facet.
    var active = { status: null, library: null };
    var labels = { status: "", library: "" };
    var chipWords = function (chip) {
      // The chip's own words, without its icon, swatch and count spans.
      return Array.prototype.filter.call(chip.childNodes, function (n) { return n.nodeType === 3; })
        .map(function (n) { return n.textContent; }).join("").trim();
    };
    var applyFilter = function () {
      var shown = 0;
      passages.forEach(function (p) {
        var ok = (!active.status || p.getAttribute("data-status") === active.status) &&
          (!active.library || (" " + (p.getAttribute("data-libraries") || "") + " ").indexOf(" " + active.library + " ") !== -1);
        p.hidden = !ok;
        if (ok) shown += 1;
      });
      chips.forEach(function (c) {
        var f = c.getAttribute("data-filter"), cut = f.indexOf(":");
        c.setAttribute("aria-pressed", active[f.slice(0, cut)] === f.slice(cut + 1) ? "true" : "false");
      });
      var any = active.status || active.library;
      var words = [];
      if (active.status) words.push(labels.status);
      if (active.library) words.push("in " + labels.library);
      if (note) note.textContent = any ? "Showing " + shown + " of " + passages.length + " " + noun + ": " + words.join(", ") + "." : "";
      if (clear) clear.hidden = !any;
    };
    chips.forEach(function (chip) {
      chip.addEventListener("click", function () {
        var filter = chip.getAttribute("data-filter"), cut = filter.indexOf(":");
        var kind = filter.slice(0, cut), value = filter.slice(cut + 1);
        if (kind !== "status" && kind !== "library") return;
        if (active[kind] === value) {
          active[kind] = null;
        } else {
          active[kind] = value;
          labels[kind] = chipWords(chip);
        }
        applyFilter();
      });
    });
    if (clear) clear.addEventListener("click", function () {
      active.status = null;
      active.library = null;
      applyFilter();
    });

    // ---- Confirm / Report ----
    var pageid = Number(article.getAttribute("data-pageid"));
    var campaign = article.getAttribute("data-campaign");
    var MESSAGES = {
      401: "Sign in again to send feedback.",
      403: "This browser session cannot send feedback.",
      409: "These links changed since this page loaded; reload to see the current links.",
      413: "That note is too long.",
      429: "Too many verdicts in a minute; try again shortly.",
      503: "Feedback is not available on this deployment yet."
    };

    var send = function (box, verdict, noteText, done) {
      var buttons = box.querySelectorAll(".articles-fb-btn");
      var msg = box.querySelector(".articles-fb-msg");
      Array.prototype.forEach.call(buttons, function (b) { b.disabled = true; });
      if (msg) msg.textContent = "Saving\u2026";
      var body = { location_key: box.getAttribute("data-location-key"), pageid: pageid, campaign: campaign, verdict: verdict };
      if (noteText) body.note = noteText;
      fetch("/api/articles/feedback", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      }).then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) { return { res: res, data: data }; });
      }).then(function (r) {
        Array.prototype.forEach.call(buttons, function (b) { b.disabled = false; });
        if (r.res.ok && r.data && r.data.ok) {
          Array.prototype.forEach.call(buttons, function (b) {
            b.setAttribute("aria-pressed", b.getAttribute("data-verdict") === verdict ? "true" : "false");
          });
          if (msg) msg.textContent = verdict === "confirm" ? "Thanks: you confirmed this link." : "Thanks: your report was recorded.";
          if (done) done();
        } else if (msg) {
          msg.textContent = MESSAGES[r.res.status] || ("Could not save: " + ((r.data && r.data.error) || r.res.status));
        }
      }).catch(function () {
        Array.prototype.forEach.call(buttons, function (b) { b.disabled = false; });
        if (msg) msg.textContent = "Could not reach WikiLean; try again.";
      });
    };

    var openReport = function (box, reportButton) {
      var existing = box.parentNode.querySelector(".articles-fb-form");
      if (existing) { existing.remove(); reportButton.setAttribute("aria-expanded", "false"); return; }
      var form = el("form", "articles-fb-form");
      var id = "articles-note-" + box.getAttribute("data-location-key").slice(0, 12);
      var label = el("label", null, "What is wrong with this link? (optional, at most 500 characters)");
      label.setAttribute("for", id);
      var area = el("textarea");
      area.id = id;
      area.maxLength = 500;
      var actions = el("div", "articles-fb-actions");
      var submit = el("button", "articles-fb-btn", "Send report");
      submit.type = "submit";
      var cancel = el("button", "articles-fb-btn", "Cancel");
      cancel.type = "button";
      actions.appendChild(submit);
      actions.appendChild(cancel);
      form.appendChild(label);
      form.appendChild(area);
      form.appendChild(actions);
      var close = function () { form.remove(); reportButton.setAttribute("aria-expanded", "false"); reportButton.focus(); };
      cancel.addEventListener("click", close);
      form.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        send(box, "report", area.value.trim(), close);
      });
      box.parentNode.appendChild(form);
      reportButton.setAttribute("aria-expanded", "true");
      area.focus();
    };

    Array.prototype.forEach.call(article.querySelectorAll(".articles-fb"), function (box) {
      Array.prototype.forEach.call(box.querySelectorAll(".articles-fb-btn"), function (button) {
        if (button.getAttribute("data-verdict") === "report") button.setAttribute("aria-expanded", "false");
        button.addEventListener("click", function () {
          if (button.getAttribute("data-verdict") === "confirm") send(box, "confirm", null, null);
          else openReport(box, button);
        });
      });
    });

    // ---- community tags (docs/ARTICLES.md, "Community tags") ----
    // Add a Lean declaration to a passage, change one, or say a link is wrong.
    // Every write goes to /api/articles/tag; the server derives the repository,
    // the revision, the record set and the universe from the store, so the only
    // things sent are what the editor chose. On success the page reloads, so
    // the merged cards and the derived status come from the server, never from
    // a guess made here.
    var TAG_MESSAGES = {
      400: "That tag was not accepted; check the declaration name.",
      401: "Sign in again to edit.",
      403: "This account may not make that edit.",
      404: "This passage is no longer in the published build; reload the page.",
      409: "Somebody edited this first; reload the page to see their version.",
      413: "That note is too long.",
      422: "This article's record set is too old to take community tags.",
      429: "You have reached an editing limit; try again later.",
      503: "Editing is paused; try again later."
    };

    var cc0Box = document.getElementById("articles-cc0");
    var cc0Msg = document.getElementById("articles-cc0-msg");

    // The CC0 checkbox and, where one is configured, the Turnstile token. Both
    // are asked for once — at a person's first edit — and never again.
    var agreement = function () {
      if (!cc0Box) return {};
      var box = document.getElementById("articles-cc0-box");
      var out = {};
      if (box && box.checked) out.cc0 = true;
      var token = cc0Box.querySelector('[name="cf-turnstile-response"]');
      if (token && token.value) out.turnstile_token = token.value;
      return out;
    };
    var askAgreement = function (data, msgEl) {
      if (!cc0Box) {
        if (msgEl) msgEl.textContent = (data && data.error) || "An agreement is required before your first edit.";
        return;
      }
      cc0Box.hidden = false;
      if (cc0Msg) {
        cc0Msg.textContent = data && data.turnstile_required
          ? "Please complete the check above, then press Save again."
          : "Please agree to the licence above, then press Save again.";
      }
      cc0Box.scrollIntoView({ block: "center" });
      if (msgEl) msgEl.textContent = "Waiting for the agreement above.";
    };

    var postTag = function (url, body, msgEl, buttons) {
      Array.prototype.forEach.call(buttons || [], function (b) { b.disabled = true; });
      if (msgEl) msgEl.textContent = "Saving…";
      var payload = {};
      for (var k in body) if (Object.prototype.hasOwnProperty.call(body, k)) payload[k] = body[k];
      var extra = agreement();
      for (var j in extra) if (Object.prototype.hasOwnProperty.call(extra, j)) payload[j] = extra[j];
      return fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      }).then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) { return { res: res, data: data }; });
      }).then(function (r) {
        Array.prototype.forEach.call(buttons || [], function (b) { b.disabled = false; });
        if (r.res.ok && r.data && r.data.ok) {
          if (msgEl) msgEl.textContent = "Saved. Reloading…";
          window.location.reload();
          return;
        }
        if (r.data && (r.data.cc0_required || r.data.turnstile_required)) {
          askAgreement(r.data, msgEl);
          return;
        }
        if (msgEl) {
          msgEl.textContent = (r.data && r.data.error) || TAG_MESSAGES[r.res.status] || ("Could not save: " + r.res.status);
        }
      }).catch(function () {
        Array.prototype.forEach.call(buttons || [], function (b) { b.disabled = false; });
        if (msgEl) msgEl.textContent = "Could not reach WikiLean; try again.";
      });
    };

    // "Tag this passage": the server-rendered form, shown on demand.
    Array.prototype.forEach.call(article.querySelectorAll(".articles-tag"), function (wrap) {
      var open = wrap.querySelector(".articles-tag-open");
      var form = wrap.querySelector(".articles-tag-form");
      if (!open || !form) return;
      var msg = form.querySelector(".articles-tag-msg");
      var cancel = form.querySelector(".articles-tag-cancel");
      var show = function (on) {
        form.hidden = !on;
        open.setAttribute("aria-expanded", on ? "true" : "false");
        if (on) {
          var first = form.querySelector("select, input");
          if (first) first.focus();
        } else {
          open.focus();
        }
      };
      open.addEventListener("click", function () { show(form.hidden); });
      if (cancel) cancel.addEventListener("click", function () { show(false); });
      form.addEventListener("keydown", function (e) { if (e.key === "Escape") show(false); });
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var decl = form.querySelector('[name="declaration"]').value.trim();
        if (!decl) { if (msg) msg.textContent = "Name the Lean declaration first."; return; }
        postTag("/api/articles/tag", {
          pageid: pageid,
          campaign: campaign,
          passage_key: wrap.getAttribute("data-passage-key"),
          library: form.querySelector('[name="library"]').value,
          declaration: decl,
          relation: form.querySelector('[name="relation"]').value,
          note: form.querySelector('[name="note"]').value.trim() || undefined
        }, msg, form.querySelectorAll("button"));
      });
    });

    // Per-card actions: change this link, say it is wrong, or put it back. A
    // coverage bundle's own actions carry a bundle key instead and are wired
    // below, so this selector names the per-location ones exactly.
    Array.prototype.forEach.call(article.querySelectorAll(".articles-tag-actions[data-pair-key]"), function (box) {
      var msg = box.querySelector(".articles-tag-msg");
      var buttons = box.querySelectorAll("button");
      var key = box.getAttribute("data-pair-key");
      var version = Number(box.getAttribute("data-version")) || 0;
      var stateUrl = "/api/articles/tag/" + encodeURIComponent(key) + "/state";
      var closeForm = function () {
        var open = box.parentNode.querySelector(".articles-card-form");
        if (open) open.remove();
        Array.prototype.forEach.call(buttons, function (b) { b.setAttribute("aria-expanded", "false"); });
      };
      var openForm = function (action, button) {
        if (box.parentNode.querySelector(".articles-card-form")) { closeForm(); return; }
        var form = el("form", "articles-card-form");
        var label = el("label", null, action === "change"
          ? "How does this declaration match the passage?"
          : "Why is this link wrong? (required, at most 500 characters)");
        var field;
        if (action === "change") {
          field = document.createElement("select");
          [["exactMatch", "exact"], ["closeMatch", "close"], ["broadMatch", "more general"],
            ["narrowMatch", "special case"], ["hasPart", "part"], ["relatedMatch", "related"]].forEach(function (pair) {
            var option = el("option", null, pair[1]);
            option.value = pair[0];
            if (pair[0] === box.getAttribute("data-relation")) option.selected = true;
            field.appendChild(option);
          });
        } else {
          field = document.createElement("textarea");
          field.maxLength = 500;
          field.rows = 2;
        }
        field.id = "cardfield-" + key.slice(0, 12) + "-" + action;
        label.setAttribute("for", field.id);
        var submit = el("button", "articles-tag-btn is-primary", action === "change" ? "Save change" : "Remove this link");
        submit.type = "submit";
        var cancel = el("button", "articles-tag-btn", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", closeForm);
        var row = el("div", "articles-tag-buttons");
        row.appendChild(submit);
        row.appendChild(cancel);
        form.appendChild(label);
        form.appendChild(field);
        form.appendChild(row);
        form.addEventListener("keydown", function (e) { if (e.key === "Escape") closeForm(); });
        form.addEventListener("submit", function (e) {
          e.preventDefault();
          if (action === "change") {
            postTag("/api/articles/tag", {
              pageid: pageid,
              campaign: campaign,
              passage_key: box.getAttribute("data-passage-key"),
              library: box.getAttribute("data-library"),
              declaration: box.getAttribute("data-declaration"),
              relation: field.value,
              note: box.getAttribute("data-note") || undefined,
              base_version: version
            }, msg, form.querySelectorAll("button"));
          } else {
            var reason = field.value.trim();
            if (!reason) { if (msg) msg.textContent = "Say why the link is wrong."; return; }
            postTag(stateUrl, {
              pageid: pageid, campaign: campaign, state: "removed", reason: reason, base_version: version
            }, msg, form.querySelectorAll("button"));
          }
        });
        box.parentNode.appendChild(form);
        button.setAttribute("aria-expanded", "true");
        field.focus();
      };
      Array.prototype.forEach.call(buttons, function (button) {
        var action = button.getAttribute("data-action");
        button.addEventListener("click", function () {
          if (action === "restore") {
            postTag(stateUrl, { pageid: pageid, campaign: campaign, state: "live", base_version: version }, msg, buttons);
          } else if (action === "change" || action === "remove") {
            openForm(action, button);
          }
        });
      });
    });

    // ---- coverage bundles: "these together state the passage" ----
    // The server renders one row per link that states part of the passage; the
    // editor ticks the ones that together state all of it and says what each
    // states. The body carries the parts in the order they are ticked and one
    // conjunct per part, c1..cN, which is the shape the route checks.
    Array.prototype.forEach.call(article.querySelectorAll(".articles-bundle-new"), function (wrap) {
      var open = wrap.querySelector(".articles-tag-open");
      var form = wrap.querySelector(".articles-bundle-form");
      if (!open || !form) return;
      var msg = form.querySelector(".articles-tag-msg");
      var cancel = form.querySelector(".articles-tag-cancel");
      var show = function (on) {
        form.hidden = !on;
        open.setAttribute("aria-expanded", on ? "true" : "false");
        if (on) {
          var first = form.querySelector("input, select");
          if (first) first.focus();
        } else {
          open.focus();
        }
      };
      open.addEventListener("click", function () { show(form.hidden); });
      if (cancel) cancel.addEventListener("click", function () { show(false); });
      form.addEventListener("keydown", function (e) { if (e.key === "Escape") show(false); });
      form.addEventListener("submit", function (e) {
        e.preventDefault();
        var parts = [];
        var conjuncts = [];
        var incomplete = false;
        Array.prototype.forEach.call(form.querySelectorAll(".articles-bundle-parts > li"), function (row) {
          var box = row.querySelector('input[type="checkbox"]');
          var clause = row.querySelector('input[type="text"]');
          if (!box || !box.checked) return;
          var said = clause ? clause.value.trim() : "";
          if (!said) { incomplete = true; return; }
          parts.push(box.value);
          conjuncts.push({ id: "c" + (conjuncts.length + 1), text: said, part: box.value });
        });
        // A ticked link with no clause is the commoner mistake, and it also
        // makes the count look short, so it is the message that comes first.
        if (incomplete) {
          if (msg) msg.textContent = "Say what each ticked link states.";
          return;
        }
        if (parts.length < 2) {
          if (msg) msg.textContent = "Tick at least two links that together state this passage.";
          return;
        }
        if (parts.length > 5) {
          if (msg) msg.textContent = "A bundle names at most five parts.";
          return;
        }
        postTag("/api/articles/coverage", {
          pageid: pageid,
          campaign: campaign,
          passage_key: wrap.getAttribute("data-passage-key"),
          parts: parts,
          conjuncts: conjuncts,
          relation: form.querySelector('[name="relation"]').value
        }, msg, form.querySelectorAll("button"));
      });
    });

    // Withdrawing a bundle and putting it back, with the reason a withdrawal needs.
    Array.prototype.forEach.call(article.querySelectorAll(".articles-tag-actions[data-bundle-key]"), function (box) {
      var msg = box.querySelector(".articles-tag-msg");
      var buttons = box.querySelectorAll("button");
      var key = box.getAttribute("data-bundle-key");
      var version = Number(box.getAttribute("data-version")) || 0;
      var url = "/api/articles/coverage/" + encodeURIComponent(key) + "/state";
      var closeForm = function () {
        var open = box.parentNode.querySelector(".articles-card-form");
        if (open) open.remove();
        Array.prototype.forEach.call(buttons, function (b) { b.setAttribute("aria-expanded", "false"); });
      };
      var openForm = function (button) {
        if (box.parentNode.querySelector(".articles-card-form")) { closeForm(); return; }
        var form = el("form", "articles-card-form");
        var field = document.createElement("textarea");
        field.maxLength = 500;
        field.rows = 2;
        field.id = "bundlefield-" + key.slice(0, 12);
        var label = el("label", null, "Why do these parts not state the passage? (required, at most 500 characters)");
        label.setAttribute("for", field.id);
        var submit = el("button", "articles-tag-btn is-primary", "Withdraw this");
        submit.type = "submit";
        var cancel = el("button", "articles-tag-btn", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", closeForm);
        var row = el("div", "articles-tag-buttons");
        row.appendChild(submit);
        row.appendChild(cancel);
        form.appendChild(label);
        form.appendChild(field);
        form.appendChild(row);
        form.addEventListener("keydown", function (e) { if (e.key === "Escape") closeForm(); });
        form.addEventListener("submit", function (e) {
          e.preventDefault();
          var reason = field.value.trim();
          if (!reason) { if (msg) msg.textContent = "Say why these parts do not state it."; return; }
          postTag(url, {
            pageid: pageid, campaign: campaign, state: "removed", reason: reason, base_version: version
          }, msg, form.querySelectorAll("button"));
        });
        box.parentNode.appendChild(form);
        button.setAttribute("aria-expanded", "true");
        field.focus();
      };
      Array.prototype.forEach.call(buttons, function (button) {
        var action = button.getAttribute("data-action");
        button.addEventListener("click", function () {
          if (action === "bundle-restore") {
            postTag(url, { pageid: pageid, campaign: campaign, state: "live", base_version: version }, msg, buttons);
          } else if (action === "bundle-withdraw") {
            openForm(button);
          }
        });
      });
    });
  }

  // ---- dashboard: live article filter ----
  var dashboard = document.getElementById("articles-dashboard");
  var table = document.getElementById("articles-list");
  var input = document.getElementById("articles-q");
  if (dashboard && table && input) {
    var tbody = table.querySelector("tbody");
    var count = document.getElementById("articles-count");
    var total = Number(table.getAttribute("data-total")) || 0;
    var shards = (table.getAttribute("data-shards") || "").split(/\s+/).filter(Boolean);
    // The statuses a CHECK can give a passage: one with no status yet is in no column and no
    // count, and a statement no library has been searched for takes no share of the bar, as
    // the server-rendered rows have it (articles-render.ts CHECKED_STATUSES).
    var STATUSES = ["formalized", "statement_only", "partial", "not_formalized"];
    var libraryNames = {};
    try { libraryNames = JSON.parse(table.getAttribute("data-library-names") || "{}") || {}; } catch (e) { libraryNames = {}; }
    var LIMIT = 200;
    var all = null;
    var loading = false;

    var fold = function (s) {
      s = String(s);
      try { s = s.normalize("NFKD").replace(/[\u0300-\u036f]/g, ""); } catch (e) { /* old engines */ }
      return s.toLowerCase();
    };
    var titleKey = function (t) {
      return String(t).normalize("NFC").replace(/[ \t\n\r\f\v\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000_]+/g, " ").replace(/^ +| +$/g, "");
    };
    // The status words the bar's accessible name uses, as articles-render.ts writes them.
    var STATUS_WORDS = {
      formalized: "formalized", statement_only: "statement only",
      partial: "partial", not_formalized: "not formalized"
    };
    // One row's coverage bar, the same element the server renders: a segment per status,
    // each its share of the width, and the whole bar one image with the counts as its name.
    var barFor = function (entry) {
      var total = 0;
      STATUSES.forEach(function (s) { total += Number(entry[s]) || 0; });
      var notChecked = Number(entry.not_checked) || 0;
      if (!total) {
        // Nothing checked: the empty track, named for what is waiting (articles-render.ts
        // emptyStatusBar), not a bar that failed to draw.
        if (!notChecked) return el("span", "articles-muted", "no status yet");
        var empty = el("div", "articles-bar is-empty");
        empty.setAttribute("role", "img");
        empty.setAttribute("aria-label", notChecked + (notChecked === 1 ? " statement" : " statements") + ": none checked yet");
        return empty;
      }
      var bar = el("div", "articles-bar");
      var words = [];
      // The bar is over the CHECKED passages; its name says so where some are not.
      var noun = notChecked ? " checked passage" : " passage";
      STATUSES.forEach(function (s) {
        var n = Number(entry[s]) || 0;
        if (!n) return;
        var seg = el("span", "articles-seg");
        seg.setAttribute("data-status", s);
        seg.style.width = ((n / total) * 100).toFixed(2) + "%";
        seg.setAttribute("title", n + " " + STATUS_WORDS[s]);
        bar.appendChild(seg);
        words.push(n + " " + STATUS_WORDS[s]);
      });
      bar.setAttribute("role", "img");
      bar.setAttribute("aria-label", total + noun + (total === 1 ? ": " : "s: ") + words.join(", "));
      return bar;
    };
    var rowFor = function (entry) {
      var tr = el("tr");
      var shown = Math.max(0, (Number(entry.passages) || 0) - (Number(entry.unresolved) || 0));
      var notChecked = Number(entry.not_checked) || 0;
      var checked = Math.max(0, shown - notChecked);
      var ids = Array.isArray(entry.libraries) ? entry.libraries : [];
      tr.setAttribute("data-title", fold(entry.title));
      tr.setAttribute("data-passages", String(checked));
      tr.setAttribute("data-formalized", String(Number(entry.formalized) || 0));
      if (notChecked) tr.setAttribute("data-not-checked", String(notChecked));
      tr.setAttribute("data-libraries", String(ids.length));
      tr.setAttribute("data-importance", String(entry.importance || ""));
      var th = el("th");
      th.setAttribute("scope", "row");
      var a = el("a", null, entry.title);
      a.href = "/articles/" + entry.pageid + "/" + encodeURIComponent(titleKey(entry.title).replace(/ /g, "_"));
      th.appendChild(a);
      tr.appendChild(th);
      var cov = el("td", "articles-cov");
      cov.appendChild(barFor(entry));
      tr.appendChild(cov);
      tr.appendChild(el("td", "num", notChecked ? checked + " of " + shown + " checked" : checked));
      var libs = el("td", "num");
      if (!ids.length) {
        libs.appendChild(el("span", "articles-muted", "none"));
      } else {
        var names = ids.map(function (id) {
          return Object.prototype.hasOwnProperty.call(libraryNames, id) ? libraryNames[id] : id;
        });
        var cell = el("span", null, ids.length + (ids.length === 1 ? " library" : " libraries"));
        cell.setAttribute("title", names.join(", "));
        libs.appendChild(cell);
      }
      tr.appendChild(libs);
      return tr;
    };
    var loadAll = function (then) {
      if (all || loading) { if (all) then(); return; }
      loading = true;
      Promise.all(shards.map(function (s) {
        return fetch("/api/articles/index/" + encodeURIComponent(s), { credentials: "same-origin" })
          .then(function (r) { return r.ok ? r.json() : null; });
      })).then(function (parts) {
        all = [];
        parts.forEach(function (part) {
          if (!part || !Array.isArray(part.fields) || !Array.isArray(part.articles)) return;
          part.articles.forEach(function (row) {
            var entry = {};
            part.fields.forEach(function (name, i) { entry[name] = row[i]; });
            entry.key = fold(entry.title);
            all.push(entry);
          });
        });
        all.sort(function (a, b) {
          var ka = titleKey(a.title), kb = titleKey(b.title);
          return ka < kb ? -1 : ka > kb ? 1 : a.pageid - b.pageid;
        });
        loading = false;
        then();
      }).catch(function () { loading = false; });
    };
    // ---- sortable columns ----
    // Progressive enhancement: the server has already put the rows in title order and the
    // heads are plain words. With the script each head is a button, the row carries every
    // key as a data attribute, and the column in force says so with aria-sort.
    var heads = Array.prototype.slice.call(table.querySelectorAll("thead th[data-sort]"));
    var sortKey = "title";
    var sortDir = 1;
    var sortValue = function (tr, key) {
      if (key === "title") return tr.getAttribute("data-title") || "";
      if (key === "formalized") {
        var passages = Number(tr.getAttribute("data-passages")) || 0;
        // An article with nothing checked sorts below one that is 0% formalized.
        return passages ? (Number(tr.getAttribute("data-formalized")) || 0) / passages : -1;
      }
      return Number(tr.getAttribute("data-" + key)) || 0;
    };
    var applySort = function () {
      var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
      rows.sort(function (a, b) {
        var va = sortValue(a, sortKey), vb = sortValue(b, sortKey);
        if (va < vb) return -sortDir;
        if (va > vb) return sortDir;
        var ta = a.getAttribute("data-title") || "", tb = b.getAttribute("data-title") || "";
        return ta < tb ? -1 : ta > tb ? 1 : 0;
      });
      rows.forEach(function (tr) { tbody.appendChild(tr); });
      heads.forEach(function (th) {
        var on = th.getAttribute("data-sort") === sortKey;
        if (on) th.setAttribute("aria-sort", sortDir === 1 ? "ascending" : "descending");
        else th.removeAttribute("aria-sort");
        var mark = th.querySelector(".articles-sort-mark");
        if (mark) mark.textContent = sortDir === 1 ? "▲" : "▼";
      });
    };
    heads.forEach(function (th) {
      var key = th.getAttribute("data-sort");
      var button = el("button", "articles-sort", th.textContent);
      button.type = "button";
      button.setAttribute("title", th.getAttribute("data-sort-title") || "");
      var mark = el("span", "articles-sort-mark", "▲");
      mark.setAttribute("aria-hidden", "true");
      button.appendChild(mark);
      th.textContent = "";
      th.appendChild(button);
      button.addEventListener("click", function () {
        if (sortKey === key) sortDir = -sortDir;
        // A name reads best from A; a measure reads best from the most.
        else { sortKey = key; sortDir = key === "title" ? 1 : -1; }
        applySort();
      });
    });
    if (heads.length) applySort();

    var render = function () {
      var q = fold(input.value.trim());
      if (all) {
        var hits = q ? all.filter(function (e) { return e.key.indexOf(q) !== -1; }) : all;
        tbody.textContent = "";
        hits.slice(0, LIMIT).forEach(function (e) { tbody.appendChild(rowFor(e)); });
        applySort();
        if (count) count.textContent = (q ? hits.length + " of " + all.length + " articles match." : all.length + " articles.") +
          (hits.length > LIMIT ? " Showing the first " + LIMIT + "." : "");
        return;
      }
      var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
      var shown = 0;
      rows.forEach(function (tr) {
        var ok = !q || (tr.getAttribute("data-title") || "").indexOf(q) !== -1;
        tr.hidden = !ok;
        if (ok) shown += 1;
      });
      if (count) count.textContent = q ? shown + " of " + total + " articles match." : total + " articles.";
    };
    var timer = null;
    input.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        // The server rendered every article: filter in place. Otherwise
        // fetch the index shards once and filter all of them.
        if (total > tbody.querySelectorAll("tr").length && shards.length) loadAll(render);
        else render();
      }, 120);
    });
  }
})();
