// Populates wiki/public/ (the Worker's static-asset dir) from the existing
// static-site build: shared CSS/JS and the shell pages (index/concepts/about/
// 404/sitemap/robots). Article pages are served dynamically by the Worker.
import { mkdirSync, copyFileSync, cpSync, existsSync, rmSync, lstatSync } from "node:fs";
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

// index.html + sitemap.xml + about.html are served dynamically from D1 (src/
// home.ts via GET /, GET /sitemap.xml, GET /about) — copying them here would
// let the asset layer shadow the Worker routes; the lead deletes the stale
// copies already in wiki/public/.
const shellFiles = [
  "concepts.html", "404.html", "robots.txt", "wikilean.ttl",
  // The brain explorer (reserved route /brain, site/build_brain_page.py); its
  // data ships as the prefix shards in assets/brain/ (copied below).
  "brain.html",
  // (graph_data.json / atlas_data.json / article-graph.* retired 2026-07-10 —
  // the Brain supersedes the whole old graph stack; routes 301/410 in src/index.ts.)
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
  // Skip symlinks: a stray self-referencing link here (seen 2026-08-03:
  // assets/brain/brain -> assets/brain) would otherwise be copied verbatim
  // and wrangler's asset walk dies on it with ELOOP at deploy time.
  cpSync(brainSrc, brainDst, {
    recursive: true,
    filter: (src) => !lstatSync(src).isSymbolicLink(),
  });
}

// Retired page assets: /map, /graph, /atlas are now redirect routes in the
// Worker, and /about is a dynamic Worker route (live counts from D1).
// public/ is generated-but-not-wiped, so a stale map.html/graph.html/
// atlas.html/about.html would still be bundled by `npm run deploy` and SHADOW
// the route. map_data.json is likewise no longer served. Remove them.
for (const f of ["map.html", "map_data.json", "map_data_v2.json", "graph.html",
                 "atlas.html", "about.html"]) {
  const p = resolve(pub, f);
  if (existsSync(p)) rmSync(p);
}

const n = buildMathlibIndex(site, resolve(pubAssets, "mathlib-index.json"));
console.log(`built ${pub} (mathlib index: ${n} decls)`);
