// Articles-plane reader (docs/ARTICLES.md, "Reader"): the behaviour of an article page
// that shows the pinned revision itself. The server renders everything this needs:
//   - the revision, each passage's text in <mark class="atl" data-p="<hex>" data-status>,
//     the first mark of each passage with id="p-<hex>" (so #p-<hex> works without this);
//   - the panel: an index of the passages, and one hidden <section id="atlr-pp-<hex>"> per
//     passage holding exactly the card list's block (its Confirm / Report are articles.js's).
// This script selects a passage (click, tap, Enter or Space on a highlight, a row of the
// index, the stepper, the left and right arrow keys, a #p-<hex> link), shows its section in the panel, outlines
// its highlight with a bar in the margin, filters by status (the legend under the title),
// by library and by prominence (the fold beside it), hides and shows the panel itself and
// remembers which, peeks on hover, and on a phone turns the panel into a bottom sheet. It
// also draws the revision's MathML the way Wikipedia does where MathML Core leaves
// something out (fixMath).
//
// XSS: it reads data attributes and writes with textContent, setAttribute, style, classList
// and MathML elements it creates itself; nothing is parsed as HTML.

(function () {
  "use strict";

  // ---- the card list of an article with a reader: the switch keeps the passage ----
  var toArticle = document.getElementById("atlr-to-article");
  if (toArticle && !document.getElementById("articles-reader")) {
    var keepHash = function () {
      var m = /^#p-([0-9a-f]{8,16})$/.exec(location.hash);
      toArticle.setAttribute("href", toArticle.getAttribute("href").split("#")[0] + (m ? m[0] : ""));
    };
    keepHash();
    window.addEventListener("hashchange", keepHash);
    return;
  }

  var root = document.getElementById("articles-reader");
  var art = document.getElementById("atlr-article");
  var panel = document.getElementById("atlr-panel");
  var slot = document.getElementById("articles-article");
  if (!root || !art || !panel || !slot) return;
  var html = document.documentElement;
  html.classList.add("atlr-js");

  var index = document.getElementById("atlr-index");
  var nav = document.getElementById("atlr-nav");
  var pos = document.getElementById("atlr-pos");
  var prevBtn = document.getElementById("atlr-prev");
  var nextBtn = document.getElementById("atlr-next");
  var backBtn = document.getElementById("atlr-back");
  var closeBtn = document.getElementById("atlr-close");
  var fab = document.getElementById("atlr-fab");
  var peek = document.getElementById("atlr-peek");
  var mbar = document.getElementById("atlr-mbar");
  var missing = document.getElementById("atlr-missing");
  var toCards = document.getElementById("atlr-to-cards");
  var filterText = document.getElementById("atlr-filter-text");
  var clearBtn = document.getElementById("atlr-clear");
  // What this page calls its passages: "statements" on a page nothing has checked
  // (articles-render.ts filterNote writes data-noun only there).
  var noteBox = document.querySelector(".articles-filter-note");
  var noun = (noteBox && noteBox.getAttribute("data-noun")) || "passages";
  var header = document.querySelector(".wl-header");
  var self = root.getAttribute("data-self") || location.pathname;

  // The statuses a passage can be shown with. A passage with no status is set aside by the page and
  // never reaches the panel; a section without one of these is ignored, never labelled.
  // not_checked is phase 1's: the statement was cut and no library has been searched for it.
  var STATUS = {
    formalized: ["\u2713", "Formalized"],
    statement_only: ["\u25d0", "Statement only"],
    partial: ["\u25d4", "Partial"],
    not_formalized: ["\u2715", "Not formalized"],
    not_checked: ["\u25cb", "Not checked yet"]
  };
  var phone = window.matchMedia ? window.matchMedia("(max-width: 999px)") : { matches: false };
  var finePointer = window.matchMedia ? window.matchMedia("(hover: hover) and (pointer: fine)") : { matches: true };
  var calm = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : { matches: false };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function each(list, fn) { Array.prototype.forEach.call(list, fn); }

  // ---- the revision's MathML, drawn as Wikipedia draws it ----
  // Wikipedia's MathML uses parts of MathML that MathML Core, and so Chromium, leaves out:
  // mathvariant beyond "normal" (\mathbb{R} would read R), named spaces (\, would be no space),
  // the table attributes (aligned rows would not line up on =, a cases brace would sit beside
  // padded cells) and menclose; and Chromium gives the function application after \sin, \log
  // or \exp no space, so "sin x" would read "sinx". This pass rewrites the formulas in place, in
  // the page only, after the builder has proved the served HTML: a double-struck R becomes
  // U+211D and so on, each named space its length, each table cell its alignment and padding,
  // and the operator names get TeX's thin space. The TeX annotation is untouched.
  // The MathML namespace is read off the page (the script names no URL).
  var MATHML_NS = (art.querySelector("math") || {}).namespaceURI;
  var THIN = "0.1667em";
  // Mathematical Alphanumeric Symbols: [A, a, 0, Greek capital Alpha] per variant (0: none).
  var VARIANT = {
    "bold": [0x1D400, 0x1D41A, 0x1D7CE, 0x1D6A8], "italic": [0x1D434, 0x1D44E, 0, 0x1D6E2],
    "bold-italic": [0x1D468, 0x1D482, 0, 0x1D71C], "script": [0x1D49C, 0x1D4B6, 0, 0],
    "bold-script": [0x1D4D0, 0x1D4EA, 0, 0], "fraktur": [0x1D504, 0x1D51E, 0, 0],
    "double-struck": [0x1D538, 0x1D552, 0x1D7D8, 0], "bold-fraktur": [0x1D56C, 0x1D586, 0, 0],
    "sans-serif": [0x1D5A0, 0x1D5BA, 0x1D7E2, 0], "bold-sans-serif": [0x1D5D4, 0x1D5EE, 0x1D7EC, 0x1D756],
    "sans-serif-italic": [0x1D608, 0x1D622, 0, 0], "sans-serif-bold-italic": [0x1D63C, 0x1D656, 0, 0x1D790],
    "monospace": [0x1D670, 0x1D68A, 0x1D7F6, 0]
  };
  // The letters Unicode had already encoded (Letterlike Symbols), which the block leaves out.
  var VARIANT_HOLES = {
    "italic": { h: 0x210E },
    "script": { B: 0x212C, E: 0x2130, F: 0x2131, H: 0x210B, I: 0x2110, L: 0x2112, M: 0x2133, R: 0x211B, e: 0x212F, g: 0x210A, o: 0x2134 },
    "fraktur": { C: 0x212D, H: 0x210C, I: 0x2111, R: 0x211C, Z: 0x2128 },
    "double-struck": { C: 0x2102, H: 0x210D, N: 0x2115, P: 0x2119, Q: 0x211A, R: 0x211D, Z: 0x2124 }
  };
  var GREEK_TAIL = { 0x2207: 25, 0x2202: 51, 0x03F5: 52, 0x03D1: 53, 0x03F0: 54, 0x03D5: 55, 0x03F1: 56, 0x03D6: 57, 0x03F4: 17 };
  function greekIndex(c) {
    if (c >= 0x391 && c <= 0x3A9 && c !== 0x3A2) return c - 0x391;
    if (c >= 0x3B1 && c <= 0x3C9) return 26 + c - 0x3B1;
    return GREEK_TAIL[c] === undefined ? -1 : GREEK_TAIL[c];
  }
  function variantText(s, v) {
    var base = VARIANT[v];
    if (!base) return null;
    var holes = VARIANT_HOLES[v] || {};
    var out = "", changed = false;
    for (var i = 0; i < s.length; i++) {
      var ch = s.charAt(i), c = s.charCodeAt(i), code = 0, g;
      if (holes[ch]) code = holes[ch];
      else if (c >= 65 && c <= 90) code = base[0] + c - 65;
      else if (c >= 97 && c <= 122) code = base[1] + c - 97;
      else if (c >= 48 && c <= 57) code = base[2] ? base[2] + c - 48 : 0;
      else if (base[3] && (g = greekIndex(c)) >= 0) code = base[3] + g;
      if (code) { out += String.fromCodePoint(code); changed = true; } else out += ch;
    }
    return changed ? out : null;
  }
  var NAMED_SPACE = { veryverythinmathspace: 1, verythinmathspace: 2, thinmathspace: 3, mediummathspace: 4,
    thickmathspace: 5, verythickmathspace: 6, veryverythickmathspace: 7 };
  function mathLength(v) {
    v = (v || "").trim();
    var m = /^(negative)?([a-z]+mathspace)$/.exec(v);
    if (m && NAMED_SPACE[m[2]]) return (m[1] ? "-" : "") + (NAMED_SPACE[m[2]] / 18).toFixed(4) + "em";
    if (/^[-+]?(\d+\.?\d*|\.\d+)$/.test(v)) return Number(v) === 0 ? "0px" : null;
    return /^-?(\d+\.?\d*|\.\d+)(em|ex|px|pt|pc|in|cm|mm|%)$/.test(v) ? v : null;
  }
  function words(v) { return (v || "").trim().split(/\s+/).filter(Boolean); }
  function nth(list, i) { return list.length ? list[Math.min(i, list.length - 1)] : null; }
  function kids(node, name) {
    return Array.prototype.filter.call(node.children, function (k) { return k.localName === name; });
  }
  var OPENS = "([{|\u2016\u27e8\u2308\u230a\u27e6\u2329";
  var CLOSES = ")]}|\u2016\u27e9\u2309\u230b\u27e7\u232a";
  // The first (or last) token a MathML element starts (or ends) with.
  function edgeToken(node, first) {
    while (node && node.firstElementChild && !/^(mi|mn|mo|mtext|ms|mspace)$/.test(node.localName)) {
      node = first ? node.firstElementChild : node.lastElementChild;
    }
    return node;
  }
  function fixMath() {
    each(art.querySelectorAll("mi[mathvariant], mn[mathvariant]"), function (token) {
      var v = token.getAttribute("mathvariant");
      if (v === "normal" || token.firstElementChild) return;
      var shown = variantText(token.textContent, v);
      if (shown !== null) token.textContent = shown;
    });
    each(art.querySelectorAll("math [width], math [lspace], math [rspace], math [height], math [depth]"), function (node) {
      ["width", "lspace", "rspace", "height", "depth"].forEach(function (name) {
        var v = node.getAttribute(name);
        if (!v || !/mathspace/.test(v)) return;
        var length = mathLength(v);
        if (length === null) return;
        // MathML Core gives mspace no negative width: \! pulls its neighbours together by a margin.
        if (node.localName === "mspace" && name === "width" && length.charAt(0) === "-") {
          node.setAttribute("width", "0px");
          node.style.marginInlineEnd = length;
        } else {
          node.setAttribute(name, length);
        }
      });
    });
    each(art.querySelectorAll("mo"), function (mo) {
      if (mo.textContent.trim() !== "\u2061") return;
      // \sin x: a thin space between the name and its argument, none before a bracket.
      var next = mo.nextElementSibling, head = next && edgeToken(next, true);
      if (next && !(head && head.localName === "mo" && OPENS.indexOf(head.textContent.trim()) !== -1)) {
        mo.setAttribute("lspace", "0px");
        mo.setAttribute("rspace", THIN);
      }
      // x \sin y: a thin space before the name too, unless an operator or an opening bracket gives one.
      var name = mo.previousElementSibling, before = name && name.previousElementSibling;
      if (!before || before.localName === "mspace") return;
      var tail = edgeToken(before, false);
      if (tail && tail.localName === "mo" && CLOSES.indexOf(tail.textContent.trim()) === -1) return;
      var space = document.createElementNS(MATHML_NS, "mspace");
      space.setAttribute("width", THIN);
      name.parentNode.insertBefore(space, name);
    });
    // TeX's ordinary symbols, which MathML Core spaces as operators: a lone operator braced as an
    // ordinary atom ({/} in SU(2)/Z_2), a bare | (|\Psi\rangle), and a sign after an opening
    // bracket, a comma or another operator (\{+1,-1\}).
    each(art.querySelectorAll("mo"), function (mo) {
      var t = mo.textContent.trim(), parent = mo.parentNode;
      var ord = parent && parent.localName === "mrow" && parent.children.length === 1 && / MJX-TeXAtom-ORD /.test(" " + parent.getAttribute("class") + " ");
      var bar = (t === "|" || t === "\u2016") && mo.getAttribute("fence") === "false";
      var sign = false;
      if (/^[+\u2212\u00b1\u2213-]$/.test(t)) {
        var prev = mo.previousElementSibling, tail = prev && edgeToken(prev, false);
        var tt = tail && tail.textContent.trim();
        sign = !!(tail && tail.localName === "mo" && tt !== "\u2061" && CLOSES.indexOf(tt) === -1 && !/^[\u2032-\u2034!]$/.test(tt));
      }
      if (ord || bar || sign) {
        mo.setAttribute("lspace", "0px");
        mo.setAttribute("rspace", "0px");
      }
    });
    // f': TeX writes the prime as a superscript; the font's prime is already raised, so Chromium
    // (which does not apply the font's script variant) would raise it twice. It goes on the line.
    each(art.querySelectorAll("msup"), function (sup) {
      var base = sup.firstElementChild, script = base && base.nextElementSibling;
      if (!script || script.nextElementSibling) return;
      var prime = script.localName === "mrow" && script.children.length === 1 ? script.firstElementChild : script;
      if (prime.localName !== "mo" || !/^[\u2032-\u2034\u2057]+$/.test(prime.textContent.trim())) return;
      var row = document.createElementNS(MATHML_NS, "mrow");
      prime.setAttribute("lspace", "0px");
      prime.setAttribute("rspace", "0px");
      sup.parentNode.insertBefore(row, sup);
      row.appendChild(base);
      row.appendChild(prime);
      sup.parentNode.removeChild(sup);
    });
    each(art.querySelectorAll("mtable"), function (table) {
      var align = words(table.getAttribute("columnalign"));
      var cs = words(table.getAttribute("columnspacing")).map(mathLength).filter(Boolean);
      var rs = words(table.getAttribute("rowspacing")).map(mathLength).filter(Boolean);
      var rowLines = words(table.getAttribute("rowlines")), colLines = words(table.getAttribute("columnlines"));
      if (!cs.length) cs = ["0.8em"];
      if (!rs.length) rs = ["1ex"];
      var rows = kids(table, "mtr");
      rows.forEach(function (row, i) {
        var cells = kids(row, "mtd");
        cells.forEach(function (cell, j) {
          var s = cell.style, a = nth(align, j);
          if (a === "left" || a === "right" || a === "center") s.textAlign = a;
          s.paddingTop = i === 0 ? "0px" : "calc(" + nth(rs, i - 1) + " / 2)";
          s.paddingBottom = i === rows.length - 1 ? "0px" : "calc(" + nth(rs, i) + " / 2)";
          s.paddingLeft = j === 0 ? "0px" : "calc(" + nth(cs, j - 1) + " / 2)";
          s.paddingRight = j === cells.length - 1 ? "0px" : "calc(" + nth(cs, j) + " / 2)";
          if (i < rows.length - 1 && /^(solid|dashed)$/.test(nth(rowLines, i) || "")) s.borderBottom = "0.06em " + nth(rowLines, i) + " currentColor";
          if (j < cells.length - 1 && /^(solid|dashed)$/.test(nth(colLines, j) || "")) s.borderRight = "0.06em " + nth(colLines, j) + " currentColor";
        });
      });
    });
  }

  // ---- the passages, in reading order ----
  var info = {};
  var order = [];
  each(slot.querySelectorAll("section.atlr-pp[data-p]"), function (section) {
    var hex = section.getAttribute("data-p");
    var prominence = section.getAttribute("data-prominence") || "";
    var status = section.getAttribute("data-status") || "";
    if (!Object.prototype.hasOwnProperty.call(STATUS, status)) return;
    info[hex] = {
      hex: hex, el: section, marks: [], row: null,
      status: status,
      libraries: " " + (section.getAttribute("data-libraries") || "") + " ",
      central: prominence === "subject" || prominence === "central",
      lead: section.getAttribute("data-lead") || "",
      leadLib: section.getAttribute("data-lead-lib") || "",
      more: Number(section.getAttribute("data-more")) || 0,
      rel: section.getAttribute("data-rel") || "",
      searched: section.getAttribute("data-searched") || "",
      first: section.getAttribute("data-first") || ""
    };
  });
  each(art.querySelectorAll("mark.atl[data-p]"), function (mark) {
    var p = info[mark.getAttribute("data-p")];
    if (!p) return;
    if (!p.marks.length) order.push(p);
    p.marks.push(mark);
  });
  // A passage the revision leaves unmarked is in the panel only, after the others.
  each(slot.querySelectorAll("section.atlr-pp[data-p]"), function (section) {
    var p = info[section.getAttribute("data-p")];
    if (p && !p.marks.length) order.push(p);
  });
  order.forEach(function (p, i) { p.index = i; });
  each(index.querySelectorAll("a.atlr-row[data-p]"), function (row) {
    var p = info[row.getAttribute("data-p")];
    if (p) p.row = row;
  });

  // The highlight's own words, for its accessible name: the text of every mark of the
  // passage, in document order. A mark covers the WHOLE passage while data-first is the
  // store's quote, which the record format caps at twelve words, so a name built from the
  // quote would leave out text the button shows: WCAG 2.5.3 (Label in Name), which axe
  // reports as label-content-name-mismatch. The quote is the fallback for a passage the
  // revision leaves unmarked (no button, so nothing to contain).
  function marked(p) {
    var words = "";
    for (var i = 0; i < p.marks.length; i++) {
      var mark = p.marks[i];
      // innerText, not textContent: the stored revision keeps each formula's TeX
      // annotation, which MathML never draws (articles-reader.css), and a name must be
      // the words the highlight SHOWS. textContent would read the TeX source aloud.
      var shown = typeof mark.innerText === "string" ? mark.innerText : mark.textContent;
      words += shown || "";
    }
    return words.replace(/\s+/g, " ").trim();
  }

  function label(p) {
    var st = STATUS[p.status];
    var shown = marked(p) || p.first;
    var words = (shown ? shown + ": " : "") + st[1].toLowerCase();
    if (p.lead) words += ", " + (p.leadLib ? p.leadLib + " " : "") + p.lead + (p.more ? " (+" + p.more + ")" : "");
    return words;
  }
  // One keyboard stop per passage: its first highlight that is not inside a link (a button
  // inside a link would be two controls in one). A passage whose every run is link text is
  // reached through the index.
  order.forEach(function (p) {
    if (!p.marks.length) return;
    p.marks[0].classList.add("atl-first");
    p.marks[p.marks.length - 1].classList.add("atl-last");
    for (var i = 0; i < p.marks.length; i++) {
      var mark = p.marks[i];
      if (!mark.closest("a")) {
        mark.setAttribute("tabindex", "0");
        mark.setAttribute("role", "button");
        mark.setAttribute("aria-label", label(p));
        p.stop = mark;
        break;
      }
    }
  });

  try { fixMath(); } catch (e) { /* the formulas stay as MathML Core draws them */ }

  // ---- the revision's markup, for assistive technology ----
  // Wikipedia indents with a <dl> of <dd>s alone (no term is defined), and leaves many images
  // without alt text, so their file links have no name. Such a list is presented as plain
  // blocks, an image with no alt is decorative (its caption, when it has one, says what it
  // shows), and its link is named after the file it opens.
  function fixAccess() {
    each(art.querySelectorAll("dl"), function (dl) {
      if (!kids(dl, "dt").length) dl.setAttribute("role", "presentation");
    });
    each(art.querySelectorAll("img:not([alt])"), function (img) { img.setAttribute("alt", ""); });
    each(art.querySelectorAll("a[href]"), function (a) {
      if (a.textContent.trim() || a.hasAttribute("aria-label")) return;
      if (a.querySelector('img:not([alt=""]), math')) return;
      var file = /\/wiki\/(File:[^?#]+)/.exec(a.getAttribute("href") || "");
      var name = file ? decodeURIComponent(file[1]).replace(/_/g, " ") : "";
      a.setAttribute("aria-label", name ? name + " (on Wikipedia)" : "Image (on Wikipedia)");
    });
  }
  try { fixAccess(); } catch (e) { /* the revision stays as served */ }

  // ---- geometry ----
  var headerH = 0;
  function measure() {
    headerH = header ? header.getBoundingClientRect().height : 0;
    // The panel sticks under the header (the body's --atlr-top), and every in-page jump,
    // Wikipedia's own [1] and back and section links among them, stops below it.
    document.body.style.setProperty("--atlr-top", Math.round(headerH) + "px");
    html.style.scrollPaddingTop = Math.round(headerH + 12) + "px";
  }
  measure();

  function sheetHeight() {
    return phone.matches && html.classList.contains("atlr-sheet-open") ? panel.getBoundingClientRect().height : 0;
  }

  // The passage's first line a third of the way down the free part of the screen.
  function passageTop(p) {
    var r = p.marks[0].getBoundingClientRect();
    var free = window.innerHeight - headerH - sheetHeight();
    var y = window.pageYOffset + r.top - headerH - Math.max(24, free / 3);
    return Math.max(0, Math.min(y, document.documentElement.scrollHeight - window.innerHeight));
  }
  // A smooth jump aims at where the passage is when it starts. If the page moves under it
  // (an image that does not load gives way to its description, a lazy one arrives), it is
  // placed again where the scroll ends, unless the reader has scrolled meanwhile.
  var settling = null;
  function settle() {
    var p = settling;
    settling = null;
    if (!p || p !== current || !p.marks.length) return;
    var y = passageTop(p);
    if (Math.abs(y - window.pageYOffset) > 4) window.scrollTo({ top: y, behavior: "auto" });
    placeBar();
  }
  function scrollToPassage(p, smooth) {
    if (!p.marks.length) return;
    var glide = smooth && !calm.matches;
    window.scrollTo({ top: passageTop(p), behavior: glide ? "smooth" : "auto" });
    settling = glide ? p : null;
    clearTimeout(settleTimer);
    if (glide) settleTimer = setTimeout(settle, "onscrollend" in window ? 2000 : 900);
  }
  var settleTimer = null;
  window.addEventListener("scrollend", function () { if (settling) { clearTimeout(settleTimer); settle(); } });
  ["wheel", "touchstart", "keydown", "mousedown"].forEach(function (type) {
    window.addEventListener(type, function () { settling = null; }, { capture: true, passive: true });
  });

  function placeBar() {
    if (!current || !current.marks.length || current.marks[0].classList.contains("is-off")) {
      mbar.hidden = true;
      return;
    }
    var box = art.getBoundingClientRect();
    var top = Infinity, bottom = -Infinity;
    current.marks.forEach(function (mark) {
      var r = mark.getBoundingClientRect();
      if (!r.width && !r.height) return;
      top = Math.min(top, r.top);
      bottom = Math.max(bottom, r.bottom);
    });
    if (top === Infinity) { mbar.hidden = true; return; }
    mbar.style.top = Math.round(top - box.top) + "px";
    mbar.style.height = Math.max(4, Math.round(bottom - top)) + "px";
    mbar.hidden = false;
  }

  // ---- filters (one status, one library, central only; and "dim other text") ----
  // Kept per article for the tab's session: another article opens unfiltered.
  var filterKey = "atlr-filter:" + (root.getAttribute("data-pageid") || location.pathname);
  var active = { status: null, library: null, central: false, dim: false };
  // The legend under the title is the status filter; the rest (library, the article's own
  // statements, dimming) is in the "More ways to filter" fold beside it.
  var chips = Array.prototype.slice.call(
    document.querySelectorAll(".articles-key .articles-chip[data-filter], .articles-filters .articles-chip[data-filter]"));
  var chipWords = {};
  chips.forEach(function (chip) {
    chipWords[chip.getAttribute("data-filter")] = Array.prototype.filter.call(chip.childNodes, function (n) {
      return n.nodeType === 3;
    }).map(function (n) { return n.textContent; }).join("").trim();
  });
  function matches(p) {
    return (!active.status || p.status === active.status) &&
      (!active.library || p.libraries.indexOf(" " + active.library + " ") !== -1) &&
      (!active.central || p.central);
  }
  function visible() { return order.filter(matches); }
  function applyFilter(save) {
    var shown = 0;
    order.forEach(function (p) {
      var ok = matches(p);
      if (ok) shown += 1;
      p.marks.forEach(function (mark) { mark.classList.toggle("is-off", !ok); });
      if (p.stop) p.stop.setAttribute("tabindex", ok ? "0" : "-1");
      if (p.row) p.row.parentNode.hidden = !ok;
    });
    each(index.querySelectorAll(".atlr-group"), function (group) {
      group.hidden = !group.querySelector(".atlr-rows > li:not([hidden])");
    });
    chips.forEach(function (chip) {
      var f = chip.getAttribute("data-filter"), cut = f.indexOf(":");
      var on = f === "central" ? active.central : f === "dim" ? active.dim
        : cut > 0 && active[f.slice(0, cut)] === f.slice(cut + 1);
      chip.setAttribute("aria-pressed", on ? "true" : "false");
    });
    document.body.classList.toggle("atlr-dim", active.dim);
    var any = active.status || active.library || active.central;
    var words = [];
    if (active.status) words.push(chipWords["status:" + active.status]);
    if (active.library) words.push("in " + chipWords["library:" + active.library]);
    if (active.central) words.push("central only");
    filterText.textContent = any ? "Showing " + shown + " of " + order.length + " " + noun + ": " + words.join(", ") + "." : "";
    clearBtn.hidden = !any;
    if (current) updateStepper();
    placeBar();
    if (save) {
      try { sessionStorage.setItem(filterKey, JSON.stringify(active)); } catch (e) { /* private mode */ }
    }
  }
  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      var f = chip.getAttribute("data-filter"), cut = f.indexOf(":");
      if (f === "central") active.central = !active.central;
      else if (f === "dim") active.dim = !active.dim;
      else {
        var kind = f.slice(0, cut), value = f.slice(cut + 1);
        if (kind !== "status" && kind !== "library") return;
        active[kind] = active[kind] === value ? null : value;
      }
      applyFilter(true);
    });
  });
  clearBtn.addEventListener("click", function () {
    active.status = null;
    active.library = null;
    active.central = false;
    applyFilter(true);
  });
  try {
    var saved = JSON.parse(sessionStorage.getItem(filterKey) || "null");
    if (saved && typeof saved === "object") {
      if (typeof saved.status === "string" && STATUS[saved.status]) active.status = saved.status;
      if (typeof saved.library === "string" && chipWords["library:" + saved.library] !== undefined) active.library = saved.library;
      active.central = saved.central === true;
      active.dim = saved.dim === true;
    }
  } catch (e) { /* private mode, or nothing saved */ }

  // ---- the passage list beside the article: hidden or shown, remembered ----
  // A layout preference, not an article's: one key for the reader, kept in localStorage
  // (wrapped, so private mode and a blocked store simply mean "shown"). On a phone the
  // panel is the bottom sheet and the button is not shown at all.
  var panelToggle = document.getElementById("atlr-panel-toggle");
  var PANEL_KEY = "wl-atlr-panel";
  function setPanel(open, save) {
    document.body.classList.toggle("atlr-no-panel", !open);
    if (panelToggle) {
      panelToggle.setAttribute("aria-expanded", open ? "true" : "false");
      panelToggle.textContent = open ? "Hide the passage list" : "Show the passage list";
    }
    if (save) {
      try { localStorage.setItem(PANEL_KEY, open ? "open" : "closed"); } catch (e) { /* private mode */ }
    }
    measure();
    placeBar();
  }
  if (panelToggle) {
    panelToggle.addEventListener("click", function () {
      setPanel(document.body.classList.contains("atlr-no-panel"), true);
    });
    var savedPanel = null;
    try { savedPanel = localStorage.getItem(PANEL_KEY); } catch (e) { /* private mode */ }
    if (savedPanel === "closed") setPanel(false, false);
  }
  // Selecting a passage needs the panel: it is where its cards are. The reader's own
  // choice is not overwritten: the next page load honours it again.
  function revealPanel() {
    if (document.body.classList.contains("atlr-no-panel")) setPanel(true, false);
  }

  // ---- the sheet (phone) ----
  // With the sheet open the header keeps its first row only, so the article keeps the screen above it.
  function openSheet() {
    if (!phone.matches) return;
    html.classList.add("atlr-sheet-open");
    fab.setAttribute("aria-expanded", "true");
    measure();
  }
  function closeSheet() {
    html.classList.remove("atlr-sheet-open");
    fab.setAttribute("aria-expanded", "false");
    measure();
  }
  fab.hidden = false;
  fab.addEventListener("click", function () {
    if (current) showIndex();
    openSheet();
    // The index from its top, whatever the panel showed last.
    panel.scrollTop = 0;
    panel.focus({ preventScroll: true });
  });
  closeBtn.addEventListener("click", function () {
    closeSheet();
    var back = current && (current.stop || current.marks[0]);
    if (back && back.getAttribute("tabindex") === "0") back.focus({ preventScroll: true });
    else fab.focus();
  });
  // A swipe down on the sheet's top strip (its handle and nav row) closes it.
  var touchY = null;
  panel.addEventListener("touchstart", function (e) {
    var t = e.touches[0];
    touchY = phone.matches && t.clientY - panel.getBoundingClientRect().top < 48 ? t.clientY : null;
  }, { passive: true });
  panel.addEventListener("touchend", function (e) {
    if (touchY !== null && e.changedTouches[0].clientY - touchY > 50) closeSheet();
    touchY = null;
  });

  // ---- selection ----
  var current = null;
  function updateStepper() {
    var list = visible();
    var at = list.indexOf(current);
    pos.textContent = at >= 0 ? (at + 1) + " of " + list.length : "\u2013 of " + list.length;
    // A passage a filter hides is no stop of the stepper: from it, both buttons lead back in.
    prevBtn.disabled = !list.length || at === 0;
    nextBtn.disabled = !list.length || at === list.length - 1;
  }
  function setHash(hash) {
    try { history.replaceState(null, "", hash ? location.pathname + location.search + hash : location.pathname + location.search); }
    catch (e) { /* a sandboxed frame */ }
    if (toCards) toCards.setAttribute("href", self + "?view=cards" + (hash || ""));
  }
  function clampText(section) {
    var box = section.querySelector(".atlr-clamp");
    var more = section.querySelector(".atlr-more");
    if (!box || !more) return;
    box.classList.remove("is-open");
    more.textContent = "Show all";
    more.setAttribute("aria-expanded", "false");
    var quote = box.querySelector(".articles-quote");
    // Longer than three lines, or a part of a sentence whose sentence "Show all" adds.
    more.hidden = !quote || (quote.scrollHeight <= quote.clientHeight + 2 && !quote.querySelector(".articles-affix"));
  }
  function select(p, opts) {
    opts = opts || {};
    hidePeek();
    revealPanel();
    if (current && current !== p) {
      current.marks.forEach(function (m) { m.classList.remove("is-sel"); });
      current.el.hidden = true;
      if (current.row) current.row.removeAttribute("aria-current");
    }
    current = p;
    p.marks.forEach(function (m) { m.classList.add("is-sel"); });
    // On a phone the sheet leaves out the passage's text when the article shows it above.
    p.el.classList.toggle("atlr-in-text", p.marks.length > 0);
    // Signing in from the panel comes back to this passage, not to the top of the article.
    each(p.el.querySelectorAll("a.articles-fb-signin[href]"), function (a) {
      var href = a.getAttribute("href"), at = href.indexOf("returnTo=");
      if (at >= 0) a.setAttribute("href", href.slice(0, at + 9) + encodeURIComponent(self + "#p-" + p.hex));
    });
    p.el.hidden = false;
    if (p.row) p.row.setAttribute("aria-current", "true");
    index.hidden = true;
    nav.hidden = false;
    missing.hidden = true;
    updateStepper();
    if (opts.hash !== false) setHash("#p-" + p.hex);
    else if (toCards) toCards.setAttribute("href", self + "?view=cards#p-" + p.hex);
    openSheet();
    panel.scrollTop = 0;
    clampText(p.el);
    // articles.js makes a formula box wider than the panel focusable on resize.
    try { window.dispatchEvent(new Event("resize")); } catch (e) { /* old engines */ }
    if (opts.scroll) scrollToPassage(p, opts.smooth);
    else if (opts.reveal && p.marks.length) {
      // A tap on a phone: the sheet must not cover the passage just tapped.
      var r = p.marks[0].getBoundingClientRect();
      if (r.top < headerH || r.top > window.innerHeight - sheetHeight() - 24) scrollToPassage(p, true);
    }
    placeBar();
    if (opts.focus) {
      var head = p.el.querySelector(".articles-p-h");
      if (head) head.focus({ preventScroll: true });
    }
  }
  function showIndex() {
    if (current) {
      current.marks.forEach(function (m) { m.classList.remove("is-sel"); });
      current.el.hidden = true;
      if (current.row) current.row.removeAttribute("aria-current");
    }
    var was = current;
    current = null;
    index.hidden = false;
    nav.hidden = true;
    mbar.hidden = true;
    setHash("");
    return was;
  }
  function step(delta) {
    var list = visible();
    if (!list.length) return;
    var at = current ? list.indexOf(current) : -1;
    var next = at === -1 ? (delta > 0 ? list[0] : list[list.length - 1]) : list[at + delta];
    if (next) select(next, { scroll: true, smooth: true });
  }
  prevBtn.addEventListener("click", function () { step(-1); });
  nextBtn.addEventListener("click", function () { step(1); });
  backBtn.addEventListener("click", function () {
    var was = showIndex();
    if (was && was.row) was.row.focus({ preventScroll: false });
    else panel.focus({ preventScroll: true });
  });

  // ---- the article: click, keyboard, hover ----
  function markOf(target) {
    var mark = target && target.closest ? target.closest("mark.atl[data-p]") : null;
    return mark && art.contains(mark) ? mark : null;
  }
  art.addEventListener("click", function (e) {
    var mark = markOf(e.target);
    if (!mark || mark.classList.contains("is-off")) return;
    var p = info[mark.getAttribute("data-p")];
    // A link inside the passage still opens; the passage is selected as well.
    if (p) select(p, { focus: !phone.matches && !e.target.closest("a"), reveal: true });
  });
  art.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" && e.key !== " ") return;
    var mark = markOf(e.target);
    if (!mark || mark.getAttribute("role") !== "button") return;
    e.preventDefault();
    var p = info[mark.getAttribute("data-p")];
    if (p) select(p, { focus: true });
  });

  var hovered = null;
  function hoverOff() {
    if (hovered) hovered.marks.forEach(function (m) { m.classList.remove("is-hover"); });
    if (hovered && hovered.row) hovered.row.classList.remove("is-hover");
    hovered = null;
    hidePeek();
  }
  function hidePeek() {
    if (!peek.hidden) peek.hidden = true;
  }
  function showPeek(p, mark) {
    var st = STATUS[p.status];
    peek.textContent = "";
    var word = el("span", "atlr-peek-st", st[0] + " " + st[1]);
    word.setAttribute("data-status", p.status);
    peek.appendChild(word);
    if (p.lead) {
      peek.appendChild(document.createTextNode(" \u00b7 "));
      peek.appendChild(el("code", null, p.lead));
      if (p.more) peek.appendChild(document.createTextNode(" +" + p.more));
      if (p.rel) peek.appendChild(document.createTextNode(" \u00b7 " + p.rel));
    } else if (p.searched) {
      peek.appendChild(document.createTextNode(" \u00b7 searched " + p.searched + " libraries"));
    }
    var rects = mark.getClientRects();
    var r = rects.length ? rects[0] : mark.getBoundingClientRect();
    peek.hidden = false;
    var h = peek.offsetHeight, w = peek.offsetWidth;
    var top = r.top - h - 6;
    if (top < headerH + 4) top = r.bottom + 6;
    peek.style.top = Math.round(top) + "px";
    peek.style.left = Math.round(Math.max(8, Math.min(r.left, window.innerWidth - w - 8))) + "px";
  }
  function hoverOn(p, mark) {
    if (hovered !== p) {
      hoverOff();
      hovered = p;
      p.marks.forEach(function (m) { m.classList.add("is-hover"); });
    }
    if (mark && finePointer.matches && !phone.matches && current !== p) showPeek(p, mark);
    else hidePeek();
  }
  art.addEventListener("mouseover", function (e) {
    var mark = markOf(e.target);
    if (!mark || mark.classList.contains("is-off")) { hoverOff(); return; }
    var p = info[mark.getAttribute("data-p")];
    if (p) hoverOn(p, mark);
  });
  art.addEventListener("mouseleave", hoverOff);
  window.addEventListener("scroll", hidePeek, { passive: true });

  // ---- the index ----
  index.addEventListener("click", function (e) {
    var row = e.target.closest ? e.target.closest("a.atlr-row[data-p]") : null;
    if (!row) return;
    var p = info[row.getAttribute("data-p")];
    if (!p) return;
    e.preventDefault();
    select(p, { scroll: true, smooth: true, focus: true });
  });
  index.addEventListener("mouseover", function (e) {
    var row = e.target.closest ? e.target.closest("a.atlr-row[data-p]") : null;
    var p = row ? info[row.getAttribute("data-p")] : null;
    if (p) { hoverOn(p, null); row.classList.add("is-hover"); } else hoverOff();
  });
  index.addEventListener("mouseleave", hoverOff);

  // ---- the panel's own controls ----
  slot.addEventListener("click", function (e) {
    var more = e.target.closest ? e.target.closest(".atlr-more") : null;
    if (more) {
      var box = more.previousElementSibling;
      var open = !box.classList.contains("is-open");
      box.classList.toggle("is-open", open);
      more.textContent = open ? "Show less" : "Show all";
      more.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    var copy = e.target.closest ? e.target.closest(".atlr-copy[data-p]") : null;
    if (!copy) return;
    var msg = copy.parentNode.querySelector(".atlr-copy-msg");
    var url = location.origin + self + "#p-" + copy.getAttribute("data-p");
    var say = function (words) { if (msg) msg.textContent = words; };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () { say("Link copied."); }, function () { say(url); });
    } else {
      say(url);
    }
  });

  // ---- keys: the left and right arrows step, Esc goes back one level ----
  function typing(target) {
    if (!target || !target.tagName) return false;
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(target.tagName) || target.isContentEditable) return true;
    // A formula or table box that scrolls sideways keeps its arrow keys.
    return target.getAttribute("tabindex") === "0" && target.scrollWidth > target.clientWidth + 1;
  }
  document.addEventListener("keydown", function (e) {
    if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey || typing(e.target)) return;
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      e.preventDefault();
      step(e.key === "ArrowRight" ? 1 : -1);
    } else if (e.key === "Escape") {
      if (!peek.hidden) { hidePeek(); return; }
      if (current) {
        var was = showIndex();
        var back = was.stop;
        if (back && back.getAttribute("tabindex") === "0") back.focus({ preventScroll: false });
        else if (was.row) was.row.focus();
        return;
      }
      if (html.classList.contains("atlr-sheet-open")) closeSheet();
    }
  });

  // ---- #p-<hex> links ----
  function fromHash(smooth) {
    var m = /^#p-([0-9a-f]{8,16})$/.exec(location.hash);
    if (!m) return false;
    var p = info[m[1]];
    if (!p) {
      showIndex();
      missing.textContent = "No passage #" + m[1] + " on this page; here are its passages.";
      missing.hidden = false;
      openSheet();
      return true;
    }
    if (!matches(p)) {
      active.status = null;
      active.library = null;
      active.central = false;
      applyFilter(true);
    }
    select(p, { scroll: true, smooth: smooth, hash: false });
    return true;
  }
  window.addEventListener("hashchange", function () { fromHash(true); });

  // ---- images that do not load (offline) give way to their description ----
  // The description takes the image's own box (its width and height), so nothing below it moves.
  function imageOff(img) {
    if (!img || img.tagName !== "IMG" || img.classList.contains("atlr-img-off")) return;
    var w = Number(img.getAttribute("width")), h = Number(img.getAttribute("height"));
    var alt = el("span", "atlr-img-alt", img.getAttribute("alt") || "Image not loaded (it loads from Wikimedia when online).");
    if (w > 0 && h > 0) {
      alt.classList.add("is-sized");
      alt.style.width = w + "px";
      alt.style.aspectRatio = w + " / " + h;
    }
    img.classList.add("atlr-img-off");
    img.parentNode.insertBefore(alt, img.nextSibling);
  }
  art.addEventListener("error", function (e) { imageOff(e.target); }, true);
  // An image that failed before this script ran.
  each(art.querySelectorAll("img"), function (img) { if (img.complete && !img.naturalWidth) imageOff(img); });

  // ---- formulas wider than the column scroll in their own box (and take keyboard focus) ----
  var wide = [];
  function fitWide() {
    wide.forEach(function (box) { box.classList.remove("atlr-wide"); box.removeAttribute("tabindex"); });
    var style = getComputedStyle(art);
    var right = art.getBoundingClientRect().left + art.clientWidth - parseFloat(style.paddingRight) + 1;
    var over = [];
    // A formula's MathML can reach past its own box, so its extent is that of its contents.
    var range = document.createRange();
    each(art.querySelectorAll(".mwe-math-element-inline"), function (box) {
      range.selectNodeContents(box);
      if (Math.max(box.getBoundingClientRect().right, range.getBoundingClientRect().right) > right) over.push(box);
    });
    // Display formulas, and on a phone tables and code, scroll sideways in their own box.
    each(art.querySelectorAll(".mwe-math-element-block, table, pre, .mw-highlight, .numblk"), function (box) {
      if (box.scrollWidth > box.clientWidth + 1 && getComputedStyle(box).overflowX !== "visible") over.push(box);
    });
    over.forEach(function (box) {
      if (box.classList.contains("mwe-math-element-inline")) box.classList.add("atlr-wide");
      box.setAttribute("tabindex", "0");
    });
    wide = over;
  }

  // ---- layout changes ----
  var timer = null;
  var lastWidth = 0;
  function relayout() {
    clearTimeout(timer);
    timer = setTimeout(function () {
      measure();
      if (art.clientWidth !== lastWidth) { lastWidth = art.clientWidth; fitWide(); }
      placeBar();
    }, 100);
  }
  window.addEventListener("resize", relayout);
  if (typeof ResizeObserver === "function") new ResizeObserver(relayout).observe(art);
  if (phone.addEventListener) phone.addEventListener("change", function () { if (!phone.matches) closeSheet(); relayout(); });

  lastWidth = art.clientWidth;
  fitWide();
  applyFilter(false);
  if (!fromHash(false)) updateStepper();
  // The browser's own jump to #p-<hex> happens before layout settles: place the passage again.
  window.addEventListener("load", function () {
    measure();
    if (current && /^#p-/.test(location.hash)) scrollToPassage(current, false);
    placeBar();
  });
})();
