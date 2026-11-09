// Brain CELL-shard ASSETS shim shared by brain-api.test.ts and mcp.test.ts: a
// small but complete v3 fixture (cells with organs of every kind, embedded
// payloads + licensed snippets, aggregated synapses with traces, supercells
// owning a rule-5 field concept, aliases, labels with `aka`, decl-index shards,
// and the suffix-index + premise-index sibling assets) served through a fake
// ASSETS fetcher, keyed with the REAL declShardKey so shard resolution matches
// production (same approach as brain-edges.test.ts).
//
// Shapes mirror site/assets/brain/cells/ exactly — including the ones that bite:
// a supercell's synapses ship WITHOUT traces, `tt` appears only when traces were
// trimmed, and `truncated.syn` is a COUNT, not a flag.

import { declShardKey } from "../../src/decl.js";
import type { Env } from "../../src/env.js";
import { nameParts } from "../../src/brain-name-parts.js";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { makeD1, type OnStatement } from "./d1shim.js";
import { namesFtsCreate, servingRows, type Cell, type ServingRows } from "./brain-d1-rows.ts";
import {
  DEFAULT_ALIASES,
  fixtureData,
  PARITY_XREF_INDEX,
  servingTreeFor,
  shardEntry,
} from "./brain-fixture-data.ts";

// The atoms, organs, aliases, labels, supercells and shard entries, and the
// parity variant, are in brain-fixture-data.ts; every export there is
// re-exported here.
export * from "./brain-fixture-data.ts";

// The doc-gen4 decl-index oracle. `Basis` and `AddCircle.fourierCoeff` are
// DELIBERATELY ABSENT (their prefixes `ba`/`ad` are not shards) — they are dead
// names whose current forms `Module.Basis` / `fourierCoeff` DO resolve, so a
// batch decl_exists can serve a verified rename. `Finset.sum_comm` is present so
// a unique-suffix candidate can be verified against the oracle. Pairs stay sorted
// (lookupInShard binary-searches). The manifest carries the snapshot etag the
// suffix index must match (suffixLookup's pin check).
export const FIXTURE_DECL_ETAG = 'W/"fixture-decl-snapshot"';
const DECL_MANIFEST = {
  scheme: { min_len: 2, max_len: 2, pad: "_" },
  shards: { co: 2, do: 1, ha: 2, mo: 2, fi: 1, fo: 1, se: 1 } as Record<string, number>,
  source_sha_or_etag: FIXTURE_DECL_ETAG,
};
const DECL_SHARDS: Record<string, Array<[string, string]>> = {
  co: [
    ["CommGroup", "Mathlib.Algebra.Group.Defs"],
    ["Commute.exp_right", "Mathlib.Analysis.SpecialFunctions.Exponential"],
  ],
  do: [["DoubleCoset.mk_out_eq_mul", "Mathlib.GroupTheory.DoubleCoset"]],
  ha: [
    ["HAdd", "Mathlib.Init.Prelude"],
    ["HAdd.hAdd", "Mathlib.Init.Prelude"],
  ],
  mo: [
    ["Module", "Mathlib.Algebra.Module.Defs"],
    ["Module.Basis", "Mathlib.LinearAlgebra.Basis.Defs"],
  ],
  fi: [["Finset.sum_comm", "Mathlib.Algebra.BigOperators.Basic"]],
  fo: [["fourierCoeff", "Mathlib.Analysis.Fourier.AddCircle"]],
  se: [["Semiconj.exp_right", "Mathlib.Analysis.SpecialFunctions.Exponential"]],
};

// The suffix index — final-segment → candidate FQ names over the FULL decl
// universe (wiki/scripts/build-suffix-index.ts; API tranche #18). Shards
// resolve with the same declShardFor prefix scheme, keyed on the NORMALIZED
// final segment. Both builder shapes ship here: the bare entries array
// (uncapped — the majority shape) and { total_count, entries } when storage is
// capped, with total_count the TRUE total ("extreme minority" lesson: the cap
// must never masquerade as the population). The manifest's source_sha_or_etag
// matches the decl manifest's (suffixLookup refuses a mismatched pair).
//   sum_comm      → unique, oracle-verified, BARE-ARRAY bucket (namespace-resolution)
//   mk_out_eq_mul → unique — THE mathlibmpr_063/003 shape: the agent had the
//                   exact segment, only the namespace was wrong (Doset →
//                   DoubleCoset)
//   exp_right     → 3 stored, 1 stale: `Retired.exp_right` is NOT in the
//                   oracle, so the served namespace_matches list holds 2
//   gone_lemma    → unique but FAILS oracle verification ⇒ no suggestion
//   mk            → the hub segment: 10 stored of n=6133 (real data: "mk")
//   hadd          → the normalized-collision bucket: `HAdd` and `HAdd.hAdd`
//                   share the key but their EXACT final segments differ — the
//                   consumer must disambiguate on the stored exact names
//   capped_seg    → a CAPPED bucket (total_count > entries stored): the view
//                   is incomplete, so no uniqueness claim may come from it
const SUFFIX_MANIFEST = {
  scheme: { min_len: 2, max_len: 2, pad: "_" },
  shards: { su: 1, mk: 2, ex: 1, go: 1, ha: 1, ca: 1 } as Record<string, number>,
  source_sha_or_etag: FIXTURE_DECL_ETAG,
};
const SUFFIX_SHARDS: Record<string, Record<string, unknown>> = {
  su: {
    // the bare-array (uncapped) shape — what ~all real buckets ship as
    sum_comm: [["Finset.sum_comm", "Mathlib.Algebra.BigOperators.Basic"]],
  },
  mk: {
    mk: {
      entries: [
        ["Equiv.mk", "Mathlib.Logic.Equiv.Defs"],
        ["Fin.mk", "Mathlib.Data.Fin.Basic"],
        ["Finset.mk", "Mathlib.Data.Finset.Basic"],
        ["Int.mk", "Mathlib.Data.Int.Defs"],
        ["List.mk", "Mathlib.Data.List.Basic"],
        ["Multiset.mk", "Mathlib.Data.Multiset.Basic"],
        ["Nat.mk", "Mathlib.Data.Nat.Defs"],
        ["Option.mk", "Mathlib.Data.Option.Basic"],
        ["Prod.mk", "Mathlib.Data.Prod.Basic"],
        ["Quot.mk", "Mathlib.Data.Quot"],
      ],
      total_count: 6133,
    },
    mk_out_eq_mul: { entries: [["DoubleCoset.mk_out_eq_mul", "Mathlib.GroupTheory.DoubleCoset"]], total_count: 1 },
  },
  ex: {
    exp_right: {
      entries: [
        ["Commute.exp_right", "Mathlib.Analysis.SpecialFunctions.Exponential"],
        ["Retired.exp_right", "Mathlib.Analysis.OldExponential"], // stale — not in the oracle
        ["Semiconj.exp_right", "Mathlib.Analysis.SpecialFunctions.Exponential"],
      ],
      total_count: 3,
    },
  },
  go: {
    gone_lemma: { entries: [["Deleted.gone_lemma", "Mathlib.Old.Gone"]], total_count: 1 },
  },
  ha: {
    // key-normalized collision: two DISTINCT exact segments in one bucket
    hadd: {
      entries: [
        ["HAdd", "Mathlib.Init.Prelude"],
        ["HAdd.hAdd", "Mathlib.Init.Prelude"],
      ],
      total_count: 2,
    },
  },
  ca: {
    // capped: 5 sharers exist, only 1 stored — enumeration/uniqueness forbidden
    capped_seg: { entries: [["Stored.capped_seg", "Mathlib.Some.Module"]], total_count: 5 },
  },
};

// The premise index — per-decl stored premise lists (API tranche #18; derived
// from MathNetwork/MathlibGraph, Apache-2.0). Shards resolve with the same
// declShardFor prefix scheme keyed on the SOURCE decl name; values are int
// lists (stored rank order, K≤12) into fixed 8192-name chunk tables
// names/<chunk>.json. The manifest mirrors the BUILDER's real shape
// (build-premise-index.ts): `chunk_size` (the Worker validates it — a silent
// divergence mis-decodes every int into a wrong-but-real name) and `pin` as
// the exact-revision source object (with a legacy-mtime fixture option).
//   Module                   → [CommGroup, Finset.sum_comm, fourierCoeff]
//   Module.Basis             → [Finset.sum_comm, Semiconj.exp_right]
//   DoubleCoset.mk_out_eq_mul → [CommGroup, Ghost.gone, Finset.sum_comm,
//                               fourierCoeff] — `Ghost.gone` is NOT in the
//                               oracle, so it drops and rows below the limit
//                               cutoff must backfill.
// Union of Module+Module.Basis ranks Finset.sum_comm first (multiplicity 2),
// then CommGroup (rank 0) < Semiconj.exp_right (rank 1) < fourierCoeff (rank 2).
export const FIXTURE_EDGES_MTIME = "2026-07-03T00:00:00.000Z";
export const FIXTURE_DATASET_REVISION = "8c706461fe266802197b62af324de12a3f1aa7fb";
export const FIXTURE_EDGES_SHA256 = "78b20d6311388159bdab03ddfb68d5ef5687ced629ee04a99d9880bcd043a08f";
export const FIXTURE_EDGES_URL =
  `https://huggingface.co/datasets/MathNetwork/MathlibGraph/resolve/${FIXTURE_DATASET_REVISION}/edges.csv`;
const PREMISE_NAMES: string[][] = [
  ["CommGroup", "Module", "Finset.sum_comm", "Module.Basis", "fourierCoeff", "Semiconj.exp_right", "Ghost.gone"],
];
const PREMISE_MANIFEST = {
  scheme: { min_len: 2, max_len: 2, pad: "_" },
  shards: { mo: 2, fi: 1, do: 1 } as Record<string, number>,
  chunk_size: 8192,
  chunks: 1,
  source: "MathNetwork/MathlibGraph (arXiv 2604.24797, Apache-2.0)",
  pin: {
    dataset_revision: FIXTURE_DATASET_REVISION,
    edges_sha256: FIXTURE_EDGES_SHA256,
    edges_bytes: 753711915,
    edges_url: FIXTURE_EDGES_URL,
    decl_index_etag: FIXTURE_DECL_ETAG,
  },
  filters: { explicit_only: true, per_decl_cap: 12 },
  hub_drop: ["Eq.mpr", "Eq.mp", "id"],
};
const PREMISE_SHARDS: Record<string, Record<string, number[]>> = {
  mo: {
    Module: [0, 2, 4],
    "Module.Basis": [2, 5],
  },
  fi: {
    "Finset.sum_comm": [0],
  },
  do: {
    "DoubleCoset.mk_out_eq_mul": [0, 6, 2, 4],
  },
};


const KEY_LEN = 6; // every fixture atom id is ≥6 chars, so fixed-length keys suffice

export interface BrainFixtureOpts {
  // undefined → serve DEFAULT_ALIASES; null → 404 aliases.json (degradation path)
  aliases?: object | null;
  // null → 404 the whole suffix index (decl_exists must still answer; only the
  // namespace-resolution suggestions go away)
  suffixIndex?: null;
  // null → 404 the whole premise index (brain_premises must 503, decl_exists
  // must be unaffected)
  premiseIndex?: null;
  // override the premise manifest's chunk_size (the Worker must 503 a mismatch
  // instead of mis-decoding every int)
  premiseChunkSize?: number;
  // Serve the currently deployed pre-pin manifest shape for compatibility.
  legacyPremisePin?: boolean;
  // override the suffix manifest's source_sha_or_etag (a mismatch with the decl
  // manifest must degrade namespace suggestions to none)
  suffixEtag?: string;
  // Perturb a valid manifest's identity to model selector rollover.
  releaseVariant?: string;
  // Override only the manifest claim after its identity is computed.
  manifestReleaseId?: string;
  // Set before identity calculation, including malformed profile bindings.
  releaseProfile?: string;
  releaseReplay?: unknown;
  // Mutate a complete manifest after its identity is computed, to model tampering.
  mutateReleaseManifest?: (manifest: Record<string, unknown>) => void;
  // Mutate the artifact list before identity calculation, so the release stays
  // self-consistent and only the artifact checks can refuse it.
  mutateArtifacts?: (artifacts: Array<Record<string, unknown>>) => void;
  // Change only an identity-excluded audit timestamp while preserving release_id.
  releaseCreatedAt?: string;
  // Override only the selector's exact manifest claim, to model byte-identity drift.
  selectorManifestSha256?: string;
  // Serve the deployed v1 selector shape during the v2 migration.
  legacyReleaseSelector?: boolean;
  // Omit paths before computing identity, producing a self-consistent but incomplete release.
  omitReleaseArtifacts?: string[];
  // Set before computing identity; current runtime profile must reject non-null
  // changeset claims until accepted-changeset replay verification exists.
  throughChangeset?: string;
  xrefIndex?: Record<string, string[]>;
  // Shape cells/manifest.json like an offline-replay release: generated_at is
  // the sha256: generation id and the prov rows follow the live order
  // (concept-graph family digest first, attribute-lineage digest next, no
  // git-shaped pin on any mathlib/kerodon/stacks row). `leadingProv` rows are
  // prepended, to model a different first-seen row order.
  replaySnapshot?: { generatedAt: string; mathlibPin: string; leadingProv?: Array<Record<string, string>> };
  // Return a transient 404 this many times before serving xref_index.json.
  xrefIndexFailures?: number;
  // Return 404 for a declared release-relative asset (for example cells/mo____.json).
  missingAssets?: string[];
  onAssetPath?: (path: string) => void;
  // The parity variant (PARITY_* below): a tree build_serving_db accepts, so
  // one release can be served through both stores. Off, nothing changes.
  parity?: boolean;
  // List brain/data/serving-tables.json with this body, which makes the
  // release a D1 release; its logical root (the body's digest) is the dataset id.
  servingSidecar?: Record<string, unknown>;
}

interface FixtureRelease {
  assetBodies: Map<string, string>;
  manifest: Record<string, unknown>;
  releaseId: string;
  releaseHex: string;
  manifestSha256: string;
  datasetId: string | null;
}


function fixtureDigest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const object = value as Record<string, unknown>;
  return `{${Object.keys(object).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`).join(",")}}`;
}

function fixtureReleaseIdentity(manifest: Record<string, unknown>): string {
  const identityValue = { ...manifest };
  delete identityValue.release_id;
  delete identityValue.attestations;
  delete identityValue.created_at;
  const payload = `wikilean\0wikilean.release.v1\0canonical-json-v1\0${canonicalJson(identityValue)}`;
  return `sha256:${fixtureDigest(payload)}`;
}

function buildFixtureRelease(opts: BrainFixtureOpts): FixtureRelease {
  const { cells, labels, supercells } = fixtureData(opts);
  const shards: Record<string, number> = {};
  const data: Record<string, Record<string, unknown>> = {};
  for (const [id, spec] of Object.entries(cells)) {
    const key = declShardKey(id, KEY_LEN);
    shards[key] = (shards[key] ?? 0) + 1;
    (data[key] ??= {})[id] = shardEntry(spec);
  }
  const replay = opts.replaySnapshot;
  const cellsManifest = {
    scheme: { kind: "prefix", min_len: KEY_LEN, max_len: KEY_LEN, pad: "_" },
    shards,
    prov: replay
      ? [
        ...(replay.leadingProv ?? []),
        // Live order (2026-10 offline replay): the concept-graph family row
        // first, then attribute rows pinned by a different lineage digest,
        // then TheoremGraph's 40-hex dataset revision (not a Mathlib source).
        { source: "mathlib", method: "agent+oracle", pin: replay.mathlibPin },
        { source: "mathlib", method: "@[wikidata] attribute (mathlib4 source)", pin: "sha256:" + "a".repeat(64) },
        { source: "kerodon", method: "@[kerodon] attribute (mathlib4 source)", pin: "sha256:" + "a".repeat(64) },
        { source: "theoremgraph", method: "dual-judge match", pin: "5caba941dd716f17dba4880bd7173edfb1cc36d1" },
      ]
      : [
        { source: "test", method: "fixture", pin: "2026-07-15" },
        { source: "mathlib", method: "@[wikidata] attribute (mathlib4 source)", pin: "2026-07-04" },
        { source: "mathlib_deps", method: "kernel extraction", pin: "2026-07-04" },
        { source: "theoremgraph", method: "dual-judge match", pin: "2026-07-04" },
      ],
    roots: ["path:Mathlib"],
    _meta: {
      schema: "brain/SCHEMA.md#v3",
      generated_at: replay ? replay.generatedAt : "2026-07-15",
      caps: { synapses_per_cell: 200, traces_per_synapse: 6 },
    },
  };
  const aliases = opts.aliases === undefined ? DEFAULT_ALIASES : opts.aliases;
  const xrefIndex = opts.xrefIndex ?? (opts.parity ? PARITY_XREF_INDEX : {});
  const assetValues = new Map<string, unknown>([
    ["sources.json", {}],
    ["xref_index.json", xrefIndex],
    ["cells/manifest.json", cellsManifest],
    ["cells/aliases.json", aliases ?? DEFAULT_ALIASES],
    ["cells/labels.json", labels],
    ["cells/supercells.json", supercells],
    ["cells/explorer.json", {}],
    ["cells/frontier_graph.json", {}],
  ]);
  for (const [key, shard] of Object.entries(data)) assetValues.set(`cells/${key}.json`, shard);
  const assetBodies = new Map(
    [...assetValues].map(([path, value]) => [path, JSON.stringify(value)]),
  );
  const artifactBodies = new Map<string, string>(
    [...assetBodies].map(([path, bytes]) => [`site/assets/brain/${path}`, bytes]),
  );
  artifactBodies.set("site/out/brain.html", "<!doctype html><title>Fixture Brain</title>");
  if (opts.servingSidecar) artifactBodies.set("brain/data/serving-tables.json", JSON.stringify(opts.servingSidecar));
  const omitted = new Set(opts.omitReleaseArtifacts ?? []);
  const artifacts = [...artifactBodies]
    .filter(([path]) => !omitted.has(path))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([path, bytes], index) => ({
      logical_name: `fixture_${index}`,
      path,
      media_type: path.endsWith(".json") ? "application/json" : "text/html",
      sha256: fixtureDigest(bytes),
      bytes: new TextEncoder().encode(bytes).byteLength,
      logical_format: path.endsWith(".json") ? "json" : "opaque",
      logical_root: path.endsWith(".json") ? `sha256:${fixtureDigest(bytes)}` : null,
    }));
  opts.mutateArtifacts?.(artifacts as Array<Record<string, unknown>>);
  const variant = fixtureDigest(opts.releaseVariant ?? "default").slice(0, 16);
  const releaseManifest: Record<string, unknown> = {
    schema: "wikilean.release/v1",
    profile: opts.releaseProfile ?? "brain-current-v1",
    ...(opts.releaseReplay === undefined ? {} : { replay: opts.releaseReplay }),
    authority: {
      git_commit: "0".repeat(40),
      semantic_state_root: `sha256:${"1".repeat(64)}`,
      through_changeset: opts.throughChangeset ?? null,
    },
    source_set_root: `sha256:${"2".repeat(64)}`,
    semantic_epoch: `fixture-${variant}`,
    reducer: {
      schedule: "fixture",
      version: "1",
      git_commit: "0".repeat(40),
      configuration_sha256: "3".repeat(64),
      environment_sha256: "4".repeat(64),
    },
    artifacts,
    attestations: [
      { kind: "build", path: "attestations/build.json", sha256: "a".repeat(64), bytes: 1 },
      { kind: "validation", path: "attestations/validation.json", sha256: "b".repeat(64), bytes: 1 },
    ],
    compatible_overlay_generation_ids: [],
    created_at: opts.releaseCreatedAt ?? "2026-07-15T00:00:00Z",
  };
  const releaseId = fixtureReleaseIdentity(releaseManifest);
  releaseManifest.release_id = opts.manifestReleaseId ?? releaseId;
  opts.mutateReleaseManifest?.(releaseManifest);
  const manifestSha256 = fixtureDigest(JSON.stringify(releaseManifest));
  const sidecarBody = artifactBodies.get("brain/data/serving-tables.json");
  return {
    assetBodies,
    manifest: releaseManifest,
    releaseId,
    releaseHex: releaseId.slice("sha256:".length),
    manifestSha256,
    datasetId: sidecarBody !== undefined && !omitted.has("brain/data/serving-tables.json")
      ? `sha256:${fixtureDigest(sidecarBody)}`
      : null,
  };
}

const DEFAULT_FIXTURE_RELEASE = buildFixtureRelease({});
export const FIXTURE_RELEASE_ID = DEFAULT_FIXTURE_RELEASE.releaseId;
export const FIXTURE_RELEASE_HEX = DEFAULT_FIXTURE_RELEASE.releaseHex;

export function installBrainFixture(
  env: Env,
  opts: BrainFixtureOpts = {},
): { releaseId: string; releaseHex: string; manifestSha256: string; datasetId: string | null } {
  const fixture = buildFixtureRelease(opts);
  const { releaseId, releaseHex } = fixture;
  const manifestSha256 = opts.selectorManifestSha256 ?? fixture.manifestSha256;
  const releaseBase = `/assets/brain/releases/${opts.legacyReleaseSelector ? releaseHex : manifestSha256}`;
  const selector = opts.legacyReleaseSelector
    ? {
      schema: "wikilean.release-selector/v1",
      release_id: releaseId,
      release: releaseHex,
      manifest: `${releaseBase}/release.json`,
      audited_at: "2026-07-15T00:00:00Z",
    }
    : {
      schema: "wikilean.release-selector/v2",
      release_id: releaseId,
      release: releaseHex,
      manifest_sha256: manifestSha256,
      manifest: `${releaseBase}/release.json`,
      audited_at: "2026-07-15T00:00:00Z",
    };
  let remainingXrefFailures = opts.xrefIndexFailures ?? 0;
  const missingAssets = new Set(opts.missingAssets ?? []);
  (env as unknown as { ASSETS: { fetch: (r: Request) => Promise<Response> } }).ASSETS = {
    fetch: async (req: Request) => {
      const path = new URL(req.url).pathname;
      opts.onAssetPath?.(path);
      const json = (o: unknown) => new Response(JSON.stringify(o), { status: 200 });
      if (path === "/assets/brain/current.json") return json(selector);
      if (path === `${releaseBase}/release.json`) return json(fixture.manifest);
      if (path.startsWith(`${releaseBase}/`)) {
        const relative = path.slice(releaseBase.length + 1);
        if (
          missingAssets.has(relative) ||
          (relative === "cells/aliases.json" && opts.aliases === null) ||
          (relative === "xref_index.json" && remainingXrefFailures-- > 0)
        ) {
          return new Response("not found", { status: 404 });
        }
        const body = fixture.assetBodies.get(relative);
        if (body !== undefined) return new Response(body, { status: 200 });
      }
      if (path === "/assets/decl-index/manifest.json") return json(DECL_MANIFEST);
      const dm = /^\/assets\/decl-index\/([a-z0-9_]+)\.json$/.exec(path);
      if (dm && DECL_SHARDS[dm[1]]) return json(DECL_SHARDS[dm[1]]);
      if (opts.suffixIndex !== null) {
        if (path === "/assets/suffix-index/manifest.json")
          return json(
            opts.suffixEtag ? { ...SUFFIX_MANIFEST, source_sha_or_etag: opts.suffixEtag } : SUFFIX_MANIFEST,
          );
        const sm = /^\/assets\/suffix-index\/([a-z0-9_]+)\.json$/.exec(path);
        if (sm && SUFFIX_SHARDS[sm[1]]) return json(SUFFIX_SHARDS[sm[1]]);
      }
      if (opts.premiseIndex !== null) {
        if (path === "/assets/premise-index/manifest.json")
          return json({
            ...PREMISE_MANIFEST,
            ...(opts.premiseChunkSize ? { chunk_size: opts.premiseChunkSize } : {}),
            ...(opts.legacyPremisePin
              ? {
                  pin: {
                    edges_mtime: FIXTURE_EDGES_MTIME,
                    edges_bytes: 753711915,
                    decl_index_etag: FIXTURE_DECL_ETAG,
                  },
                }
              : {}),
          });
        const pn = /^\/assets\/premise-index\/names\/(\d+)\.json$/.exec(path);
        if (pn && PREMISE_NAMES[Number(pn[1])]) return json(PREMISE_NAMES[Number(pn[1])]);
        const pm = /^\/assets\/premise-index\/([a-z0-9_]+)\.json$/.exec(path);
        if (pm && PREMISE_SHARDS[pm[1]]) return json(PREMISE_SHARDS[pm[1]]);
      }
      return new Response("not found", { status: 404 });
    },
  };
  return { releaseId, releaseHex, manifestSha256, datasetId: fixture.datasetId };
}

// ---- the D1 fixture ---------------------------------------------------------
//
// installBrainD1Fixture serves the parity variant as a D1 release: the release
// lists the serving sidecar (so storeFor picks the D1 store) and still carries
// every static file (so the static store can serve the same release), and
// env.BRAIN_DB is an in-memory wikilean-brain created from
// wiki/migrations-brain and filled with the rows brain-d1-rows.ts builds from
// the same values, plus the dataset's names_<hex> table as the importer
// creates it.

/** Every wikilean-brain migration, in order. */
export const MIGRATIONS_BRAIN: readonly string[] = ["0001_brain.sql", "0002_name_keys_exact.sql"];

export function brainMigration(name: string): string {
  return readFileSync(new URL(`../../migrations-brain/${name}`, import.meta.url), "utf8");
}

// Every D1 column after ds, per table (site/ops/brain_d1_import.py D1_COLUMNS),
// with what the fixture writes into the columns the port leaves out.
const D1_TABLE_COLUMNS: Record<string, string[]> = {
  cell_pages: ["seq", "id", "json"],
  aliases: ["seq", "kind", "key", "owner"],
  labels: ["seq", "ord", "id", "label", "aka", "f", "p", "libs", "xy"],
  supercells: ["seq", "ord", "path", "label", "aka", "parent", "fa", "frontier", "n_cells", "n_sub", "fhist", "kids", "syn", "json"],
  name_keys: ["seq", "name_lc", "kind", "owner", "len16"],
  hot_prefixes: ["seq", "prefix", "cells", "sups", "live_sups"],
  cell_under: ["seq", "anc", "ord"],
  synapse_traces: ["seq", "pair", "json"],
  xrefs: ["seq", "page_ord", "node_ord", "page", "node"],
};
const PLACEHOLDERS: Record<string, Cell> = { libs: null, xy: null, n_sub: 0, fhist: "{}", kids: null };

export const DECOY_DATASET_ID = `sha256:${"d".repeat(64)}`;

export interface BrainD1FixtureOpts extends Omit<BrainFixtureOpts, "parity" | "servingSidecar"> {
  // build_serving_db.HOT_PREFIX_NAMES for the rows (default 1,000; 2 reaches the hot path)
  hotPrefixNames?: number;
  namesMode?: "trigram" | "parts";
  datasetState?: "importing" | "ready" | "deleting";
  schemaVersion?: number;
  // false: the release names a dataset the database does not hold
  registerDataset?: boolean;
  // a copy of every row under another ready dataset, with altered text, seeded
  // before (lower ds) or after (higher ds) the release's own
  decoy?: "before" | "after";
  // a distinct sidecar body, so a second fixture gets its own dataset id
  datasetVariant?: string;
  onStatement?: OnStatement;
  // install into this database instead of a new one (two releases, one database)
  db?: DatabaseSync;
  // the migrations a new database gets (default MIGRATIONS_BRAIN)
  migrations?: readonly string[];
}

// A leaked decoy name row must name a real atom, or the correctly scoped head
// lookup (labelRows) would drop it without a trace: each name_keys owner
// becomes the next distinct owner, a bijection, so the decoy's
// UNIQUE(ds, name_lc, kind, owner) keys stay unique.
function ownerRotation(spec: { columns: string[]; rows: Cell[][] }): Map<string, string> {
  const at = spec.columns.indexOf("owner");
  const owners = [...new Set(spec.rows.map((row) => String(row[at])))];
  return new Map(owners.map((owner, i) => [owner, owners[(i + 1) % owners.length]]));
}

function decoyRow(table: string, columns: string[], row: Cell[], rotation: Map<string, string>): Cell[] {
  return row.map((value, i) => {
    const column = columns[i];
    if (typeof value !== "string") return value;
    if (table === "name_keys" && column === "owner") return rotation.get(value) ?? value;
    if (column === "owner" || column === "node" || column === "label") return `DECOY ${value}`;
    if (column === "json" && table === "cell_pages") {
      const entry = JSON.parse(value) as { cell?: Record<string, unknown> };
      if (entry.cell) entry.cell = { ...entry.cell, label: "DECOY" };
      return JSON.stringify(entry);
    }
    if (column === "json" && table === "supercells") return JSON.stringify({ ...JSON.parse(value), label: "DECOY" });
    if (column === "json" && table === "synapse_traces") {
      return JSON.stringify({ tt: 1, traces: [{ kind: "DECOY", src: "DECOY", dst: "DECOY" }] });
    }
    return value;
  });
}

function seedDataset(
  db: DatabaseSync,
  datasetId: string,
  rows: ServingRows,
  opts: { state: string; schemaVersion: number; namesMode: "trigram" | "parts"; decoy: boolean },
): void {
  db.prepare(
    "INSERT INTO datasets (dataset_id, schema_version, tables_json, longest_name, sup_names_from, names_mode, state, imported_at) " +
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
  ).run(datasetId, opts.schemaVersion, "{}", rows.longest_name, rows.sup_names_from, opts.namesMode, opts.state,
    "2026-11-09T00:00:00Z");
  const ds = Number((db.prepare("SELECT ds FROM datasets WHERE dataset_id = ?").get(datasetId) as { ds: number }).ds);
  const rotation = ownerRotation(rows.tables.name_keys);
  for (const [table, columns] of Object.entries(D1_TABLE_COLUMNS)) {
    const spec = rows.tables[table];
    const insert = db.prepare(
      `INSERT INTO ${table} (ds, ${columns.join(", ")}) VALUES (${["?", ...columns.map(() => "?")].join(", ")})`,
    );
    for (const raw of spec.rows) {
      const row = opts.decoy ? decoyRow(table, spec.columns, raw, rotation) : raw;
      const values = columns.map((column) => {
        if (table === "hot_prefixes" && column === "live_sups") return row[spec.columns.indexOf("sups")];
        const at = spec.columns.indexOf(column);
        return at >= 0 ? row[at] : PLACEHOLDERS[column];
      });
      insert.run(ds, ...values);
    }
  }
  if (opts.decoy) return;
  // the importer's statements (brain_d1_import.fts_create, which
  // test_brain_d1_import.py compares with namesFtsCreate, and fts_content)
  const hex = datasetId.slice("sha256:".length);
  const names = rows.tables.name_keys;
  db.exec(namesFtsCreate(opts.namesMode, hex));
  const fill = db.prepare(`INSERT INTO names_${hex} (rowid, ${opts.namesMode === "trigram" ? "name" : "parts"}) VALUES (?, ?)`);
  const seqAt = names.columns.indexOf("seq");
  const nameAt = names.columns.indexOf("name");
  for (const row of names.rows) {
    const name = String(row[nameAt]);
    fill.run(row[seqAt], opts.namesMode === "trigram" ? name : nameParts(name));
  }
}

/** A fresh wikilean-brain stand-in: the migrations applied (all by default), no dataset. */
export function brainDatabase(migrations: readonly string[] = MIGRATIONS_BRAIN): DatabaseSync {
  const db = new DatabaseSync(":memory:");
  for (const name of migrations) db.exec(brainMigration(name));
  return db;
}

export function installBrainD1Fixture(
  env: Env,
  opts: BrainD1FixtureOpts = {},
): { releaseId: string; releaseHex: string; manifestSha256: string; datasetId: string; db: DatabaseSync; rows: ServingRows } {
  const fixtureOpts: BrainFixtureOpts = {
    ...opts,
    parity: true,
    servingSidecar: { schema: "wikilean.brain-serving-tables/v1", fixture: opts.datasetVariant ?? "parity" },
  };
  const installed = installBrainFixture(env, fixtureOpts);
  const datasetId = installed.datasetId!;
  const rows = servingRows(servingTreeFor(fixtureOpts), { hotPrefixNames: opts.hotPrefixNames });
  const db = opts.db ?? brainDatabase(opts.migrations);
  const base = { state: "ready", schemaVersion: 1, namesMode: opts.namesMode ?? "trigram" as const, decoy: false };
  const decoyed = db.prepare("SELECT 1 FROM datasets WHERE dataset_id = ?").get(DECOY_DATASET_ID) !== undefined;
  if (opts.decoy === "before" && !decoyed) seedDataset(db, DECOY_DATASET_ID, rows, { ...base, decoy: true });
  const known = db.prepare("SELECT 1 FROM datasets WHERE dataset_id = ?").get(datasetId) !== undefined;
  if (opts.registerDataset !== false && !known) {
    seedDataset(db, datasetId, rows, {
      ...base,
      state: opts.datasetState ?? "ready",
      schemaVersion: opts.schemaVersion ?? 1,
    });
  }
  if (opts.decoy === "after" && !decoyed) seedDataset(db, DECOY_DATASET_ID, rows, { ...base, decoy: true });
  (env as unknown as { BRAIN_DB: unknown }).BRAIN_DB = makeD1(db, { onStatement: opts.onStatement });
  return { ...installed, datasetId, db, rows };
}
