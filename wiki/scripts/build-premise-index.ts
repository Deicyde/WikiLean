// Per-declaration premise lists (decl → the explicit premises its proof uses)
// → sharded static JSON backing the brain_premises tool: seeds resolve via the
// decl oracle, each seed's stored list is fetched here, and the union is
// ranked by (multiplicity across seeds, stored rank).
//
//   npm run build:premise-index         # reads local caches only (no network)
//
// SOURCE: catalog/.cache/mathnetwork/edges.csv — the released dataset of "The
// Network Structure of Mathlib" (MathNetwork/MathlibGraph, arXiv:2604.24797,
// Apache-2.0; fetched by catalog/fetch_mathlib_graph.py). Columns
// source,target,is_explicit,is_simplifier; decl NAMES directly (no UUID join);
// row order within a source = proof order. Only is_explicit=True rows are
// premises (source-visible, non-elaborator-synthesized).
// JOIN: catalog/.cache/declaration-data.json — the raw doc-gen4 body cached by
// `DECL_DATA_CACHE=... npm run build:decl-index` — gates every stored name on
// the decl-index oracle snapshot (a premise the Worker cannot resolve is dead
// bytes) and provides `kind` for the theorem-only reduction.
//
// FILTERS (all recorded in manifest.json):
//   is_explicit=True · self-loops dropped · top-0.1%-in-degree hub targets
//   dropped (Eq.mpr-class plumbing; full list in the manifest) · source and
//   target must exist in the decl-index snapshot · per-source top-K=12 targets
//   in file order, deduped · if the emitted bytes exceed BUDGET_BYTES (~25 MB),
//   sources reduce to theorem-kind decls (kinds recorded).
//
// SHAPE (recorded in manifest.json under "format"):
//   Shard files are JSON objects { [source_decl]: [int, ...] } (sorted keys),
//   split with the SAME longest-prefix scheme as the decl index — the Worker
//   resolves a source name with the existing declShardFor. Each int i indexes
//   the fixed-chunk name tables names/<floor(i/8192)>.json (JSON arrays of
//   8192 names each; last chunk short), name table sorted lexicographically.
import { createReadStream, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { createInterface } from "node:readline";
import { spawn } from "node:child_process";
import {
  buildKeyedShards,
  MAX_SHARD_BYTES,
  MIN_KEY_LEN,
  PAD,
  type Manifest,
} from "./build-decl-index.ts";

export const TOP_K = 12;
export const HUB_DROP_FRAC = 0.001;
export const CHUNK_SIZE = 8192;
export const BUDGET_BYTES = 25_000_000;
export const THEOREM_KINDS = new Set(["theorem"]);

const EDGES_CSV = "../catalog/.cache/mathnetwork/edges.csv";
const DECL_DATA = "../catalog/.cache/declaration-data.json";
const HEADER = "source,target,is_explicit,is_simplifier";
const HF_DATASET = "MathNetwork/MathlibGraph";

interface HfSourceMetadata {
  dataset: string;
  revision: string;
  file_url: string;
  sha256: string;
  size: number;
}

interface HfGuard {
  metadata: HfSourceMetadata;
  release: () => Promise<void>;
}

async function acquireHfGuard(edgesPath: string): Promise<HfGuard> {
  const guardScript = resolve(process.cwd(), "../catalog/verify_huggingface_cache.py");
  const child = spawn(
    "python3",
    [
      guardScript,
      "--dataset",
      HF_DATASET,
      "--file",
      `edges.csv=${edgesPath}`,
      "--hold",
    ],
    { stdio: ["pipe", "pipe", "inherit"] },
  );
  child.stdout.setEncoding("utf8");
  const exited = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>(
    (resolveExit, rejectExit) => {
      child.once("error", rejectExit);
      child.once("exit", (code, signal) => resolveExit({ code, signal }));
    },
  );
  let metadata: HfSourceMetadata;
  try {
    metadata = await new Promise<HfSourceMetadata>((resolveReady, rejectReady) => {
      let buffer = "";
      let settled = false;
      const fail = (error: Error) => {
        if (!settled) {
          settled = true;
          rejectReady(error);
        }
      };
      exited.then(
        ({ code, signal }) => {
          fail(new Error(`Hugging Face cache guard exited before ready: code=${code} signal=${signal}`));
        },
        (error) => fail(error instanceof Error ? error : new Error(String(error))),
      );
      child.stdout.on("data", (chunk: string) => {
        if (settled) return;
        buffer += chunk;
        const newline = buffer.indexOf("\n");
        if (newline < 0) return;
        try {
          const value = JSON.parse(buffer.slice(0, newline)) as {
            dataset?: string;
            revision?: string;
            files?: Record<string, HfSourceMetadata>;
          };
          const source = value.files?.["edges.csv"];
          if (value.dataset !== HF_DATASET || !source || value.revision !== source.revision) {
            throw new Error("Hugging Face cache guard returned inconsistent metadata");
          }
          settled = true;
          resolveReady(source);
        } catch (error) {
          fail(error instanceof Error ? error : new Error(String(error)));
        }
      });
    });
  } catch (error) {
    child.stdin.end();
    child.kill();
    await exited.catch(() => undefined);
    throw error;
  }
  let released = false;
  return {
    metadata,
    release: async () => {
      if (released) return;
      released = true;
      child.stdin.end();
      const { code, signal } = await exited;
      if (code !== 0) {
        throw new Error(`Hugging Face cache guard failed: code=${code} signal=${signal}`);
      }
    },
  };
}

// Minimal CSV field split: fast path for the ~all unquoted lines; quoted
// fields (notation decls with commas inside «», e.g. MeasureTheory's ⨍ terms)
// take the stateful path with "" escapes.
export function splitCsv(line: string): string[] {
  if (!line.includes('"')) return line.split(",");
  const out: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (quoted) {
      if (ch === '"') {
        if (line[i + 1] === '"') { field += '"'; i++; }
        else quoted = false;
      } else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { out.push(field); field = ""; }
    else field += ch;
  }
  out.push(field);
  return out;
}

async function eachEdge(
  path: string,
  fn: (source: string, target: string, explicit: boolean) => void,
): Promise<number> {
  let rows = 0;
  let first = true;
  const rl = createInterface({
    input: createReadStream(path, { highWaterMark: 4 << 20 }),
    crlfDelay: Infinity,
  });
  for await (let line of rl) {
    if (line.endsWith("\r")) line = line.slice(0, -1);
    if (first) {
      first = false;
      if (line !== HEADER) throw new Error(`edges.csv header changed: expected "${HEADER}", got "${line}"`);
      continue;
    }
    if (!line) continue;
    const f = splitCsv(line);
    if (f.length !== 4) throw new Error(`edges.csv row ${rows + 2}: ${f.length} fields — "${line.slice(0, 120)}"`);
    if (f[2] !== "True" && f[2] !== "False") {
      throw new Error(`edges.csv row ${rows + 2}: is_explicit "${f[2]}" not True/False`);
    }
    rows++;
    fn(f[0], f[1], f[2] === "True");
  }
  return rows;
}

interface PremiseManifest extends Manifest {
  source_dataset: string;
  source_license: string;
  source_paper: string;
  pin: {
    dataset_revision: string;
    edges_sha256: string;
    edges_bytes: number;
    edges_url: string;
    decl_index_etag: string;
  };
  filters: {
    is_explicit: true;
    drop_self_loops: true;
    top_k: number;
    hub_drop_top_frac: number;
    hub_drop_count: number;
    oracle_gate: string;
    theorem_like_only: boolean;
    theorem_like_kinds: string[];
  };
  hub_drop: Array<[name: string, in_degree: number]>;
  counts: { rows: number; explicit_rows: number; sources: number; edges_kept: number; names_total: number };
  chunk_size: number;
  chunks: number;
  format: string;
}

interface Variant {
  shards: Map<string, Array<[string, number[]]>>;
  chunks: string[][];
  namesTotal: number;
  edgesKept: number;
  shardBytes: number;
  chunkBytes: number;
  totalBytes: number;
}

// lists → int-encoded shards + name-table chunks, measured.
function assemble(lists: Map<string, string[]>): Variant {
  const nameSet = new Set<string>();
  for (const targets of lists.values()) for (const t of targets) nameSet.add(t);
  const names = [...nameSet].sort();
  const idx = new Map(names.map((n, i) => [n, i]));
  let edgesKept = 0;
  const entries: Array<[string, number[]]> = [];
  for (const [source, targets] of lists) {
    entries.push([source, targets.map((t) => idx.get(t)!)]);
    edgesKept += targets.length;
  }
  const shards = buildKeyedShards(entries);
  let shardBytes = 0;
  for (const arr of shards.values()) {
    shardBytes += Buffer.byteLength(JSON.stringify(Object.fromEntries(arr)), "utf8");
  }
  const chunks: string[][] = [];
  let chunkBytes = 0;
  for (let i = 0; i < names.length; i += CHUNK_SIZE) {
    const chunk = names.slice(i, i + CHUNK_SIZE);
    chunks.push(chunk);
    chunkBytes += Buffer.byteLength(JSON.stringify(chunk), "utf8");
  }
  return {
    shards,
    chunks,
    namesTotal: names.length,
    edgesKept,
    shardBytes,
    chunkBytes,
    totalBytes: shardBytes + chunkBytes,
  };
}

async function buildPremiseIndex(
  edgesPath: string,
  sourceMetadata: HfSourceMetadata,
) {
  const declDataPath = resolve(process.cwd(), DECL_DATA);
  const declManifestPath = resolve(process.cwd(), "public", "assets", "decl-index", "manifest.json");
  const outDir = resolve(process.cwd(), "public", "assets", "premise-index");

  if (!existsSync(edgesPath)) {
    throw new Error(
      `MISSING INPUT: ${edgesPath}\n` +
        `The MathNetwork/MathlibGraph edge dump is a machine-local cache (754 MB, gitignored).\n` +
        `Fetch it with the reviewed revision shown in catalog/huggingface_pins.json.`,
    );
  }
  if (!existsSync(declDataPath)) {
    throw new Error(
      `MISSING INPUT: ${declDataPath}\n` +
        `The raw doc-gen4 body is cached by the decl-index refresh. Run first (from wiki/):\n` +
        `  DECL_DATA_CACHE=${declDataPath} npm run build:decl-index`,
    );
  }

  console.log(`reading ${declDataPath}`);
  const declData = JSON.parse(readFileSync(declDataPath, "utf8")) as {
    declarations: Record<string, { kind?: string }>;
  };
  const oracle = declData.declarations;
  const declManifest = JSON.parse(readFileSync(declManifestPath, "utf8")) as Manifest;

  // SNAPSHOT GUARD: the oracle gate joins against DECL_DATA_CACHE while the
  // recorded pin comes from the decl-index manifest — two files that only
  // correspond when the cache was written by the same build:decl-index run.
  // The .meta.json sidecar exists precisely to check this; a mismatch means
  // the emitted manifest would record a join that never happened, so fail loud.
  const metaPath = declDataPath + ".meta.json";
  if (!existsSync(metaPath)) {
    throw new Error(
      `MISSING SIDECAR: ${metaPath}\n` +
        `The decl-data cache has no provenance sidecar, so the oracle snapshot cannot be verified.\n` +
        `Re-run (from wiki/): DECL_DATA_CACHE=${declDataPath} npm run build:decl-index`,
    );
  }
  const meta = JSON.parse(readFileSync(metaPath, "utf8")) as { etag?: string };
  if (meta.etag !== declManifest.source_sha_or_etag) {
    throw new Error(
      `SNAPSHOT MISMATCH: decl-data cache etag ${meta.etag} != decl-index manifest etag ` +
        `${declManifest.source_sha_or_etag}.\n` +
        `The cache and the emitted decl-index come from different doc-gen4 snapshots — the oracle\n` +
        `gate would filter against one universe while pin.decl_index_etag records another.\n` +
        `Re-run (from wiki/): DECL_DATA_CACHE=${declDataPath} npm run build:decl-index (or npm run build:indexes)`,
    );
  }

  // Pass 1 — in-degree over explicit edges, for the hub drop.
  console.log(`pass 1/2: in-degrees over ${edgesPath}`);
  const inDeg = new Map<string, number>();
  let explicitRows = 0;
  const rows = await eachEdge(edgesPath, (source, target, explicit) => {
    if (!explicit || source === target) return;
    explicitRows++;
    inDeg.set(target, (inDeg.get(target) ?? 0) + 1);
  });
  const hubCount = Math.ceil(inDeg.size * HUB_DROP_FRAC);
  const hubList = [...inDeg.entries()]
    .sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1))
    .slice(0, hubCount);
  const hubs = new Set(hubList.map(([n]) => n));
  console.log(`  rows ${rows}, explicit ${explicitRows}, distinct explicit targets ${inDeg.size}`);
  console.log(`  hub drop: top ${hubCount} (in-degree ≥ ${hubList[hubList.length - 1][1]}); head: ${hubList.slice(0, 5).map(([n, d]) => `${n}(${d})`).join(", ")}`);

  // Pass 2 — per-source top-K premises in file (= proof) order.
  console.log(`pass 2/2: per-source top-${TOP_K} lists`);
  const lists = new Map<string, string[]>();
  let skippedHub = 0;
  let skippedTargetOracle = 0;
  let skippedSourceOracle = 0;
  await eachEdge(edgesPath, (source, target, explicit) => {
    if (!explicit || source === target) return;
    if (hubs.has(target)) { skippedHub++; return; }
    if (!(source in oracle)) { skippedSourceOracle++; return; }
    if (!(target in oracle)) { skippedTargetOracle++; return; }
    let arr = lists.get(source);
    if (!arr) { arr = []; lists.set(source, arr); }
    if (arr.length < TOP_K && !arr.includes(target)) arr.push(target);
  });
  console.log(`  sources ${lists.size}; edge rows skipped: hub ${skippedHub}, source∉oracle ${skippedSourceOracle}, target∉oracle ${skippedTargetOracle}`);

  let variant = assemble(lists);
  console.log(`  all-kinds variant: ${(variant.totalBytes / 1e6).toFixed(1)} MB (shards ${(variant.shardBytes / 1e6).toFixed(1)} + names ${(variant.chunkBytes / 1e6).toFixed(1)})`);
  let theoremOnly = false;
  if (variant.totalBytes > BUDGET_BYTES) {
    theoremOnly = true;
    const before = lists.size;
    for (const source of [...lists.keys()]) {
      if (!THEOREM_KINDS.has(oracle[source]?.kind ?? "")) lists.delete(source);
    }
    variant = assemble(lists);
    console.log(`  over ${(BUDGET_BYTES / 1e6).toFixed(0)} MB budget → theorem-kind sources only: ${before} → ${lists.size} sources, ${(variant.totalBytes / 1e6).toFixed(1)} MB`);
  }

  let maxLen = MIN_KEY_LEN;
  const counts: Record<string, number> = {};
  for (const [key, arr] of variant.shards) {
    counts[key] = arr.length;
    if (key.length > maxLen) maxLen = key.length;
  }
  const manifest: PremiseManifest = {
    built_at: new Date().toISOString(),
    source: "catalog/.cache/mathnetwork/edges.csv (catalog/fetch_mathlib_graph.py)",
    source_sha_or_etag: `sha256:${sourceMetadata.sha256}`,
    source_dataset: "https://huggingface.co/datasets/MathNetwork/MathlibGraph",
    source_license: "Apache-2.0",
    source_paper: "arXiv:2604.24797",
    pin: {
      dataset_revision: sourceMetadata.revision,
      edges_sha256: sourceMetadata.sha256,
      edges_bytes: sourceMetadata.size,
      edges_url: sourceMetadata.file_url,
      decl_index_etag: declManifest.source_sha_or_etag,
    },
    filters: {
      is_explicit: true,
      drop_self_loops: true,
      top_k: TOP_K,
      hub_drop_top_frac: HUB_DROP_FRAC,
      hub_drop_count: hubCount,
      oracle_gate: "source and target must exist in the decl-index snapshot (pin.decl_index_etag)",
      theorem_like_only: theoremOnly,
      theorem_like_kinds: theoremOnly ? [...THEOREM_KINDS] : [],
    },
    hub_drop: hubList,
    counts: {
      rows,
      explicit_rows: explicitRows,
      sources: lists.size,
      edges_kept: variant.edgesKept,
      names_total: variant.namesTotal,
    },
    chunk_size: CHUNK_SIZE,
    chunks: variant.chunks.length,
    total: lists.size,
    format:
      `shard = { [source_decl]: [int, ...] } (proof order, deduped, ≤${TOP_K}); ` +
      `int i → names/<floor(i/${CHUNK_SIZE})>.json[i % ${CHUNK_SIZE}]; name table sorted`,
    scheme: { kind: "prefix", min_len: MIN_KEY_LEN, max_len: maxLen, max_bytes: MAX_SHARD_BYTES, pad: PAD },
    shards: counts,
  };

  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(resolve(outDir, "names"), { recursive: true });
  let largest = { key: "", bytes: 0 };
  for (const [key, arr] of variant.shards) {
    const json = JSON.stringify(Object.fromEntries(arr));
    const bytes = Buffer.byteLength(json, "utf8");
    if (bytes > largest.bytes) largest = { key, bytes };
    writeFileSync(resolve(outDir, `${key}.json`), json);
  }
  variant.chunks.forEach((chunk, i) => {
    writeFileSync(resolve(outDir, "names", `${i}.json`), JSON.stringify(chunk));
  });
  const manifestJson = JSON.stringify(manifest);
  writeFileSync(resolve(outDir, "manifest.json"), manifestJson);

  console.log(`wrote ${outDir}`);
  console.log(`  sources:        ${lists.size} (theorem_like_only: ${theoremOnly})`);
  console.log(`  edges kept:     ${variant.edgesKept} (mean ${(variant.edgesKept / lists.size).toFixed(2)}/source)`);
  console.log(`  name table:     ${variant.namesTotal} names in ${variant.chunks.length} chunks`);
  console.log(`  shards:         ${variant.shards.size} (+ ${variant.chunks.length} name chunks + manifest.json)`);
  console.log(`  bytes:          shards ${(variant.shardBytes / 1e6).toFixed(1)} MB + names ${(variant.chunkBytes / 1e6).toFixed(1)} MB + manifest ${(Buffer.byteLength(manifestJson) / 1e3).toFixed(0)} KB = ${((variant.totalBytes + Buffer.byteLength(manifestJson)) / 1e6).toFixed(1)} MB`);
  console.log(`  largest shard:  ${largest.key}.json — ${largest.bytes} bytes`);
  console.log(`  max key length: ${maxLen}`);
}

async function main() {
  const edgesPath = resolve(process.cwd(), EDGES_CSV);
  const guard = await acquireHfGuard(edgesPath);
  try {
    await buildPremiseIndex(edgesPath, guard.metadata);
  } finally {
    await guard.release();
  }
}

const isCli = import.meta.url === `file://${process.argv[1]}`;
if (isCli) {
  main().catch((e) => {
    console.error(e);
    process.exit(1);
  });
}
