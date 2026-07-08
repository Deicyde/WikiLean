// Populates wiki/public/ (the Worker's static-asset dir) from the existing
// static-site build: shared CSS/JS and the shell pages (index/concepts/about/
// 404/sitemap/robots). Article pages are served dynamically by the Worker.
import { execFileSync } from "node:child_process";
import { mkdirSync, copyFileSync, cpSync, existsSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import { buildMathlibIndex } from "./build-mathlib-index.ts";

const wiki = process.cwd();
const site = resolve(wiki, "..", "site");
const root = resolve(wiki, "..");
const pub = resolve(wiki, "public");
const pubAssets = resolve(pub, "assets");

mkdirSync(pubAssets, { recursive: true });

function runFromRoot(label: string, args: string[]) {
  console.log(`build-public: ${label}`);
  execFileSync(args[0], args.slice(1), { cwd: root, stdio: "inherit" });
}

// The Brain page and its shards are generated artifacts, not committed assets.
// Clean deploy worktrees must either rebuild them or be seeded with the generated
// site/assets/brain directory, otherwise /brain and /assets/brain/* disappear.
runFromRoot("building Brain page", ["python3", "site/build_brain_page.py"]);
const brainShardInput = resolve(root, "brain", "data", "rollup_edges.tree.jsonl");
const brainSrc = resolve(site, "assets", "brain");
const brainSrcManifest = resolve(brainSrc, "manifest.json");
if (existsSync(brainShardInput)) {
  runFromRoot("building Brain shards", ["python3", "brain/build_shards.py"]);
} else if (!existsSync(brainSrcManifest)) {
  throw new Error(
    "missing generated Brain shards: run python3 brain/build_shards.py, or seed site/assets/brain before build-public",
  );
} else {
  console.log("build-public: using existing generated Brain shards");
}

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
  // The brain explorer (reserved route /brain, site/build_brain_page.py); its
  // data ships as the prefix shards in assets/brain/ (copied below).
  "brain.html",
  // Data blobs kept for the KV-fallback path + the /api/atlas agent surface (the
  // /map, /graph and /atlas *pages* now 301 → /brain; see src/index.ts).
  "graph_data.json", "atlas_data.json",
  "article-graph.html", "article-graph-data.json",
];
for (const f of shellFiles) {
  const src = resolve(site, "out", f);
  if (existsSync(src)) copyFileSync(src, resolve(pub, f));
}
const brainPage = resolve(pub, "brain.html");
if (!existsSync(brainPage)) {
  throw new Error("missing public/brain.html after build; site/build_brain_page.py did not produce site/out/brain.html");
}

// BRAIN neighborhood shards (brain/build_shards.py → site/assets/brain/):
// wipe-then-copy so renamed shard keys never leave stale files behind, the
// same discipline as build-decl-index.ts. Scoped strictly to assets/brain/.
const brainDst = resolve(pubAssets, "brain");
if (existsSync(brainSrc)) {
  rmSync(brainDst, { recursive: true, force: true });
  cpSync(brainSrc, brainDst, { recursive: true });
}
const brainManifest = resolve(brainDst, "manifest.json");
if (!existsSync(brainManifest)) {
  throw new Error("missing public/assets/brain/manifest.json after build; brain/build_shards.py did not produce site/assets/brain/");
}

// Retired page assets: /map, /graph, /atlas are now redirect routes in the
// Worker. public/ is generated-but-not-wiped, so a stale map.html/graph.html/
// atlas.html would still be bundled by `npm run deploy` and SHADOW the redirect.
// map_data.json is likewise no longer served. Remove them.
for (const f of ["map.html", "map_data.json", "graph.html", "atlas.html"]) {
  const p = resolve(pub, f);
  if (existsSync(p)) rmSync(p);
}

const n = buildMathlibIndex(site, resolve(pubAssets, "mathlib-index.json"));
console.log(`built ${pub} (mathlib index: ${n} decls)`);
