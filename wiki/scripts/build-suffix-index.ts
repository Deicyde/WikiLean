// Suffix (final-segment) index over the FULL doc-gen4 decl universe → sharded
// static JSON, the namespace-resolution half of the decl_exists upgrade: a miss
// like `meromorphicOrderAt_nonneg_iff_analyticAt` resolves to its owning
// namespace(s) by final segment instead of the old 19,611-organ linear scan.
//
//   npm run build:suffix-index          # reads public/assets/decl-index (no network)
//
// SOURCE: the freshly built decl-index shards in public/assets/decl-index/ —
// the exact same doc-gen4 snapshot by construction (run build:decl-index
// first); the decl-index manifest's etag is recorded here as the pin.
//
// SHAPE (recorded in manifest.json under "format"):
//   Bucket key = the name's FINAL "."-segment, normalized per UTF-16 code unit
//   with the shard-key rule (lowercase [a-z0-9], anything else "_", no
//   padding) — so distinct segments that normalize identically (Ne/ne) share a
//   bucket and the consumer disambiguates on the stored exact names. Bucket
//   value = the bare entries array [[decl, module], ...] sorted by decl
//   (code-unit order) — or, when the bucket exceeds BUCKET_CAP=64 stored
//   ("mk" is ~6k decls), { total_count, entries } with total_count uncapped;
//   both shapes parse through the Worker's parseSuffixBucket. Shard files are
//   JSON objects { [bucketKey]: bucket } with sorted keys, split with the SAME
//   longest-prefix scheme as the decl index — the Worker resolves a suffix
//   with the existing declShardFor (its code-unit normalization of the raw
//   suffix equals our bucket key by construction).
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  buildKeyedShards,
  MAX_SHARD_BYTES,
  MIN_KEY_LEN,
  PAD,
  shardKeyChar,
  type DeclPair,
  type Manifest,
} from "./build-decl-index.ts";
// The bucket contract (final-segment key + the two bucket shapes) lives in the
// SHARED module wiki/src/decl.ts — the Worker's parseSuffixBucket/suggestRename
// and this builder both import it, so the two sides cannot drift.
import { bucketEntries, bucketTotal, finalSegment, type SuffixBucket } from "../src/decl.ts";

export const BUCKET_CAP = 64;

// Uncapped buckets ship as the bare entries array (total = length); only the
// ~200 over-cap buckets pay the { total_count, entries } wrapper.
export type Bucket = SuffixBucket;
export { bucketEntries, bucketTotal, finalSegment };

// Charwise normalization of a whole segment — shardKey without padding. Iterates
// UTF-16 code UNITS (never code points) to mirror the Worker's suffixBucketKey
// (= declShardKey over the segment): an astral char like 𝕜 is TWO units → "__".
export function normSegment(seg: string): string {
  let k = "";
  for (let i = 0; i < seg.length; i++) k += shardKeyChar(seg[i]);
  return k;
}

// name → bucket-key groups, entries sorted, capped at BUCKET_CAP + total_count.
export function buildBuckets(pairs: DeclPair[]): Map<string, Bucket> {
  const groups = new Map<string, DeclPair[]>();
  for (const p of pairs) {
    const key = normSegment(finalSegment(p[0]));
    if (!key) continue; // pathological all-empty segment; never a real decl
    const arr = groups.get(key);
    if (arr) arr.push(p);
    else groups.set(key, [p]);
  }
  const buckets = new Map<string, Bucket>();
  for (const [key, arr] of groups) {
    arr.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0));
    buckets.set(
      key,
      arr.length > BUCKET_CAP ? { total_count: arr.length, entries: arr.slice(0, BUCKET_CAP) } : arr,
    );
  }
  return buckets;
}

interface SuffixManifest extends Manifest {
  decl_index_built_at: string;
  total_decls: number;
  bucket_cap: number;
  format: string;
}

// Buckets every build must contain — same posture as the decl-index SPOT_CHECKS:
// a failed check means the source or the grouping broke, and a wrong index is
// worse than no index. [rawSuffix, mustContainDecl, minTotal]
const SPOT_CHECKS: Array<[suffix: string, decl: string, minTotal: number]> = [
  ["IntegrableOn", "MeasureTheory.IntegrableOn", 1],
  ["mk", "Prod.mk", 6000],
  ["Prime", "Nat.Prime", 2],
];

function loadDeclIndex(dir: string): { pairs: DeclPair[]; manifest: Manifest } {
  const manifest = JSON.parse(readFileSync(resolve(dir, "manifest.json"), "utf8")) as Manifest;
  const pairs: DeclPair[] = [];
  for (const key of Object.keys(manifest.shards)) {
    for (const p of JSON.parse(readFileSync(resolve(dir, `${key}.json`), "utf8")) as DeclPair[]) {
      pairs.push(p);
    }
  }
  if (pairs.length !== manifest.total) {
    throw new Error(`decl-index inconsistent: shards carry ${pairs.length} pairs, manifest.total=${manifest.total}`);
  }
  return { pairs, manifest };
}

function main() {
  const declDir = resolve(process.cwd(), "public", "assets", "decl-index");
  const outDir = resolve(process.cwd(), "public", "assets", "suffix-index");

  console.log(`reading ${declDir}`);
  const { pairs, manifest: declManifest } = loadDeclIndex(declDir);
  const buckets = buildBuckets(pairs);

  for (const [suffix, decl, minTotal] of SPOT_CHECKS) {
    const b = buckets.get(normSegment(suffix));
    if (!b) throw new Error(`spot-check bucket missing: ${suffix}`);
    const total = bucketTotal(b);
    const entries = bucketEntries(b);
    if (total < minTotal) throw new Error(`spot-check ${suffix}: total ${total} < ${minTotal}`);
    if (total > BUCKET_CAP && entries.length !== BUCKET_CAP) {
      throw new Error(`spot-check ${suffix}: over-cap bucket stores ${entries.length} ≠ ${BUCKET_CAP}`);
    }
    if (minTotal <= BUCKET_CAP && !entries.some((e) => e[0] === decl)) {
      throw new Error(`spot-check ${suffix}: ${decl} not in stored entries`);
    }
  }

  const shards = buildKeyedShards([...buckets.entries()]);
  let total = 0;
  let maxLen = MIN_KEY_LEN;
  const counts: Record<string, number> = {};
  for (const [key, arr] of shards) {
    counts[key] = arr.length;
    total += arr.length;
    if (key.length > maxLen) maxLen = key.length;
  }
  const manifest: SuffixManifest = {
    built_at: new Date().toISOString(),
    source: "public/assets/decl-index (doc-gen4 declaration-data; run build:decl-index first)",
    source_sha_or_etag: declManifest.source_sha_or_etag,
    decl_index_built_at: declManifest.built_at,
    total,
    total_decls: pairs.length,
    bucket_cap: BUCKET_CAP,
    format:
      "shard = { [key]: bucket }; key = final '.'-segment normalized per UTF-16 code unit " +
      "(lowercase [a-z0-9], else '_'); bucket = [[decl, module], ...] sorted by decl, or " +
      `{ total_count, entries } when over ${BUCKET_CAP} stored (total_count uncapped)`,
    scheme: { kind: "prefix", min_len: MIN_KEY_LEN, max_len: maxLen, max_bytes: MAX_SHARD_BYTES, pad: PAD },
    shards: counts,
  };

  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  let largest = { key: "", bytes: 0 };
  let totalBytes = 0;
  for (const [key, arr] of shards) {
    const json = JSON.stringify(Object.fromEntries(arr));
    const bytes = Buffer.byteLength(json, "utf8");
    totalBytes += bytes;
    if (bytes > largest.bytes) largest = { key, bytes };
    writeFileSync(resolve(outDir, `${key}.json`), json);
  }
  writeFileSync(resolve(outDir, "manifest.json"), JSON.stringify(manifest));

  const mk = buckets.get("mk")!;
  console.log(`wrote ${outDir}`);
  console.log(`  decl-index pin: ${manifest.source_sha_or_etag} (built ${manifest.decl_index_built_at})`);
  console.log(`  decls in:       ${pairs.length}`);
  console.log(`  buckets:        ${total} (${[...buckets.values()].filter((b) => bucketTotal(b) === 1).length} unique, ${[...buckets.values()].filter((b) => !Array.isArray(b)).length} over-cap)`);
  console.log(`  shards:         ${shards.size} (+ manifest.json), ${(totalBytes / 1e6).toFixed(1)} MB total`);
  console.log(`  largest shard:  ${largest.key}.json — ${largest.bytes} bytes`);
  console.log(`  max key length: ${maxLen}`);
  console.log(`  spot-check ✓    IntegrableOn bucket has MeasureTheory.IntegrableOn`);
  console.log(`  spot-check ✓    mk bucket: total ${bucketTotal(mk)}, stored ${bucketEntries(mk).length}`);
  if (largest.bytes > MAX_SHARD_BYTES) {
    console.warn(`  WARNING: largest shard exceeds ${MAX_SHARD_BYTES} bytes (maxLen guard tripped)`);
  }
}

const isCli = import.meta.url === `file://${process.argv[1]}`;
if (isCli) {
  try {
    main();
  } catch (e) {
    console.error(e);
    process.exit(1);
  }
}
