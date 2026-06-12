// Full Mathlib declaration-name index → sharded static JSON consumed by the
// editor's autocomplete (wiki/assets/editor.js) and, later, server-side
// hallucinated-decl validation (the ROADMAP's "one decl index artifact").
//
//   npm run build:decl-index            # fetches the source (network)
//   npm run build:decl-index -- <path>  # re-shard from a local copy (offline)
//
// SOURCE CHOICE (investigated 2026-06-12):
//   https://leanprover-community.github.io/mathlib4_docs/declarations/declaration-data.bmp
//   — doc-gen4's machine-readable index behind the official mathlib4 docs
//   search. Despite the .bmp extension (a doc-gen4 serving quirk; GitHub Pages
//   returns Content-Type image/bmp) the body is JSON:
//     { declarations: { [name]: { docLink, kind } }, instances, instancesFor, modules }
//   ~65 MB, ~411k declarations, rebuilt with every mathlib4_docs deploy; the
//   HTTP ETag identifies the build and is recorded in the manifest.
//   - declaration-data.json does NOT exist (404) — probed.
//   - Rejected alternative: scraping .lean files in the local mathlib4 checkout.
//     Extracting decl names from source is unreliable (namespaces, sections,
//     `variable` binders, macro-generated declarations) and the checkout drifts
//     from what the docs site actually publishes.
//   The module is derived from docLink ("./Mathlib/Data/Nat/Prime/Defs.html#Nat.Prime"
//   → "Mathlib.Data.Nat.Prime.Defs"). The index covers doc-gen4's full universe
//   (Mathlib + Lean core + Std/Batteries + deps) — every name is resolvable in a
//   Mathlib environment, which is exactly what the validation oracle needs.
//
// SHARD SCHEME (recorded in manifest.json under "scheme"):
//   Longest-prefix shards. Key characters: lowercased [a-z0-9]; anything else
//   → "_"; names shorter than the key length are padded with "_". Every shard
//   starts at a 2-char key; any shard whose serialized JSON exceeds
//   MAX_SHARD_BYTES splits to (len+1)-char keys, RECURSIVELY — a flat 3-char
//   split is not enough in practice ("cat…" = CategoryTheory alone is ~4.7 MB,
//   and "std"/"lea" carry Std.*/Lean.*). Leaf keys are prefix-free by
//   construction, so a full name resolves to exactly one shard: the unique
//   manifest key that prefixes its padded normalized form.
//   Each shard file is a JSON array of [decl, module] pairs sorted by decl
//   (code-unit order). manifest.json:
//     { built_at, source, source_sha_or_etag, total,
//       scheme: { kind:"prefix", min_len, max_len, max_bytes, pad:"_" },
//       shards: { [key]: count } }
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

export const SOURCE_URL =
  "https://leanprover-community.github.io/mathlib4_docs/declarations/declaration-data.bmp";
export const MAX_SHARD_BYTES = 400_000;
export const MIN_KEY_LEN = 2;
export const MAX_KEY_LEN = 64; // termination guard for pathological collisions
export const PAD = "_";

export type DeclPair = [decl: string, module: string];

export interface Manifest {
  built_at: string;
  source: string;
  source_sha_or_etag: string;
  total: number;
  scheme: {
    kind: "prefix";
    min_len: number;
    max_len: number; // longest key actually emitted (client loop bound)
    max_bytes: number;
    pad: string;
  };
  shards: Record<string, number>;
}

// "./Mathlib/Data/Nat/Prime/Defs.html#Nat.Prime" → "Mathlib.Data.Nat.Prime.Defs"
export function moduleFromDocLink(docLink: string): string {
  return docLink
    .replace(/^\.\//, "")
    .replace(/\.html(#.*)?$/, "")
    .split("/")
    .join(".");
}

export function shardKeyChar(c: string): string {
  const l = c.toLowerCase();
  return /[a-z0-9]/.test(l) ? l : PAD;
}

// First `len` characters of `name`, normalized; padded with PAD when short.
export function shardKey(name: string, len: number): string {
  let k = "";
  for (let i = 0; i < len; i++) k += i < name.length ? shardKeyChar(name[i]) : PAD;
  return k;
}

function shardBytes(pairs: DeclPair[]): number {
  return Buffer.byteLength(JSON.stringify(pairs), "utf8");
}

export interface ShardOptions {
  maxBytes?: number;
  minLen?: number;
  maxLen?: number;
}

// Group pairs into prefix-free shards: start at minLen-char keys; recursively
// split any shard over maxBytes onto (len+1)-char keys until it fits or the
// maxLen guard trips (two distinct names can normalize identically at every
// length — e.g. "Nat.add" / "Nat_Add" — so oversize is accepted at maxLen).
// Deterministic: each shard sorted by decl in code-unit order; the returned
// Map's keys are sorted too.
export function buildShards(pairs: DeclPair[], opts: ShardOptions = {}): Map<string, DeclPair[]> {
  const maxBytes = opts.maxBytes ?? MAX_SHARD_BYTES;
  const minLen = opts.minLen ?? MIN_KEY_LEN;
  const maxLen = opts.maxLen ?? MAX_KEY_LEN;
  const leaves = new Map<string, DeclPair[]>();
  const group = (items: DeclPair[], len: number): Map<string, DeclPair[]> => {
    const m = new Map<string, DeclPair[]>();
    for (const p of items) {
      const k = shardKey(p[0], len);
      const arr = m.get(k);
      if (arr) arr.push(p);
      else m.set(k, [p]);
    }
    return m;
  };
  const queue: Array<[len: number, items: DeclPair[]]> = [[minLen, pairs]];
  while (queue.length) {
    const [len, items] = queue.pop()!;
    for (const [key, arr] of group(items, len)) {
      if (len < maxLen && shardBytes(arr) > maxBytes) {
        queue.push([len + 1, arr]);
      } else {
        arr.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
        leaves.set(key, arr);
      }
    }
  }
  return new Map([...leaves.entries()].sort(([a], [b]) => (a < b ? -1 : 1)));
}

export function buildManifest(
  shards: Map<string, DeclPair[]>,
  meta: { source: string; source_sha_or_etag: string; built_at?: string; maxBytes?: number; minLen?: number },
): Manifest {
  let total = 0;
  let maxLen = meta.minLen ?? MIN_KEY_LEN;
  const counts: Record<string, number> = {};
  for (const [key, arr] of shards) {
    counts[key] = arr.length;
    total += arr.length;
    if (key.length > maxLen) maxLen = key.length;
  }
  return {
    built_at: meta.built_at ?? new Date().toISOString(),
    source: meta.source,
    source_sha_or_etag: meta.source_sha_or_etag,
    total,
    scheme: {
      kind: "prefix",
      min_len: meta.minLen ?? MIN_KEY_LEN,
      max_len: maxLen,
      max_bytes: meta.maxBytes ?? MAX_SHARD_BYTES,
      pad: PAD,
    },
    shards: counts,
  };
}

// Decls every build must contain — a failed spot-check means the source moved
// or the parse broke, and a wrong index is worse than no index.
export const SPOT_CHECKS = ["Ideal.IsPrime", "AddCommGroup", "Nat.Prime", "Subgroup.Normal"];

interface DeclarationData {
  declarations: Record<string, { docLink: string; kind?: string }>;
}

export function pairsFromDeclarationData(data: DeclarationData): DeclPair[] {
  return Object.entries(data.declarations).map(
    ([name, info]): DeclPair => [name, moduleFromDocLink(info.docLink)],
  );
}

async function loadSource(localPath?: string): Promise<{ raw: string; etag: string; source: string }> {
  if (localPath) {
    return { raw: readFileSync(localPath, "utf8"), etag: "local:" + localPath, source: SOURCE_URL };
  }
  const res = await fetch(SOURCE_URL);
  if (!res.ok) throw new Error(`fetch ${SOURCE_URL} → ${res.status}`);
  return { raw: await res.text(), etag: res.headers.get("etag") ?? "unknown", source: SOURCE_URL };
}

async function main() {
  const localPath = process.argv[2];
  const outDir = resolve(process.cwd(), "public", "assets", "decl-index");

  console.log(localPath ? `reading ${localPath}` : `fetching ${SOURCE_URL} (~65 MB)`);
  const { raw, etag, source } = await loadSource(localPath);
  const data = JSON.parse(raw) as DeclarationData;
  if (!data.declarations || typeof data.declarations !== "object") {
    throw new Error("source shape changed: no top-level `declarations` object");
  }
  const pairs = pairsFromDeclarationData(data);

  const missing = SPOT_CHECKS.filter((d) => !(d in data.declarations));
  if (missing.length) throw new Error(`spot-check decls missing from source: ${missing.join(", ")}`);

  const shards = buildShards(pairs);
  const manifest = buildManifest(shards, { source, source_sha_or_etag: etag });

  // Rebuild the directory from scratch so renamed/split shards never leave
  // stale files behind. Scoped strictly to decl-index/.
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  let largest = { key: "", bytes: 0 };
  let totalBytes = 0;
  for (const [key, arr] of shards) {
    const json = JSON.stringify(arr);
    const bytes = Buffer.byteLength(json, "utf8");
    totalBytes += bytes;
    if (bytes > largest.bytes) largest = { key, bytes };
    writeFileSync(resolve(outDir, `${key}.json`), json);
  }
  writeFileSync(resolve(outDir, "manifest.json"), JSON.stringify(manifest));

  console.log(`wrote ${outDir}`);
  console.log(`  source etag:    ${etag}`);
  console.log(`  total decls:    ${manifest.total}`);
  console.log(`  shards:         ${shards.size} (+ manifest.json), ${(totalBytes / 1e6).toFixed(1)} MB total`);
  console.log(`  largest shard:  ${largest.key}.json — ${largest.bytes} bytes`);
  console.log(`  max key length: ${manifest.scheme.max_len}`);
  for (const d of SPOT_CHECKS) {
    console.log(`  spot-check ✓    ${d} → ${moduleFromDocLink(data.declarations[d].docLink)}`);
  }
  if (largest.bytes > (manifest.scheme.max_bytes ?? MAX_SHARD_BYTES)) {
    console.warn(`  WARNING: largest shard exceeds ${MAX_SHARD_BYTES} bytes (maxLen guard tripped)`);
  }
}

const isCli = import.meta.url === `file://${process.argv[1]}`;
if (isCli) {
  main().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
