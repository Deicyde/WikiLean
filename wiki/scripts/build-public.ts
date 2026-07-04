// Populates wiki/public/ (the Worker's static-asset dir) from the existing
// static-site build: shared CSS/JS and the shell pages (index/concepts/about/
// 404/sitemap/robots). Article pages are served dynamically by the Worker.
import { mkdirSync, copyFileSync, cpSync, existsSync, rmSync } from "node:fs";
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
const shellFiles = [
  "concepts.html", "about.html", "404.html", "robots.txt", "wikilean.ttl",
  // The unified map page (reserved route /map) + its KV-first data artifact.
  "map.html", "map_data.json",
  // The brain explorer (reserved route /brain, site/build_brain_page.py); its
  // data ships as the prefix shards in assets/brain/ (copied below).
  "brain.html",
  // Data blobs kept for the fallback path + the /api/atlas agent surface (the
  // old /graph and /atlas *pages* now 301 → /map; see src/index.ts).
  "graph_data.json", "atlas_data.json",
  "article-graph.html", "article-graph-data.json",
];
for (const f of shellFiles) {
  const src = resolve(site, "out", f);
  if (existsSync(src)) copyFileSync(src, resolve(pub, f));
}

// BRAIN neighborhood shards (brain/build_shards.py → site/assets/brain/):
// wipe-then-copy so renamed shard keys never leave stale files behind, the
// same discipline as build-decl-index.ts. Scoped strictly to assets/brain/.
const brainSrc = resolve(site, "assets", "brain");
const brainDst = resolve(pubAssets, "brain");
if (existsSync(brainSrc)) {
  rmSync(brainDst, { recursive: true, force: true });
  cpSync(brainSrc, brainDst, { recursive: true });
}

// Retired page assets: /graph + /atlas are now redirect routes in the Worker.
// public/ is generated-but-not-wiped, so a stale graph.html/atlas.html would
// still be bundled by `npm run deploy` and SHADOW the redirect. Remove them.
for (const f of ["graph.html", "atlas.html"]) {
  const p = resolve(pub, f);
  if (existsSync(p)) rmSync(p);
}

const n = buildMathlibIndex(site, resolve(pubAssets, "mathlib-index.json"));
console.log(`built ${pub} (mathlib index: ${n} decls)`);
