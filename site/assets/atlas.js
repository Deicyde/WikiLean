// Formalization atlas pages (docs/ATLAS.md, "UI"): a small enhancement over
// server-rendered HTML that already works without it. It wires the theme
// toggle, the coverage-strip filter chips, the per-location Confirm / Report
// controls, and live filtering of the dashboard's article list.
//
// XSS: every string from the page or from the API is written with
// textContent or setAttribute; nothing is parsed as HTML.

(function () {
  "use strict";

  // ---- theme toggle (the atlas shell carries no inline handlers) ----
  var toggle = document.getElementById("wl-theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var root = document.documentElement;
      var next = root.dataset.theme === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try { localStorage.setItem("wl-theme", next); } catch (e) { /* private mode */ }
    });
  }

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  // ---- article coverage page ----
  var article = document.getElementById("atlas-article");
  if (article) {
    var passages = Array.prototype.slice.call(article.querySelectorAll(".atlas-passage"));
    var chips = Array.prototype.slice.call(article.querySelectorAll(".atlas-chip[data-filter]"));
    var note = article.querySelector(".atlas-filter-text");
    var clear = article.querySelector(".atlas-clear");

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
      if (note) note.textContent = any ? "Showing " + shown + " of " + passages.length + " passages: " + words.join(", ") + "." : "";
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
      409: "The atlas changed since this page loaded; reload to see the current links.",
      413: "That note is too long.",
      429: "Too many verdicts in a minute; try again shortly.",
      503: "Feedback is not available on this deployment yet."
    };

    var send = function (box, verdict, noteText, done) {
      var buttons = box.querySelectorAll(".atlas-fb-btn");
      var msg = box.querySelector(".atlas-fb-msg");
      Array.prototype.forEach.call(buttons, function (b) { b.disabled = true; });
      if (msg) msg.textContent = "Saving\u2026";
      var body = { location_key: box.getAttribute("data-location-key"), pageid: pageid, campaign: campaign, verdict: verdict };
      if (noteText) body.note = noteText;
      fetch("/api/atlas/feedback", {
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
      var existing = box.parentNode.querySelector(".atlas-fb-form");
      if (existing) { existing.remove(); reportButton.setAttribute("aria-expanded", "false"); return; }
      var form = el("form", "atlas-fb-form");
      var id = "atlas-note-" + box.getAttribute("data-location-key").slice(0, 12);
      var label = el("label", null, "What is wrong with this link? (optional, at most 500 characters)");
      label.setAttribute("for", id);
      var area = el("textarea");
      area.id = id;
      area.maxLength = 500;
      var actions = el("div", "atlas-fb-actions");
      var submit = el("button", "atlas-fb-btn", "Send report");
      submit.type = "submit";
      var cancel = el("button", "atlas-fb-btn", "Cancel");
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

    Array.prototype.forEach.call(article.querySelectorAll(".atlas-fb"), function (box) {
      Array.prototype.forEach.call(box.querySelectorAll(".atlas-fb-btn"), function (button) {
        if (button.getAttribute("data-verdict") === "report") button.setAttribute("aria-expanded", "false");
        button.addEventListener("click", function () {
          if (button.getAttribute("data-verdict") === "confirm") send(box, "confirm", null, null);
          else openReport(box, button);
        });
      });
    });
  }

  // ---- dashboard: live article filter ----
  var dashboard = document.getElementById("atlas-dashboard");
  var table = document.getElementById("atlas-list");
  var input = document.getElementById("atlas-q");
  if (dashboard && table && input) {
    var tbody = table.querySelector("tbody");
    var count = document.getElementById("atlas-count");
    var total = Number(table.getAttribute("data-total")) || 0;
    var shards = (table.getAttribute("data-shards") || "").split(/\s+/).filter(Boolean);
    var STATUSES = ["formalized", "statement_only", "partial", "not_formalized", "unresolved"];
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
    var importance = function (v) {
      if (!v || v === "unknown") return "Not assessed";
      if (v === "na") return "N/A";
      return v.charAt(0).toUpperCase() + v.slice(1);
    };
    var rowFor = function (entry) {
      var tr = el("tr");
      tr.setAttribute("data-title", fold(entry.title));
      var th = el("th");
      th.setAttribute("scope", "row");
      var a = el("a", null, entry.title);
      a.href = "/atlas/" + entry.pageid + "/" + encodeURIComponent(titleKey(entry.title).replace(/ /g, "_"));
      th.appendChild(a);
      tr.appendChild(th);
      var passagesCell = el("td", "num", entry.passages);
      tr.appendChild(passagesCell);
      STATUSES.forEach(function (s) {
        var td = el("td", "num", entry[s] || 0);
        td.setAttribute("data-status", s);
        tr.appendChild(td);
      });
      var libs = el("td");
      var ids = Array.isArray(entry.libraries) ? entry.libraries : [];
      if (!ids.length) libs.appendChild(el("span", "atlas-muted", "none"));
      ids.forEach(function (id, i) {
        if (i) libs.appendChild(document.createTextNode(" "));
        var name = Object.prototype.hasOwnProperty.call(libraryNames, id) ? libraryNames[id] : id;
        libs.appendChild(el("span", "atlas-libtag", name));
      });
      tr.appendChild(libs);
      tr.appendChild(el("td", null, importance(entry.importance)));
      return tr;
    };
    var loadAll = function (then) {
      if (all || loading) { if (all) then(); return; }
      loading = true;
      Promise.all(shards.map(function (s) {
        return fetch("/api/atlas/index/" + encodeURIComponent(s), { credentials: "same-origin" })
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
    var render = function () {
      var q = fold(input.value.trim());
      if (all) {
        var hits = q ? all.filter(function (e) { return e.key.indexOf(q) !== -1; }) : all;
        tbody.textContent = "";
        hits.slice(0, LIMIT).forEach(function (e) { tbody.appendChild(rowFor(e)); });
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
