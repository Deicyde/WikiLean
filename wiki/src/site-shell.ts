// The one site header, used by every page the Worker renders: the home page,
// /about and /contribute (home.ts), the formalization reader and its article
// pages (articles-render.ts), the chrome pages (pages.ts), /agents (agents.ts)
// and the old annotated article pages (engine/page.ts).
//
// Before the restyle of 2026-11-19 there were six independent definitions of
// .wl-header with different markup and three different palettes, so "the home
// page and the articles plane have different headers" was a missing component,
// not a styling bug. This is that component; its CSS is the shared layer in
// site/assets/style.css, which every one of those pages now links.
//
// One row only. A page that needs a title, a breadcrumb or a view switch puts
// them in the context strip (contextStrip) under the header, never in a second
// header row.

import { htmlEscape } from "./engine/html.js";
import { siteNav } from "./site-nav.js";

const attr = (s: string) => htmlEscape(String(s ?? ""), true);
const text = (s: string) => htmlEscape(String(s ?? ""), false);

// The no-FOUC theme bootstrap: set [data-theme] before the stylesheet parses,
// so dark mode applies on first paint. Priority: localStorage, then the OS
// preference, then light.
//
// IMPORTANT: on the articles plane this exact script text is pinned by sha256
// in security.ts ARTICLES_INLINE_SCRIPT_HASHES.theme, and the enforced policy
// blocks it the moment a byte changes. Keep the two in step (test/articles.ts
// recomputes the hash from the served page, so CI catches a drift).
export const THEME_BOOTSTRAP =
  `<script>(function(){try{var s=localStorage.getItem("wl-theme");var t=s==="dark"||s==="light"?s:` +
  `(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");` +
  `document.documentElement.dataset.theme=t;}catch(e){}})();</script>`;

export const THEME_TOGGLE_BUTTON =
  `<button id="wl-theme-toggle" class="wl-theme-toggle" type="button" aria-label="Toggle dark mode" title="Toggle dark mode">🌓</button>`;

// The toggle's behaviour, for a page that is not already loading a script that
// does it. `nonce` is for /agents, whose policy runs inline scripts by nonce.
export function themeToggleScript(nonce = ""): string {
  const attrs = nonce ? ` nonce="${attr(nonce)}"` : "";
  return `<script${attrs}>(function(){var b=document.getElementById("wl-theme-toggle");if(!b)return;` +
    `b.addEventListener("click",function(){var r=document.documentElement;` +
    `var n=r.dataset.theme==="dark"?"light":"dark";r.dataset.theme=n;` +
    `try{localStorage.setItem("wl-theme",n);}catch(e){}});})();</script>`;
}

export interface SiteHeaderOptions {
  // The nav path to mark aria-current="page", "" for none.
  current?: string;
  // What sits beside the brand after a "·": the page's own name. `href` makes
  // it a link, `cls` adds a class (the reader marks its own with
  // "articles-home", which its stylesheet hides on a phone).
  context?: { label: string; href?: string; cls?: string };
  // The header search box's markup, where the page carries one.
  search?: string;
  // Extra links after the shared nav and before the toggle, already escaped
  // markup (the annotated article page's History and "view on Wikipedia").
  extraNav?: string;
  // A page that renders no theme toggle at all (none today; kept so a page
  // under a policy that cannot run the toggle has a way out).
  toggle?: boolean;
}

// The header markup, opening <header> through its closing tag. A page that
// needs a row of its own controls under the title (the annotated article
// page's badges and filters) passes it as `controls`; nothing else may.
export function siteHeader(opts: SiteHeaderOptions = {}, controls = ""): string {
  const ctx = opts.context
    ? `\n    <span class="wl-sep" aria-hidden="true">·</span>\n    ` +
      (opts.context.href
        ? `<a class="wl-article${opts.context.cls ? ` ${attr(opts.context.cls)}` : ""}" href="${attr(opts.context.href)}">${text(opts.context.label)}</a>`
        : `<span class="wl-article${opts.context.cls ? ` ${attr(opts.context.cls)}` : ""}">${opts.context.label}</span>`)
    : "";
  return `<header class="wl-header">
  <div class="wl-title">
    <a class="wl-brand" href="/">WikiLean</a>${ctx}
    <nav class="wl-nav" aria-label="Site">
      ${opts.search ?? ""}
      ${siteNav(opts.current ?? "")}
      ${opts.extraNav ?? ""}
      ${opts.toggle === false ? "" : THEME_TOGGLE_BUTTON}
    </nav>
  </div>${controls ? `\n${controls}` : ""}
</header>`;
}

// The context strip: the page's own breadcrumb, title and controls, on the
// header's surface and under its border. This is what replaces a second header
// row — a page has at most one of these and never two stacked bars.
export function contextStrip(opts: { crumb?: string; title?: string; right?: string }): string {
  const parts = [
    opts.crumb ? `<span class="wl-ctx-crumb">${opts.crumb}</span>` : "",
    opts.title ? `<span class="wl-ctx-h">${opts.title}</span>` : "",
    opts.right ? `<span class="wl-ctx-right">${opts.right}</span>` : "",
  ].filter(Boolean);
  return `<div class="wl-ctx"><div class="wl-ctx-in">${parts.join("")}</div></div>`;
}
