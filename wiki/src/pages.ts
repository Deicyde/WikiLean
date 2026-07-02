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
    // P3: is the logged-in viewer watching this article? Drives the
    // ★ Watch / ★ Watching toggle state in the editor bar.
    isWatching?: boolean;
    // Propose-then-approve: pending AI proposals to update this article's human
    // annotations (PendingProposal[]). Rendered as an inline banner by editor.js.
    proposals?: unknown[];
  },
): string {
  const ret = encodeURIComponent("/" + opts.slug);
  let inject: string;
  if (opts.user) {
    inject =
      `<script>window.__WL_SLUG__=${safeJson(opts.slug)};` +
      // P3: ship the user id so the editor bar can link "editing as <name>" → /u/<id>.
      `window.__WL_USER__=${safeJson({ id: opts.user.id, name: opts.user.name, role: opts.user.role })};` +
      // base_version for optimistic concurrency: the editor POSTs this back so
      // the server can 409 if the article was edited since this page loaded.
      `window.__WL_VERSION__=${safeJson(opts.version ?? 0)};` +
      `window.__WL_WATCHING__=${safeJson(Boolean(opts.isWatching))};` +
      `window.__WL_PROPOSALS__=${safeJson(opts.proposals ?? [])};` +
      `window.__WL_FULL_ANNOS__=${safeJson({ annotations: opts.annotations })};</script>\n` +
      // v=4: warm-palette editor chrome + sticky bar offsets (W3 fixes #5/#6d).
      // v=5: anchor-editing fieldset styles (highlight-range box + Use-selection btn).
      `<link rel="stylesheet" href="/assets/review.css?v=5">\n` +
      // Bump ?v= when these assets change so browsers refetch (the URL is the
      // cache key; without this, returning users see the stale editor / CSS).
      // v=14: highlight-range editing — Section + Snippet are now exposed in
      // the panel with a "Use selection" button (typed anchors stay locked).
      // v=15: propose-then-approve inline banner (window.__WL_PROPOSALS__).
      `<script src="/assets/editor.js?v=15"></script>\n`;
  } else {
    inject =
      `<a id="wl-signin" href="/login?returnTo=${ret}" ` +
      // Accent token #1a4b8c (was GitHub-blue #0969da) — W3 fix #6a.
      `style="position:fixed;right:14px;bottom:14px;z-index:5000;background:#1a4b8c;color:#fff;` +
      `text-decoration:none;padding:8px 14px;border-radius:8px;font:13px -apple-system,sans-serif;` +
      `box-shadow:0 2px 10px rgba(0,0,0,.2)">✎ Sign in to edit</a>\n`;
  }

  // Slim drift banner above the article chrome. `revid` is guaranteed numeric
  // by the guard, so the interpolated href can't break out of the attribute.
  if (typeof opts.latestRevid === "number" && typeof opts.revid === "number" && opts.latestRevid > opts.revid) {
    // Warm amber tint derived from the palette's #b08020, ink #7d5a10,
    // accent link #1a4b8c (was GitHub yellow) — W3 fix #6b.
    const banner =
      `<div id="wl-stale-banner" style="background:rgba(176,128,32,.12);border-bottom:1px solid rgba(176,128,32,.4);color:#7d5a10;` +
      `padding:8px 16px;text-align:center;font:13px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">` +
      `Annotations are pinned to an earlier Wikipedia revision — the article has changed upstream. ` +
      `<a href="https://en.wikipedia.org/w/index.php?diff=cur&amp;oldid=${opts.revid}" target="_blank" rel="noopener" ` +
      `style="color:#1a4b8c">See what changed ↗</a></div>\n`;
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
.wl-kind{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.72rem;font-weight:600;white-space:nowrap;background:#ece6d8;color:#5f594e}
.wl-kind-edit{background:rgba(26,75,140,.10);color:#1a4b8c}
.wl-kind-revert{background:rgba(179,55,47,.10);color:#9c2f28}
.wl-kind-pipeline{background:rgba(47,125,79,.12);color:#2f7d4f}
.wl-kind-contribution{background:rgba(176,128,32,.14);color:#7d5a10}
.wl-unpatrolled{color:#b08020;font-size:.65rem;vertical-align:1px;margin-right:6px;cursor:default}
.wl-patrolled{color:#2f7d4f;font-size:.8rem;white-space:nowrap;cursor:default}
.wl-filterbar{color:#6e675a;font-size:.85rem;margin:0 0 14px}
.wl-filterbar a.active{font-weight:700;text-decoration:underline}
.wl-rq{display:inline-block;padding:1px 7px;border-radius:9px;font-size:.7rem;font-weight:600;white-space:nowrap;background:rgba(26,75,140,.08);color:#1a4b8c;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.wl-stat-num{font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
.wl-stats-foot{color:#6e675a;font-size:.82rem;margin:22px 0 0;line-height:1.6}
h2.wl-stats-h{font-family:Charter,'Bitstream Charter','Iowan Old Style',Georgia,'Times New Roman',serif;font-size:1.15rem;margin:28px 0 10px}
.wl-profile-hdr{display:flex;gap:18px;align-items:center;margin:0 0 6px}
.wl-avatar{width:56px;height:56px;border-radius:50%;background:#ece6d8;flex:0 0 56px}
.wl-avatar-fallback{display:inline-flex;align-items:center;justify-content:center;font-weight:700;font-size:1.4rem;color:#5f594e}
.wl-profile-name{margin:0}
.wl-role{display:inline-block;padding:1px 8px;border-radius:10px;font-size:.7rem;font-weight:700;background:rgba(26,75,140,.10);color:#1a4b8c;text-transform:uppercase;letter-spacing:.04em;vertical-align:3px;margin-left:6px}
.wl-profile-stats{display:flex;gap:18px;color:#5f594e;font-size:.92rem;margin:14px 0 18px;flex-wrap:wrap}
.wl-profile-stats b{color:#1f1d1a}
.wl-watch-list{padding-left:1.2em;margin:8px 0 8px;columns:2;column-gap:36px}
.wl-watch-list li{margin:3px 0;break-inside:avoid}

/* Theme-toggle button (matches the article-page version in style.css). */
.wl-theme-toggle{background:transparent;border:1px solid #d8d0bd;color:#5f594e;border-radius:50%;width:28px;height:28px;padding:0;line-height:1;font-size:14px;cursor:pointer;display:inline-flex;align-items:center;justify-content:center;margin-left:10px}
.wl-theme-toggle:hover{color:#1f1d1a;border-color:#1a4b8c}

/* ---- Dark mode for the chrome pages. Additive [data-theme="dark"] rules
   override the warm-paper defaults above without touching the existing
   selectors — same approach as style.css's article-page dark block. */
[data-theme="dark"] body{background:#1a1816;color:#ebe5d8}
[data-theme="dark"] :focus-visible{outline-color:#6e9adf}
[data-theme="dark"] .wl-brand{color:#ebe5d8}
[data-theme="dark"] .wl-brand:hover{color:#6e9adf}
[data-theme="dark"] .wl-navlink{color:#6e9adf}
[data-theme="dark"] .lead{color:#9a9081}
[data-theme="dark"] table{background:#232020;border-color:#4d4742}
[data-theme="dark"] th,[data-theme="dark"] td{border-bottom-color:#3a3530}
[data-theme="dark"] th{background:#2a2725;color:#9a9081}
[data-theme="dark"] a{color:#6e9adf}
[data-theme="dark"] .muted{color:#8a8278}
[data-theme="dark"] button.revert{background:#232020;color:#ebe5d8;border-color:#4d4742}
[data-theme="dark"] button.revert:hover{color:#6e9adf;border-color:#6e9adf}
[data-theme="dark"] .wl-fr-wrong_decl{background:rgba(226,104,95,.18);color:#f08e85}
[data-theme="dark"] .wl-fr-wrong_status{background:rgba(212,160,66,.20);color:#e2bf78}
[data-theme="dark"] .wl-fr-irrelevant{background:#2e2a2f;color:#9a9081}
[data-theme="dark"] .wl-fr-missing_formalization{background:rgba(110,154,223,.16);color:#6e9adf}
[data-theme="dark"] .wl-fr-other{background:#2e2a2f;color:#9a9081}
[data-theme="dark"] .wl-diff-card{background:#232020;border-color:#4d4742}
[data-theme="dark"] .wl-diff-add{border-left-color:#4ca97a}
[data-theme="dark"] .wl-diff-delete{border-left-color:#e2685f}
[data-theme="dark"] .wl-diff-modify{border-left-color:#d4a042}
[data-theme="dark"] .wl-diff-add .wl-diff-type{color:#8fd4ad}
[data-theme="dark"] .wl-diff-delete .wl-diff-type{color:#f08e85}
[data-theme="dark"] .wl-diff-modify .wl-diff-type{color:#e2bf78}
[data-theme="dark"] .wl-kind{background:#2e2a2f;color:#9a9081}
[data-theme="dark"] .wl-kind-edit{background:rgba(110,154,223,.16);color:#6e9adf}
[data-theme="dark"] .wl-kind-revert{background:rgba(226,104,95,.18);color:#f08e85}
[data-theme="dark"] .wl-kind-pipeline{background:rgba(76,169,122,.18);color:#8fd4ad}
[data-theme="dark"] .wl-kind-contribution{background:rgba(212,160,66,.20);color:#e2bf78}
[data-theme="dark"] .wl-unpatrolled{color:#d4a042}
[data-theme="dark"] .wl-patrolled{color:#8fd4ad}
[data-theme="dark"] .wl-filterbar{color:#9a9081}
[data-theme="dark"] .wl-filterbar a.active{color:#ebe5d8}
[data-theme="dark"] .wl-rq{background:rgba(110,154,223,.14);color:#6e9adf}
[data-theme="dark"] .wl-stats-foot{color:#9a9081}
[data-theme="dark"] .wl-avatar{background:#2e2a2f}
[data-theme="dark"] .wl-avatar-fallback{color:#9a9081}
[data-theme="dark"] .wl-role{background:rgba(110,154,223,.16);color:#6e9adf}
[data-theme="dark"] .wl-profile-stats{color:#9a9081}
[data-theme="dark"] .wl-profile-stats b{color:#ebe5d8}
[data-theme="dark"] .wl-theme-toggle{color:#9a9081;border-color:#4d4742}
[data-theme="dark"] .wl-theme-toggle:hover{color:#ebe5d8;border-color:#6e9adf}
`;

function shell(title: string, bodyInner: string, extraScript = ""): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>WikiLean · ${htmlEscape(title, false)}</title>
<meta name="robots" content="noindex">
<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");document.documentElement.dataset.theme=t;}catch(e){}})();</script>
<style>${SHELL_CSS}</style>
</head>
<body>
<header class="wl-header"><a class="wl-brand" href="/">WikiLean</a><span><a class="wl-navlink" href="/recent-changes">Recent changes</a> · <a class="wl-navlink" href="/proposals">Proposals</a> · <a class="wl-navlink" href="/flags">Flags</a> · <a class="wl-navlink" href="/stats">Stats</a> · <a class="wl-navlink" href="/about">About</a><button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button></span></header>
<div class="wrap">
${bodyInner}
</div>
<script>(function(){var b=document.getElementById("wl-theme-toggle");if(!b)return;b.addEventListener("click",function(){var r=document.documentElement;var n=r.dataset.theme==="dark"?"light":"dark";r.dataset.theme=n;try{localStorage.setItem("wl-theme",n);}catch(e){}});})();</script>
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
  kind: string; // edit | revert | seed | pipeline | contribution
  patrolledBy: string | null;
  patrolledAt: number | null;
  patrollerName: string | null;
}

// The revisions.kind vocabulary (0004), as filter values for ?kind=.
export const RECENT_KINDS = ["edit", "revert", "seed", "pipeline", "contribution"] as const;

// One patrol cell. Only human edits (kind='edit') participate: unpatrolled
// rows get a subtle amber marker (everyone) + a mark-patrolled button
// (patroller/admin only — same UI#3 rule as revert buttons: never render an
// affordance that always 403s); patrolled rows show who/when in the title
// attribute (hover).
function patrolCell(r: RecentRow, canPatrol: boolean): string {
  if (r.kind !== "edit") return "";
  if (r.patrolledBy === null) {
    return (
      `<span class="wl-unpatrolled" title="awaiting patrol">●</span>` +
      (canPatrol ? `<button class="revert wl-patrol" data-rev="${r.id}">mark patrolled</button>` : "")
    );
  }
  const who = htmlEscape(r.patrollerName ?? r.patrolledBy, true);
  const when = r.patrolledAt !== null ? htmlEscape(fmtDate(r.patrolledAt), true) : "";
  return `<span class="wl-patrolled" title="patrolled by ${who}${when ? " · " + when : ""}">✓ patrolled</span>`;
}

export function recentChangesPage(
  rows: RecentRow[],
  opts: { kind: string | null; canPatrol: boolean; watching?: boolean; loggedIn?: boolean },
): string {
  // Filter chrome: all + one link per revisions.kind value. ?watching=1 is an
  // additional axis (logged-in only); we render it as a separate "scope"
  // section so the kind filters compose with it.
  const watchingActive = Boolean(opts.watching);
  const kindBase = watchingActive ? "/recent-changes?watching=1" : "/recent-changes";
  const kindLinks = [
    `<a href="${kindBase}"${opts.kind === null ? ' class="active"' : ""}>all</a>`,
    ...RECENT_KINDS.map(
      (k) =>
        `<a href="${watchingActive ? `${kindBase}&kind=${k}` : `/recent-changes?kind=${k}`}"${opts.kind === k ? ' class="active"' : ""}>${k}</a>`,
    ),
  ].join(" · ");

  // Scope chrome: visible only to logged-in viewers (anonymous viewers can't
  // have a watchlist, so showing the tab would be confusing).
  let scopeBar = "";
  if (opts.loggedIn) {
    const kindSuffix = opts.kind ? `&kind=${opts.kind}` : "";
    scopeBar =
      `<p class="wl-filterbar">Scope: ` +
      `<a href="/recent-changes${opts.kind ? `?kind=${opts.kind}` : ""}"${!watchingActive ? ' class="active"' : ""}>everything</a>` +
      ` · ` +
      `<a href="/recent-changes?watching=1${kindSuffix}"${watchingActive ? ' class="active"' : ""}>your watchlist</a>` +
      `</p>`;
  }

  const heading = watchingActive ? "Recent changes · watchlist" : "Recent changes";
  const intro = watchingActive
    ? `<p class="lead">Latest edits to articles you're watching. Add more with the <b>★ Watch</b> button on any article.</p>`
    : `<p class="lead">Latest annotation edits across all articles. Patrol here — open an article's history to revert.</p>`;

  const body =
    `<h1>${heading}</h1>` +
    intro +
    scopeBar +
    `<p class="wl-filterbar">Show: ${kindLinks}</p>` +
    `<div style="overflow-x:auto">` +
    `<table><thead><tr><th>When</th><th>Article</th><th>Editor</th><th>Kind</th><th>Comment</th><th>Patrol</th><th></th></tr></thead><tbody>` +
    rows
      .map((r) => {
        const who = r.userId ? htmlEscape(r.userName ?? r.userId, false) : '<span class="muted">system</span>';
        return (
          `<tr><td>${fmtDate(r.createdAt)}</td>` +
          `<td><a href="/${encodeURIComponent(r.slug)}">${htmlEscape(r.displayTitle, false)}</a></td>` +
          `<td>${who}</td>` +
          `<td><span class="wl-kind wl-kind-${htmlEscape(r.kind, true)}">${htmlEscape(r.kind, false)}</span></td>` +
          `<td>${r.comment ? htmlEscape(r.comment, false) : '<span class="muted">—</span>'}</td>` +
          `<td>${patrolCell(r, opts.canPatrol)}</td>` +
          `<td><a href="/${encodeURIComponent(r.slug)}/history">history</a></td></tr>`
        );
      })
      .join("") +
    `</tbody></table></div>`;

  const script = opts.canPatrol
    ? `<script>
document.querySelectorAll("button.wl-patrol").forEach(function(b){ // (canPatrol-only markup)
  b.addEventListener("click", function(){
    b.disabled=true; b.textContent="…";
    fetch("/api/revision/"+b.dataset.rev+"/patrol",{method:"POST"})
      .then(function(r){return r.json()})
      .then(function(res){
        if(res.ok){ var cell=b.parentElement; cell.innerHTML='<span class="wl-patrolled">✓ patrolled</span>'; }
        else { alert("patrol failed: "+(res.error||"")); b.disabled=false; b.textContent="mark patrolled"; }
      })
      .catch(function(e){alert("patrol failed: "+e); b.disabled=false; b.textContent="mark patrolled";});
  });
});
</script>`
    : "";

  return shell("Recent changes", body, script);
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

// ---- /stats — live experiment instrumentation (P2a) -------------------------
// Every number on this page is a cheap SQL aggregate (count columns, GROUP
// BYs) — the annotation blobs are never parsed at request time. Each row names
// the research question it feeds (RQ1–RQ8, docs/research-plan.md): RQ1
// human-correction rates by field, RQ2 endorse-vs-modify ratio, RQ3 AI
// dissent/ladder blocks, RQ4 time-to-correction survival, RQ5 annotation
// survival across AI passes, RQ6 inter-generation AI agreement (deferred),
// RQ7 confidence calibration (deferred), RQ8 cost per accepted annotation.
// RQ3 reads from the runner's sidecars (decisions.jsonl), not D1, and RQ6/RQ7
// are deferred — all three are deliberately absent here. Median
// time-to-first-human-touch is omitted: it needs a per-annotation
// first-pipeline-event × first-human-event self-join over annotation_events,
// which is not a cheap single SQL pass on D1.

export interface StatsEventCell {
  eventType: string;
  actorType: string;
  allTime: number;
  last30d: number;
}

export interface StatsData {
  articles: {
    total: number;
    neverReviewed: number;
    fresh: number; // reviewed within the last 30d
    stale: number; // reviewed, but >30d ago
    drifted: number; // moderation_state.wp_drifted = 1
    parked: number; // state in (moved, deleted, needs_human)
  };
  // From the per-article count columns (D-C5) — by status, tombstones
  // excluded. Provenance totals would need a blob parse; the human-provenance
  // signal is read from endorse/modify events instead (RQ2).
  annotations: { formalized: number; partial: number; notFormalized: number; pendingCounts: number };
  events: StatsEventCell[]; // event_type × actor_type, all-time + last 30d
  flags: { open: number; fixed: number; dismissed: number };
  revisions: Array<{ kind: string; count: number }>;
  patrol: { unpatrolledEdits: number; patrolledEdits: number };
  runs: Array<{
    kind: string;
    runs: number;
    articles: number;
    errors: number;
    tokens: number;
    cost: number | null;
  }>;
  // Propose-then-approve lifecycle (the AI-precision measure): acceptance
  // rate = approved / (approved + rejected); stale = target vanished before a
  // decision. meanDecisionMs is null until the first decision.
  proposals: {
    pending: number;
    approved: number;
    rejected: number;
    stale: number;
    meanDecisionMs: number | null;
  };
}

function statNum(n: number): string {
  return `<td class="wl-stat-num">${n.toLocaleString("en-US")}</td>`;
}

function rqCell(labels: string): string {
  return `<td>${labels
    .split(" ")
    .map((l) => `<span class="wl-rq">${htmlEscape(l, false)}</span>`)
    .join(" ")}</td>`;
}

function statRow(metric: string, n: number, rq: string): string {
  return `<tr><td>${htmlEscape(metric, false)}</td>${statNum(n)}${rqCell(rq)}</tr>`;
}

function statTable(head: string, rows: string): string {
  return `<div style="overflow-x:auto"><table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

export function statsPage(d: StatsData): string {
  const metricHead = `<th>Metric</th><th>Count</th><th>Feeds</th>`;

  const reviewRows =
    statRow("Articles", d.articles.total, "RQ8") +
    statRow("Never reviewed by the pipeline", d.articles.neverReviewed, "RQ5") +
    statRow("Review fresh (≤30d)", d.articles.fresh, "RQ5") +
    statRow("Review stale (>30d)", d.articles.stale, "RQ5") +
    statRow("Drifted from pinned Wikipedia revision", d.articles.drifted, "RQ8") +
    statRow("Parked (moved / deleted / needs human)", d.articles.parked, "RQ4");

  const annRows =
    statRow("Formalized", d.annotations.formalized, "RQ8") +
    statRow("Partial", d.annotations.partial, "RQ8") +
    statRow("Not formalized", d.annotations.notFormalized, "RQ8") +
    statRow("Articles awaiting count backfill", d.annotations.pendingCounts, "RQ8");

  const evTotal = { allTime: 0, last30d: 0 };
  const endorse = { allTime: 0, last30d: 0 };
  const reject = { allTime: 0, last30d: 0 };
  const modifyHuman = { allTime: 0, last30d: 0 };
  for (const e of d.events) {
    evTotal.allTime += e.allTime;
    evTotal.last30d += e.last30d;
    if (e.eventType === "endorse") {
      endorse.allTime += e.allTime;
      endorse.last30d += e.last30d;
    }
    if (e.eventType === "reject") {
      reject.allTime += e.allTime;
      reject.last30d += e.last30d;
    }
    if (e.eventType === "modify" && e.actorType === "human") {
      modifyHuman.allTime += e.allTime;
      modifyHuman.last30d += e.last30d;
    }
  }
  const eventRows =
    d.events
      .map(
        (e) =>
          `<tr><td><code>${htmlEscape(e.eventType, false)}</code></td>` +
          `<td><code>${htmlEscape(e.actorType, false)}</code></td>` +
          `${statNum(e.last30d)}${statNum(e.allTime)}${rqCell("RQ1 RQ5")}</tr>`,
      )
      .join("") +
    `<tr><td colspan="2"><b>All events</b></td>${statNum(evTotal.last30d)}${statNum(evTotal.allTime)}${rqCell("RQ1")}</tr>`;

  const signalRows =
    `<tr><td>Endorsements (human agrees with AI)</td>${statNum(endorse.last30d)}${statNum(endorse.allTime)}${rqCell("RQ2")}</tr>` +
    `<tr><td>Human modifications</td>${statNum(modifyHuman.last30d)}${statNum(modifyHuman.allTime)}${rqCell("RQ1 RQ2")}</tr>` +
    `<tr><td>Rejections (human veto / tombstone)</td>${statNum(reject.last30d)}${statNum(reject.allTime)}${rqCell("RQ1 RQ4")}</tr>`;

  const flagRows =
    statRow("Open flags", d.flags.open, "RQ4") +
    statRow("Resolved: fixed", d.flags.fixed, "RQ4") +
    statRow("Resolved: dismissed", d.flags.dismissed, "RQ4");

  const revisionRows =
    d.revisions.map((r) => statRow(`Revisions: ${r.kind}`, r.count, "RQ5")).join("") +
    statRow("Human edits awaiting patrol", d.patrol.unpatrolledEdits, "RQ4") +
    statRow("Human edits patrolled", d.patrol.patrolledEdits, "RQ4");

  const runTotal = d.runs.reduce(
    (acc, r) => ({
      runs: acc.runs + r.runs,
      articles: acc.articles + r.articles,
      errors: acc.errors + r.errors,
      tokens: acc.tokens + r.tokens,
      cost: r.cost === null ? acc.cost : (acc.cost ?? 0) + r.cost,
    }),
    { runs: 0, articles: 0, errors: 0, tokens: 0, cost: null as number | null },
  );
  const fmtCost = (c: number | null): string =>
    `<td class="wl-stat-num">${c === null ? '<span class="muted">—</span>' : "$" + c.toFixed(2)}</td>`;
  const runRow = (label: string, r: { runs: number; articles: number; errors: number; tokens: number; cost: number | null }) =>
    `<tr><td>${htmlEscape(label, false)}</td>${statNum(r.runs)}${statNum(r.articles)}${statNum(r.errors)}${statNum(r.tokens)}${fmtCost(r.cost)}${rqCell("RQ8")}</tr>`;
  const runsRows =
    d.runs.map((r) => runRow(r.kind, r)).join("") + runRow("all runs", runTotal);

  const body =
    `<h1>Stats</h1>` +
    `<p class="lead">Live instrumentation for the human+AI moderation experiment. Cached for up to 5 minutes; ` +
    `each row names the research question (RQ1–RQ8) it feeds.</p>` +
    `<h2 class="wl-stats-h">Articles by review state</h2>` +
    statTable(metricHead, reviewRows) +
    `<h2 class="wl-stats-h">Annotations by status</h2>` +
    statTable(metricHead, annRows) +
    `<p class="muted" style="font-size:.82rem">By-provenance totals are deliberately not computed here (they would ` +
    `require parsing every annotation blob per request); the human-provenance signal is read from the events below.</p>` +
    `<h2 class="wl-stats-h">Annotation events (event type × actor)</h2>` +
    statTable(`<th>Event</th><th>Actor</th><th>Last 30d</th><th>All time</th><th>Feeds</th>`, eventRows) +
    `<h2 class="wl-stats-h">Human signals</h2>` +
    statTable(`<th>Signal</th><th>Last 30d</th><th>All time</th><th>Feeds</th>`, signalRows) +
    `<h2 class="wl-stats-h">Reader flags</h2>` +
    statTable(metricHead, flagRows) +
    `<h2 class="wl-stats-h">Revisions &amp; patrol</h2>` +
    statTable(metricHead, revisionRows) +
    `<h2 class="wl-stats-h">Pipeline runs</h2>` +
    statTable(`<th>Kind</th><th>Runs</th><th>Articles</th><th>Errors</th><th>Tokens</th><th>Cost</th><th>Feeds</th>`, runsRows) +
    `<h2 class="wl-stats-h">AI proposals (propose-then-approve)</h2>` +
    statTable(
      metricHead,
      statRow("Pending (awaiting a human)", d.proposals.pending, "RQ2") +
        statRow("Approved", d.proposals.approved, "RQ1 RQ2") +
        statRow("Rejected", d.proposals.rejected, "RQ1 RQ2") +
        statRow("Stale (target vanished undecided)", d.proposals.stale, "RQ5") +
        `<tr><td>Acceptance rate (approved ÷ decided)</td><td class="wl-stat-num">${
          d.proposals.approved + d.proposals.rejected > 0
            ? Math.round((100 * d.proposals.approved) / (d.proposals.approved + d.proposals.rejected)) + "%"
            : "—"
        }</td>${rqCell("RQ2")}</tr>` +
        `<tr><td>Mean time to decision</td><td class="wl-stat-num">${
          d.proposals.meanDecisionMs !== null && d.proposals.meanDecisionMs >= 0
            ? (d.proposals.meanDecisionMs / 3600000).toFixed(1) + "h"
            : "—"
        }</td>${rqCell("RQ4")}</tr>`,
    ) +
    `<p class="wl-stats-foot"><b>Reading a zero:</b> every instrument on this page has shipped, so a 0 in any row ` +
    `means that instrumentation is broken, not that nothing happened — investigate before celebrating. ` +
    `Median time-to-first-human-touch is omitted: it needs a per-annotation pipeline-event × human-event self-join ` +
    `that is not a cheap single SQL pass on D1. RQ3 (AI dissent / ladder blocks) is measured in the runner&#x27;s ` +
    `decision sidecars, not in this database; RQ6 (inter-generation agreement) and RQ7 (confidence calibration) ` +
    `are deferred pending their sample/field (docs/research-plan.md).</p>`;

  return shell("Stats", body);
}

// ---- /u/:id — user profile (P3 contribution-loop) --------------------------

export interface UserProfileRow {
  id: number;
  slug: string;
  displayTitle: string;
  comment: string | null;
  kind: string;
  createdAt: number;
}

export function userProfilePage(
  profile: {
    id: string;
    name: string | null;
    image: string | null;
    role: string;
    createdAt: number | null;
  },
  stats: {
    totalEdits: number;
    articlesTouched: number;
    humanRevs: number;
  },
  recent: UserProfileRow[],
  watching: string[], // slugs watched by THIS user (only populated when viewing own profile)
  isSelf: boolean,
): string {
  const name = htmlEscape(profile.name ?? profile.id.slice(0, 8), false);
  const avatar = profile.image
    ? `<img src="${htmlEscape(profile.image, true)}" alt="" class="wl-avatar">`
    : `<span class="wl-avatar wl-avatar-fallback">${name.charAt(0).toUpperCase()}</span>`;
  const role =
    profile.role && profile.role !== "user"
      ? ` <span class="wl-role">${htmlEscape(profile.role, false)}</span>`
      : "";
  const joined = profile.createdAt
    ? ` · joined ${htmlEscape(fmtDate(profile.createdAt), false)}`
    : "";

  const recentRows = recent.length
    ? recent
        .map(
          (r) =>
            `<tr><td>${fmtDate(r.createdAt)}</td>` +
            `<td><a href="/${encodeURIComponent(r.slug)}">${htmlEscape(r.displayTitle, false)}</a></td>` +
            `<td><span class="wl-kind wl-kind-${htmlEscape(r.kind, true)}">${htmlEscape(r.kind, false)}</span></td>` +
            `<td>${r.comment ? htmlEscape(r.comment, false) : '<span class="muted">—</span>'}</td>` +
            `<td><a href="/${encodeURIComponent(r.slug)}/history">history</a></td></tr>`,
        )
        .join("")
    : `<tr><td colspan="5" class="muted">No edits yet.</td></tr>`;

  const watchSection = isSelf
    ? `<h2 class="wl-stats-h">Watchlist (${watching.length})</h2>` +
      (watching.length
        ? `<ul class="wl-watch-list">${watching
            .map(
              (s) =>
                `<li><a href="/${encodeURIComponent(s)}">${htmlEscape(s.replace(/_/g, " "), false)}</a></li>`,
            )
            .join("")}</ul>` +
          `<p class="muted" style="font-size:.85rem">Filter <a href="/recent-changes?watching=1">recent changes</a> to just these articles.</p>`
        : `<p class="muted">No watched articles yet. Click <b>★ Watch</b> in the editor bar of any article to add it here.</p>`)
    : "";

  const body =
    `<div class="wl-profile-hdr">${avatar}` +
    `<div><h1 class="wl-profile-name">${name}${role}</h1>` +
    `<p class="muted" style="font-size:.9rem">${htmlEscape(profile.id, false)}${joined}</p></div></div>` +
    `<div class="wl-profile-stats">` +
    `<span><b>${stats.totalEdits}</b> revisions</span>` +
    `<span><b>${stats.articlesTouched}</b> articles touched</span>` +
    `<span><b>${stats.humanRevs}</b> direct edits (non-revert)</span>` +
    `</div>` +
    watchSection +
    `<h2 class="wl-stats-h">Recent revisions</h2>` +
    `<div style="overflow-x:auto"><table>` +
    `<thead><tr><th>When</th><th>Article</th><th>Kind</th><th>Comment</th><th></th></tr></thead>` +
    `<tbody>${recentRows}</tbody></table></div>`;

  return shell(name, body);
}

// ---- /proposals — the cross-article propose-then-approve review queue -------
// One row per pending AI proposal (from the proposals lifecycle table), with
// the target annotation's CURRENT values beside the proposed delta so the
// reviewer can judge without opening the article. Approve/Reject post the
// existing /api/article/:slug contract (approve carries that slug's current
// version as base_version; a 409 mid-queue just reloads).
export interface ProposalQueueRow {
  proposalId: string;
  slug: string;
  displayTitle: string;
  version: number;
  annotationId: string;
  label: string; // target annotation's label ('' if the target is gone)
  current: Record<string, unknown> | null; // target's current approvable fields
  fields: Record<string, unknown>; // the proposed delta
  reason: string;
  model: string | null;
  createdAt: number;
}

function fmtVal(v: unknown): string {
  if (v === null || v === undefined) return "∅";
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    return typeof o.decl === "string" ? String(o.decl) : JSON.stringify(v);
  }
  return String(v);
}

export function proposalsQueuePage(
  rows: ProposalQueueRow[],
  meta: { total: number; forbidden?: boolean },
): string {
  if (meta.forbidden) {
    return shell(
      "AI proposals",
      `<h1>AI proposals</h1><p>Reviewing proposals is a patroller/admin task. ` +
        `If you'd like to help moderate, <a class="wl-navlink" href="/about">get in touch</a>.</p>`,
    );
  }
  const truncNote =
    meta.total > rows.length
      ? `<p class="muted">Showing the newest ${rows.length} of <b>${meta.total}</b> pending proposals — decide some to see the rest.</p>`
      : "";
  const body =
    `<h1>AI proposals</h1>` +
    `<p class="muted">The AI may propose updates to human-owned annotations but never applies them ` +
    `(<a class="wl-navlink" href="/about">how moderation works</a>). Approving applies the change and keeps the annotation yours; ` +
    `rejecting remembers the delta so it is not re-proposed.</p>` +
    truncNote +
    (rows.length === 0
      ? `<p><b>No pending proposals.</b> New ones appear here after AI review passes.</p>`
      : `<div style="overflow-x:auto"><table>` +
        `<thead><tr><th>Article</th><th>Annotation</th><th>Proposed change</th><th>Why</th><th>Age</th><th></th></tr></thead>` +
        `<tbody>` +
        rows
          .map((r) => {
            const delta = Object.entries(r.fields)
              .map(([k, v]) => {
                const from = r.current ? fmtVal(r.current[k]) : "?";
                return `<code>${htmlEscape(k, false)}</code> ${htmlEscape(from, false)} → <b>${htmlEscape(fmtVal(v), false)}</b>`;
              })
              .join("<br>");
            const age = Math.max(0, Math.round((Date.now() - r.createdAt) / 86400000));
            return (
              `<tr data-pid="${htmlEscape(r.proposalId, false)}" data-slug="${htmlEscape(r.slug)}" data-ver="${r.version}">` +
              `<td><a class="wl-navlink" href="/${htmlEscape(r.slug)}">${htmlEscape(r.displayTitle)}</a></td>` +
              `<td>${htmlEscape(r.label || r.annotationId)}</td>` +
              `<td>${delta}</td>` +
              `<td class="muted">${htmlEscape(r.reason)}${r.model ? `<br><span class="muted">${htmlEscape(r.model, false)}</span>` : ""}</td>` +
              `<td class="muted">${age}d</td>` +
              `<td style="white-space:nowrap">` +
              `<button class="revert wl-prop-approve" data-pid="${htmlEscape(r.proposalId, false)}">✓ approve</button> ` +
              `<select class="wl-prop-why" aria-label="Reject reason">` +
              `<option value="">reject: why?</option>` +
              `<option value="incorrect">incorrect</option>` +
              `<option value="not_better">not better</option>` +
              `<option value="out_of_scope">out of scope</option>` +
              `<option value="other">other</option>` +
              `</select> ` +
              `<button class="revert wl-prop-reject" data-pid="${htmlEscape(r.proposalId, false)}">✗ reject</button>` +
              `</td></tr>`
            );
          })
          .join("") +
        `</tbody></table></div>`);

  const script = `<script>
document.addEventListener("click", async function (e) {
  var btn = e.target.closest ? e.target.closest(".wl-prop-approve, .wl-prop-reject") : null;
  if (!btn) return;
  var tr = btn.closest("tr");
  var body = { proposal_id: btn.dataset.pid };
  if (btn.classList.contains("wl-prop-approve")) {
    body.action = "approve_proposal";
    body.base_version = Number(tr.dataset.ver);
  } else {
    body.action = "reject_proposal";
    var why = tr.querySelector(".wl-prop-why");
    if (why && why.value) body.reject_reason = why.value;
  }
  tr.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
  try {
    var res = await fetch("/api/article/" + encodeURIComponent(tr.dataset.slug), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === 200) {
      tr.remove();
    } else if (res.status === 409) {
      alert("Article changed since this page loaded — reloading.");
      location.reload();
    } else {
      var j = await res.json().catch(function () { return {}; });
      alert("Failed: " + (j.error || res.status));
      tr.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
    }
  } catch (err) {
    alert("Network error — try again.");
    tr.querySelectorAll("button").forEach(function (b) { b.disabled = false; });
  }
});
</${"script"}>`;
  return shell("AI proposals", body, script);
}
