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
      `<script src="/assets/editor.js?v=6"></script>\n`;
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

const SHELL_CSS = `
*{box-sizing:border-box}
body{margin:0;background:#fafbfc;color:#1f2328;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wl-header{background:#fff;border-bottom:1px solid #d0d7de;padding:14px 28px;display:flex;align-items:center;justify-content:space-between}
.wl-brand{font-weight:700;color:#0969da;font-size:18px;text-decoration:none}
.wl-navlink{color:#0969da;text-decoration:none;font-size:.9rem}
.wrap{max-width:900px;margin:0 auto;padding:28px 28px 64px}
h1{font-size:1.5rem;margin:0 0 .3rem}
.lead{color:#57606a;margin:0 0 18px}
table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d0d7de;border-radius:8px;overflow:hidden}
th,td{border-bottom:1px solid #eaeef2;padding:8px 12px;text-align:left;font-size:.9rem;vertical-align:top}
th{background:#f6f8fa;text-transform:uppercase;letter-spacing:.04em;font-size:.72rem;color:#57606a}
tr:last-child td{border-bottom:none}
a{color:#0969da;text-decoration:none}
a:hover{text-decoration:underline}
.muted{color:#8c959f}
button.revert{font:inherit;font-size:.8rem;padding:3px 9px;border:1px solid #d0d7de;border-radius:6px;background:#f6f8fa;cursor:pointer}
button.revert:hover{background:#eaeef2}
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
<header class="wl-header"><a class="wl-brand" href="/">WikiLean</a><span><a class="wl-navlink" href="/recent-changes">Recent changes</a> · <a class="wl-navlink" href="/about">About</a></span></header>
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
      .map((r) => {
        const who = r.userId ? htmlEscape(r.userName ?? r.userId, false) : '<span class="muted">system</span>';
        const revertBtn = canEdit
          ? `<button class="revert" data-rev="${r.id}">revert to this</button>`
          : "";
        return (
          `<tr><td>${fmtDate(r.createdAt)}</td><td>${who}</td><td>${r.count}</td>` +
          `<td>${r.comment ? htmlEscape(r.comment, false) : '<span class="muted">—</span>'}</td><td>${revertBtn}</td></tr>`
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
