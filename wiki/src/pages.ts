// Server-rendered chrome for the live wiki: the auth bar + editor injection on
// article pages, plus the History and Recent-changes pages.

import { htmlEscape } from "./engine/html.js";
import type { AuthUser } from "./auth.js";
import type { Annotation } from "./engine/types.js";

function safeJson(obj: unknown): string {
  return JSON.stringify(obj).replaceAll("</", "<\\/");
}

function fmtDate(ms: number): string {
  return new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC";
}

// Compact relative age ("4m ago", "3h ago", "2d ago") for the flags queue,
// where recency matters more than the exact timestamp.
function fmtAge(ms: number): string {
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}

// Injected into every article page (post-cache, varies by viewer): a sign-in
// prompt for anonymous viewers, or the editor + full annotation model for
// logged-in users. Also the upstream-staleness banner — per-request injection
// only, NEVER in the cached base (the cache invariant: latest_revid /
// last_upstream_check writes don't bump `version`, so the base page can't
// know about drift).
export function injectAuthAndEditor(
  page: string,
  opts: {
    slug: string;
    user: AuthUser | null;
    annotations: Annotation[];
    version?: number;
    // Staleness banner inputs: the pinned revid + the newest upstream revid
    // seen by the drift cron (articles.revid / articles.latest_revid).
    revid?: number | null;
    latestRevid?: number | null;
  },
): string {
  const ret = encodeURIComponent("/" + opts.slug);
  let inject: string;
  if (opts.user) {
    inject =
      `<script>window.__WL_SLUG__=${safeJson(opts.slug)};` +
      `window.__WL_USER__=${safeJson({ name: opts.user.name, role: opts.user.role })};` +
      // base_version for optimistic concurrency: the editor POSTs this back so
      // the server can 409 if the article was edited since this page loaded.
      `window.__WL_VERSION__=${safeJson(opts.version ?? 0)};` +
      `window.__WL_FULL_ANNOS__=${safeJson({ annotations: opts.annotations })};</script>\n` +
      `<link rel="stylesheet" href="/assets/review.css?v=3">\n` +
      // Bump ?v= when these assets change so browsers refetch (the URL is the
      // cache key; without this, returning users see the stale editor / CSS).
      `<script src="/assets/editor.js?v=8"></script>\n`;
  } else {
    inject =
      `<a id="wl-signin" href="/login?returnTo=${ret}" ` +
      `style="position:fixed;right:14px;bottom:14px;z-index:5000;background:#0969da;color:#fff;` +
      `text-decoration:none;padding:8px 14px;border-radius:8px;font:13px -apple-system,sans-serif;` +
      `box-shadow:0 2px 10px rgba(0,0,0,.2)">✎ Sign in to edit</a>\n`;
  }

  // Slim drift banner above the article chrome. `revid` is guaranteed numeric
  // by the guard, so the interpolated href can't break out of the attribute.
  if (typeof opts.latestRevid === "number" && typeof opts.revid === "number" && opts.latestRevid > opts.revid) {
    const banner =
      `<div id="wl-stale-banner" style="background:#fff8c5;border-bottom:1px solid #d4a72c;color:#4d2d00;` +
      `padding:8px 16px;text-align:center;font:13px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">` +
      `Annotations are pinned to an earlier Wikipedia revision — the article has changed upstream. ` +
      `<a href="https://en.wikipedia.org/w/index.php?diff=cur&amp;oldid=${opts.revid}" target="_blank" rel="noopener" ` +
      `style="color:#0969da">See what changed ↗</a></div>\n`;
    const m = page.match(/<body[^>]*>/);
    if (m && m.index !== undefined) {
      const end = m.index + m[0].length;
      page = page.slice(0, end) + "\n" + banner + page.slice(end);
    } else {
      inject = banner + inject;
    }
  }

  const idx = page.lastIndexOf("</body>");
  return idx === -1 ? page + inject : page.slice(0, idx) + inject + page.slice(idx);
}

// Warm academic-minimalist shell, matching home.ts: paper background, serif
// display headings (system stacks only), one deep-blue accent (#1a4b8c), and
// the status trio retuned to #2f7d4f / #b08020 / #b3372f. Text-on-tint badge
// colors use darker inks (#7d5a10, #9c2f28) to keep WCAG AA contrast.
const SHELL_CSS = `
*{box-sizing:border-box}
body{margin:0;background:#f7f4ee;color:#1f1d1a;line-height:1.55;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
:focus-visible{outline:2px solid #1a4b8c;outline-offset:2px}
.wl-header{display:flex;align-items:baseline;justify-content:space-between;gap:8px 20px;flex-wrap:wrap;max-width:900px;margin:0 auto;padding:20px 24px 0}
.wl-brand{font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;font-weight:700;font-size:1.1rem;color:#1f1d1a;text-decoration:none}
.wl-brand:hover{color:#1a4b8c}
.wl-navlink{color:#1a4b8c;text-decoration:none;font-size:.88rem}
.wl-navlink:hover{text-decoration:underline}
.wrap{max-width:900px;margin:0 auto;padding:26px 24px 64px}
h1{font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;font-size:1.6rem;margin:0 0 .35rem}
h1 a{color:inherit}
.lead{color:#5f594e;margin:0 0 20px}
table{border-collapse:collapse;width:100%;background:#fffdf9;border:1px solid #d8d0bd;border-radius:8px;overflow:hidden}
th,td{border-bottom:1px solid #ece6d8;padding:8px 12px;text-align:left;font-size:.9rem;vertical-align:top}
th{background:#f2eee3;text-transform:uppercase;letter-spacing:.05em;font-size:.72rem;color:#5f594e}
tr:last-child td{border-bottom:none}
a{color:#1a4b8c;text-decoration:none}
a:hover{text-decoration:underline}
.muted{color:#6e675a}
button.revert{font:inherit;font-size:.8rem;padding:3px 10px;border:1px solid #d8d0bd;border-radius:6px;background:#fffdf9;color:#1f1d1a;cursor:pointer;margin-right:4px}
button.revert:hover{border-color:#1a4b8c;color:#1a4b8c}
.wl-flag-reason{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.72rem;font-weight:600;white-space:nowrap}
.wl-fr-wrong_decl{background:rgba(179,55,47,.10);color:#9c2f28}
.wl-fr-wrong_status{background:rgba(176,128,32,.14);color:#7d5a10}
.wl-fr-irrelevant{background:#ece6d8;color:#5f594e}
.wl-fr-missing_formalization{background:rgba(26,75,140,.10);color:#1a4b8c}
.wl-fr-other{background:#ece6d8;color:#5f594e}
.wl-diff-card{background:#fffdf9;border:1px solid #d8d0bd;border-left-width:4px;border-radius:8px;padding:12px 16px;margin:0 0 14px}
.wl-diff-add{border-left-color:#2f7d4f}
.wl-diff-delete{border-left-color:#b3372f}
.wl-diff-modify{border-left-color:#b08020}
.wl-diff-head{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:6px}
.wl-diff-type{font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em}
.wl-diff-add .wl-diff-type{color:#2f7d4f}
.wl-diff-delete .wl-diff-type{color:#9c2f28}
.wl-diff-modify .wl-diff-type{color:#7d5a10}
.wl-diff-label{font-weight:600}
.wl-diff-card table{border:none;border-radius:0}
.wl-diff-card td{font-size:.85rem;word-break:break-word;white-space:pre-wrap}
.wl-diff-card td.wl-diff-field{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.8rem;white-space:nowrap}
`;

function shell(title: string, bodyInner: string, extraScript = ""): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · ${htmlEscape(title, false)}</title>
<meta name="robots" content="noindex">
<style>${SHELL_CSS}</style>
</head>
<body>
<header class="wl-header"><a class="wl-brand" href="/">WikiLean</a><span><a class="wl-navlink" href="/recent-changes">Recent changes</a> · <a class="wl-navlink" href="/flags">Flags</a> · <a class="wl-navlink" href="/about">About</a></span></header>
<div class="wrap">
${bodyInner}
</div>
${extraScript}
</body>
</html>`;
}

export interface HistoryRow {
  id: number;
  userId: string | null;
  userName: string | null;
  comment: string | null;
  createdAt: number;
  count: number;
}

export function historyPage(
  slug: string,
  displayTitle: string,
  rows: HistoryRow[],
  canEdit: boolean,
): string {
  const body =
    `<h1>Revision history — <a href="/${encodeURIComponent(slug)}">${htmlEscape(displayTitle, false)}</a></h1>` +
    `<p class="lead">${rows.length} revision${rows.length === 1 ? "" : "s"}, newest first.</p>` +
    `<div style="overflow-x:auto">` +
    `<table><thead><tr><th>When</th><th>Editor</th><th>Annotations</th><th>Comment</th><th></th></tr></thead><tbody>` +
    rows
      .map((r, i) => {
        const who = r.userId ? htmlEscape(r.userName ?? r.userId, false) : '<span class="muted">system</span>';
        // Rows are newest-first, so a revision's predecessor is the NEXT row.
        // The oldest revision has nothing to diff against.
        const prev = rows[i + 1];
        const diffLink = prev
          ? `<a href="/${encodeURIComponent(slug)}/diff/${prev.id}/${r.id}">diff</a> `
          : "";
        const revertBtn = canEdit
          ? `<button class="revert" data-rev="${r.id}">revert to this</button>`
          : "";
        return (
          `<tr><td>${fmtDate(r.createdAt)}</td><td>${who}</td><td>${r.count}</td>` +
          `<td>${r.comment ? htmlEscape(r.comment, false) : '<span class="muted">—</span>'}</td><td>${diffLink}${revertBtn}</td></tr>`
        );
      })
      .join("") +
    `</tbody></table></div>`;

  const script = canEdit
    ? `<script>
document.querySelectorAll("button.revert").forEach(function(b){
  b.addEventListener("click", function(){
    if(!confirm("Revert the article to revision #"+b.dataset.rev+"? This creates a new revision.")) return;
    b.disabled=true; b.textContent="reverting…";
    fetch("/api/article/${encodeURIComponent(slug)}/revert/"+b.dataset.rev,{method:"POST"})
      .then(function(r){return r.json()})
      .then(function(res){ if(res.ok){location.href="/${encodeURIComponent(slug)}"} else {alert("revert failed: "+(res.error||"")); b.disabled=false; b.textContent="revert to this";} })
      .catch(function(e){alert("revert failed: "+e); b.disabled=false; b.textContent="revert to this";});
  });
});
</script>`
    : "";

  return shell(`History: ${displayTitle}`, body, script);
}

export interface RecentRow {
  slug: string;
  displayTitle: string;
  id: number;
  userName: string | null;
  userId: string | null;
  comment: string | null;
  createdAt: number;
}

export function recentChangesPage(rows: RecentRow[]): string {
  const body =
    `<h1>Recent changes</h1>` +
    `<p class="lead">Latest annotation edits across all articles. Patrol here — open an article's history to revert.</p>` +
    `<div style="overflow-x:auto">` +
    `<table><thead><tr><th>When</th><th>Article</th><th>Editor</th><th>Comment</th><th></th></tr></thead><tbody>` +
    rows
      .map((r) => {
        const who = r.userId ? htmlEscape(r.userName ?? r.userId, false) : '<span class="muted">system</span>';
        return (
          `<tr><td>${fmtDate(r.createdAt)}</td>` +
          `<td><a href="/${encodeURIComponent(r.slug)}">${htmlEscape(r.displayTitle, false)}</a></td>` +
          `<td>${who}</td>` +
          `<td>${r.comment ? htmlEscape(r.comment, false) : '<span class="muted">—</span>'}</td>` +
          `<td><a href="/${encodeURIComponent(r.slug)}/history">history</a></td></tr>`
        );
      })
      .join("") +
    `</tbody></table></div>`;
  return shell("Recent changes", body);
}

// ---- /flags — the anonymous-report patrol queue (D-C4/D-C6) ----------------

// Human-readable labels + badge classes for the flag-reason enum. The reason
// column is CHECK-constrained server-side, but render defensively anyway:
// unknown values fall back to the escaped raw string with the neutral badge.
const FLAG_REASONS: Record<string, { label: string; cls: string }> = {
  wrong_decl: { label: "wrong decl", cls: "wl-fr-wrong_decl" },
  wrong_status: { label: "wrong status", cls: "wl-fr-wrong_status" },
  irrelevant: { label: "not relevant", cls: "wl-fr-irrelevant" },
  missing_formalization: { label: "missing formalization", cls: "wl-fr-missing_formalization" },
  other: { label: "other", cls: "wl-fr-other" },
};

export function flagsPage(
  flags: Array<{
    id: number;
    slug: string;
    displayTitle: string;
    annotationId: string | null;
    reason: string;
    comment: string | null;
    status: string;
    createdAt: number;
  }>,
  canResolve: boolean,
): string {
  const rows = flags
    .map((f) => {
      const r = FLAG_REASONS[f.reason] ?? { label: f.reason, cls: "wl-fr-other" };
      // Short-form annotation id: enough hex to find it, small enough to scan.
      const anno = f.annotationId
        ? `<code title="${htmlEscape(f.annotationId)}">${htmlEscape(f.annotationId.slice(0, 6), false)}…</code>`
        : '<span class="muted">article</span>';
      // "revert wl-resolve": the shared button.revert styling + a flag-specific
      // hook for the resolve script. The wl-resolve token must ONLY appear in
      // this canResolve-gated markup (never in SHELL_CSS) — viewers without
      // the role get a page with no resolve affordance at all.
      const actions = canResolve
        ? f.status === "open"
          ? `<button class="revert wl-resolve" data-flag="${f.id}" data-res="fixed">fixed</button>` +
            `<button class="revert wl-resolve" data-flag="${f.id}" data-res="dismissed">dismiss</button>`
          : `<span class="muted">${htmlEscape(f.status, false)}</span>`
        : "";
      return (
        `<tr data-flag-row="${f.id}"><td title="${htmlEscape(fmtDate(f.createdAt))}">${fmtAge(f.createdAt)}</td>` +
        `<td><a href="/${encodeURIComponent(f.slug)}">${htmlEscape(f.displayTitle, false)}</a></td>` +
        `<td>${anno}</td>` +
        `<td><span class="wl-flag-reason ${r.cls}">${htmlEscape(r.label, false)}</span></td>` +
        `<td>${f.comment ? htmlEscape(f.comment, false) : '<span class="muted">—</span>'}</td>` +
        (canResolve ? `<td>${actions}</td>` : "") +
        `</tr>`
      );
    })
    .join("");

  const body =
    `<h1>Flags</h1>` +
    `<p class="lead">Reader-reported problems, newest first. Open the article to fix the annotation, then resolve the flag.</p>` +
    (flags.length === 0
      ? `<p class="muted">No open flags — the queue is clear.</p>`
      : `<div style="overflow-x:auto">` +
        `<table><thead><tr><th>Age</th><th>Article</th><th>Annotation</th><th>Reason</th><th>Comment</th>` +
        (canResolve ? `<th></th>` : "") +
        `</tr></thead><tbody>${rows}</tbody></table></div>`);

  const script = canResolve
    ? `<script>
document.querySelectorAll("button.wl-resolve").forEach(function(b){ // (canResolve-only markup)
  b.addEventListener("click", function(){
    b.disabled=true; b.textContent="…";
    fetch("/api/flag/"+b.dataset.flag+"/resolve",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({resolution:b.dataset.res})})
      .then(function(r){return r.json()})
      .then(function(res){
        if(res.ok){
          var row=document.querySelector('[data-flag-row="'+b.dataset.flag+'"]');
          if(row){row.style.opacity=".45";var cell=b.parentElement;cell.innerHTML='<span class="muted">'+b.dataset.res+'</span>';}
        } else {alert("resolve failed: "+(res.error||"")); b.disabled=false; b.textContent=b.dataset.res==="fixed"?"fixed":"dismiss";}
      })
      .catch(function(e){alert("resolve failed: "+e); b.disabled=false; b.textContent=b.dataset.res==="fixed"?"fixed":"dismiss";});
  });
});
</script>`
    : "";

  return shell("Flags", body, script);
}

// ---- /:slug/diff/:fromId/:toId — field-level annotation diff (D-C6) --------

// Render one diffed value. These are raw annotation field values (user/AI
// content), so everything is escaped; non-strings (numbers, nested objects
// from exotic fields) round-trip through JSON for a stable representation.
function diffVal(v: unknown): string {
  if (v === undefined || v === null || v === "") return '<span class="muted">—</span>';
  return htmlEscape(typeof v === "string" ? v : JSON.stringify(v), false);
}

const DIFF_TYPE_LABEL: Record<"add" | "modify" | "delete", string> = {
  add: "added",
  modify: "modified",
  delete: "deleted",
};

export function diffPage(
  slug: string,
  displayTitle: string,
  fromId: number,
  toId: number,
  changes: Array<{
    annotationId: string;
    changeType: "add" | "modify" | "delete";
    label: string | null;
    fields: Array<{ field: string; from: unknown; to: unknown }>;
  }>,
): string {
  const cards = changes
    .map((ch) => {
      const label = ch.label
        ? `<span class="wl-diff-label">${htmlEscape(ch.label, false)}</span>`
        : "";
      const fieldRows = ch.fields
        .map(
          (f) =>
            `<tr><td class="wl-diff-field">${htmlEscape(f.field, false)}</td>` +
            `<td>${diffVal(f.from)}</td><td>${diffVal(f.to)}</td></tr>`,
        )
        .join("");
      const table = ch.fields.length
        ? `<div style="overflow-x:auto"><table><thead><tr><th>Field</th><th>From #${fromId}</th><th>To #${toId}</th></tr></thead>` +
          `<tbody>${fieldRows}</tbody></table></div>`
        : "";
      return (
        `<div class="wl-diff-card wl-diff-${ch.changeType}">` +
        `<div class="wl-diff-head"><span class="wl-diff-type">${DIFF_TYPE_LABEL[ch.changeType]}</span>` +
        `${label}<code title="annotation id">${htmlEscape(ch.annotationId, false)}</code></div>` +
        table +
        `</div>`
      );
    })
    .join("");

  const body =
    `<h1>Diff — <a href="/${encodeURIComponent(slug)}">${htmlEscape(displayTitle, false)}</a></h1>` +
    `<p class="lead">Revision #${fromId} → #${toId} · ` +
    `<a href="/${encodeURIComponent(slug)}/history">back to history</a></p>` +
    (changes.length === 0
      ? `<p class="muted">No annotation changes between these revisions.</p>`
      : cards);

  return shell(`Diff: ${displayTitle}`, body);
}
