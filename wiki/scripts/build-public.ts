// Populates wiki/public/ (the Worker's static-asset dir) from the existing
// static-site build: shared CSS/JS and the shell pages (index/concepts/about/
// 404/sitemap/robots). Article pages are served dynamically by the Worker.
import { mkdirSync, copyFileSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { buildMathlibIndex } from "./build-mathlib-index.ts";

const wiki = process.cwd();
const site = resolve(wiki, "..", "site");
const pub = resolve(wiki, "public");
const pubAssets = resolve(pub, "assets");

mkdirSync(pubAssets, { recursive: true });

// Shared article assets + editor styles come from the static site; the live
// editor logic is wiki-specific.
const fromSite = ["style.css", "script.js", "review.css"];
for (const f of fromSite) {
  const src = resolve(site, "assets", f);
  if (existsSync(src)) copyFileSync(src, resolve(pubAssets, f));
}
const fromWiki = ["editor.js"];
for (const f of fromWiki) {
  const src = resolve(wiki, "assets", f);
  if (existsSync(src)) copyFileSync(src, resolve(pubAssets, f));
}

// index.html + sitemap.xml are served dynamically from D1 as of Wave D
// (src/home.ts via GET / and GET /sitemap.xml) — copying them here would let
// the asset layer shadow the Worker routes; the lead deletes the stale
// copies already in wiki/public/.
const shellFiles = ["concepts.html", "about.html", "404.html", "robots.txt", "wikilean.ttl"];
for (const f of shellFiles) {
  const src = resolve(site, "out", f);
  if (existsSync(src)) copyFileSync(src, resolve(pub, f));
}

const n = buildMathlibIndex(site, resolve(pubAssets, "mathlib-index.json"));
console.log(`built ${pub} (mathlib index: ${n} decls)`);
