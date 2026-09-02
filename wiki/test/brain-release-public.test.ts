import { createHash } from "node:crypto";
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { execFileSync } from "node:child_process";
import { afterEach, describe, expect, it } from "vitest";
import { stageBrainPublicRelease } from "../scripts/brain-release-public";

const roots: string[] = [];
const REQUIRED = [
  "brain/data/nodes.jsonl",
  "brain/data/edges.jsonl",
  "brain/data/edges_links.jsonl",
  "brain/data/brain.sqlite3",
  "brain/data/cells.jsonl",
  "brain/data/synapses.jsonl",
  "brain/data/frontier.jsonl",
  "brain/data/frontier_graph.json",
  "brain/data/community_edges.jsonl",
  "catalog/data/source_registry.json",
  "site/assets/brain/sources.json",
  "site/assets/brain/xref_index.json",
  "site/assets/brain/cells/manifest.json",
  "site/assets/brain/cells/aliases.json",
  "site/assets/brain/cells/labels.json",
  "site/assets/brain/cells/supercells.json",
  "site/assets/brain/cells/explorer.json",
  "site/assets/brain/cells/frontier_graph.json",
  "site/assets/brain/cells/aa.json",
  "site/assets/brain/cells/traces/aa.json",
];

function tempRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "wikilean-brain-public-"));
  roots.push(root);
  return root;
}

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

function digest(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
}

function identity(domain: string, value: Record<string, unknown>): string {
  const identityValue = { ...value };
  delete identityValue.release_id;
  delete identityValue.attestations;
  delete identityValue.created_at;
  return `sha256:${digest(Buffer.from(`wikilean\0${domain}\0canonical-json-v1\0${canonicalJson(identityValue)}`))}`;
}

function makeRelease(
  root: string,
  fill: string,
  largeShardBytes = 0,
  overrides: { profile?: string; throughChangeset?: string | null; omitArtifact?: string } = {},
): { releaseDir: string; manifestPath: string; releaseId: string; hex: string } {
  const artifactData = REQUIRED.filter(path => path !== overrides.omitArtifact).map((path, index) => ({
    path,
    bytes: largeShardBytes > 0 && path.endsWith("cells/aa.json")
      ? Buffer.alloc(largeShardBytes, fill)
      : Buffer.from(JSON.stringify({ fill, path, index }) + "\n"),
    logical_name: `artifact_${index}`,
    media_type: path.endsWith(".jsonl")
      ? "application/x-ndjson"
      : path.endsWith(".sqlite3")
        ? "application/vnd.sqlite3"
        : "application/json",
    logical_format: path.endsWith(".jsonl")
      ? "jsonl-rowset"
      : path.endsWith(".sqlite3")
        ? "opaque"
        : "json",
  }));
  artifactData.push({
    path: "site/out/brain.html",
    bytes: Buffer.from(
      `<html><script>fetch("/assets/brain/current.json")</script><p>frozen-${fill}</p></html>`,
    ),
    logical_name: "brain_page",
    media_type: "text/html",
    logical_format: "opaque",
  });
  artifactData.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
  const artifacts = artifactData.map(item => ({
    logical_name: item.logical_name,
    path: item.path,
    media_type: item.media_type,
    sha256: digest(item.bytes),
    bytes: item.bytes.byteLength,
    logical_format: item.logical_format,
    logical_root: item.logical_format === "opaque" ? null : `sha256:${digest(item.bytes)}`,
  }));
  const identityInput = {
    schema: "wikilean.release/v1",
    profile: overrides.profile ?? "brain-current-v1",
    authority: {
      git_commit: "0".repeat(40),
      semantic_state_root: `sha256:${"1".repeat(64)}`,
      through_changeset: overrides.throughChangeset ?? null,
    },
    source_set_root: `sha256:${"2".repeat(64)}`,
    semantic_epoch: `fixture-${digest(Buffer.from(fill)).slice(0, 16)}`,
    reducer: {
      schedule: "fixture",
      version: "1",
      git_commit: "0".repeat(40),
      configuration_sha256: "3".repeat(64),
      environment_sha256: "4".repeat(64),
    },
    artifacts,
    attestations: [],
    compatible_overlay_generation_ids: [],
    created_at: "2030-01-01T00:00:00Z",
  };
  const releaseId = identity("wikilean.release.v1", identityInput);
  const hex = releaseId.slice("sha256:".length);
  const releaseDir = join(root, hex);
  mkdirSync(releaseDir, { recursive: true });
  for (const item of artifactData) {
    const target = join(releaseDir, ...item.path.split("/"));
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, item.bytes);
  }
  const attestationData = [
    {
      kind: "build" as const,
      path: "attestations/build.json",
      bytes: Buffer.from(JSON.stringify({ schema: "wikilean.build-attestation/v1", release_id: releaseId })),
    },
    {
      kind: "validation" as const,
      path: "attestations/validation.json",
      bytes: Buffer.from(JSON.stringify({ schema: "wikilean.validation-attestation/v1", release_id: releaseId })),
    },
  ];
  const attestations = attestationData.map(item => ({
    kind: item.kind,
    path: item.path,
    sha256: digest(item.bytes),
    bytes: item.bytes.byteLength,
  }));
  for (const item of attestationData) {
    const target = join(releaseDir, ...item.path.split("/"));
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, item.bytes);
  }
  const manifest = { ...identityInput, release_id: releaseId, attestations };
  const manifestPath = join(releaseDir, "release.json");
  writeFileSync(manifestPath, JSON.stringify(manifest) + "\n");
  return { releaseDir, manifestPath, releaseId, hex };
}

const PUBLIC_BASELINE_SCHEMA = "wikilean.public-asset-baseline/v1";
const PUBLIC_BASELINE_DOMAIN = "wikilean.public-asset-baseline.v1";
const PUBLIC_BASELINE_AUTHORITY = "d".repeat(40);
const PUBLIC_BASELINE_CONTENTS: Record<string, string> = {
  "404.html": "baseline-404\n",
  "robots.txt": "User-agent: *\nAllow: /\n",
  "wikilean.ttl": "@prefix wl: <https://wikilean.example/> .\n",
  "concepts.html": "<html>baseline-concepts</html>\n",
  "assets/style.css": "body { color: baseline; }\n",
  "assets/script.js": "globalThis.baseline = true;\n",
  "assets/review.css": ".review { display: block; }\n",
  "assets/editor.js": "globalThis.baselineEditor = true;\n",
  "assets/mathlib-index.json": JSON.stringify([
    ["Nat.add", "Mathlib.Data.Nat.Basic"],
    ["Nat.mul", "Mathlib.Data.Nat.Basic"],
  ]),
  "assets/decl-index/manifest.json": "{\"schema\":\"fixture-decl-index\"}\n",
  "assets/decl-index/aa.json": "[\"Nat.add\"]\n",
  "assets/suffix-index/manifest.json": "{\"schema\":\"fixture-suffix-index\"}\n",
  "assets/suffix-index/aa.json": "[\"add\"]\n",
  "assets/premise-index/manifest.json": "{\"schema\":\"fixture-premise-index\"}\n",
  "assets/premise-index/aa.json": "[\"Nat.add_comm\"]\n",
  "assets/icons/wiki.svg": "<svg>baseline-icon</svg>\n",
};

interface PublicBaselineFixture {
  root: string;
  manifestPath: string;
  baselineId: string;
  hex: string;
  authorityCommit: string;
  files: Array<{ path: string; sha256: string; bytes: number }>;
  contents: Map<string, Buffer>;
  totalBytes: number;
}

function makePublicBaseline(
  store: string,
  options: {
    omit?: string[];
    manifestOnly?: Array<{ path: string; bytes: Buffer }>;
  } = {},
): PublicBaselineFixture {
  const omitted = new Set(options.omit ?? []);
  const contents = new Map(
    Object.entries(PUBLIC_BASELINE_CONTENTS)
      .filter(([path]) => !omitted.has(path))
      .map(([path, value]) => [path, Buffer.from(value)]),
  );
  const files = [
    ...[...contents].map(([path, bytes]) => ({
      path,
      sha256: digest(bytes),
      bytes: bytes.byteLength,
    })),
    ...(options.manifestOnly ?? []).map(item => ({
      path: item.path,
      sha256: digest(item.bytes),
      bytes: item.bytes.byteLength,
    })),
  ].sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0);
  const identityValue = {
    schema: PUBLIC_BASELINE_SCHEMA,
    authority: { git_commit: PUBLIC_BASELINE_AUTHORITY },
    files,
  };
  const baselineId = `sha256:${digest(Buffer.from(
    `wikilean\0${PUBLIC_BASELINE_DOMAIN}\0canonical-json-v1\0${canonicalJson(identityValue)}`,
  ))}`;
  const hex = baselineId.slice("sha256:".length);
  const root = join(store, hex);
  mkdirSync(root, { recursive: true });
  for (const [path, bytes] of contents) {
    const target = join(root, ...path.split("/"));
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, bytes);
  }
  const manifestPath = join(root, "manifest.json");
  writeFileSync(manifestPath, `${canonicalJson({ ...identityValue, baseline_id: baselineId })}\n`);
  return {
    root,
    manifestPath,
    baselineId,
    hex,
    authorityCommit: PUBLIC_BASELINE_AUTHORITY,
    files,
    contents,
    totalBytes: files.reduce((sum, file) => sum + file.bytes, 0),
  };
}

const buildPublicScript = resolve(import.meta.dirname, "..", "scripts", "build-public.ts");

function invokeBuildPublic(wiki: string, args: string[]): string {
  return execFileSync(process.execPath, [
    "--experimental-strip-types",
    buildPublicScript,
    ...args,
  ], { cwd: wiki, encoding: "utf8", stdio: "pipe" });
}

function baselineArgs(baseline: PublicBaselineFixture): string[] {
  return [
    "--public-baseline-manifest",
    baseline.manifestPath,
    "--public-baseline-dir",
    baseline.root,
  ];
}

function stage(release: ReturnType<typeof makeRelease>, destination: string, overrides = {}) {
  return stageBrainPublicRelease({
    manifestPath: release.manifestPath,
    releaseDir: release.releaseDir,
    destination,
    auditedAt: "2030-01-01T00:00:00Z",
    maxObjects: 1_000,
    maxBytes: 10_000_000,
    maxFileBytes: 1_000_000,
    ...overrides,
  });
}

function selector(destination: string): Record<string, unknown> {
  return JSON.parse(readFileSync(join(destination, "current.json"), "utf8"));
}

function immutablePath(destination: string, hex: string, relativePath: string): string {
  return join(destination, "releases", hex, relativePath);
}

describe("stageBrainPublicRelease", () => {
  it("stages a first immutable release, selector, and byte-identical aliases", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "A");
    const destination = join(root, "public", "assets", "brain");

    const result = stage(release, destination);

    expect(result.schema).toBe("wikilean.public-stage-result/v1");
    expect(result.release_id).toBe(release.releaseId);
    expect(result.previous_release_id).toBeNull();
    expect(result.retained_release_ids).toEqual([release.releaseId]);
    expect(result.copy_buffer_bytes).toBe(1024 * 1024);
    expect(result.duration_ms).toBeGreaterThanOrEqual(0);
    expect(result.max_rss_bytes).toBeGreaterThan(0);
    expect(result.free_bytes_before).toBeGreaterThan(0);
    expect(result.free_bytes_after).toBeGreaterThan(0);
    expect(result.warnings).toEqual([]);
    expect(result.brain_page).toBeNull();
    const stagedSelector = selector(destination);
    expect(stagedSelector).toEqual({
      schema: "wikilean.release-selector/v1",
      release_id: release.releaseId,
      release: release.hex,
      manifest: `/assets/brain/releases/${release.hex}/release.json`,
      audited_at: "2030-01-01T00:00:00Z",
    });
    stage(release, destination, { auditedAt: "2040-01-01T00:00:00Z" });
    expect(selector(destination).audited_at).toBe("2040-01-01T00:00:00Z");
    for (const relativePath of ["sources.json", "xref_index.json", "cells/manifest.json", "cells/aa.json"]) {
      expect(readFileSync(join(destination, relativePath))).toEqual(
        readFileSync(immutablePath(destination, release.hex, relativePath)),
      );
    }
    expect(existsSync(immutablePath(destination, release.hex, "brain/data/brain.sqlite3"))).toBe(false);
    expect(existsSync(immutablePath(destination, release.hex, "attestations/build.json"))).toBe(false);
  });

  it("retains prior production current, removes stale namespaces, and keeps aliases on current", () => {
    const root = tempRoot();
    const store = join(root, "store");
    const a = makeRelease(store, "A");
    const b = makeRelease(store, "B");
    const c = makeRelease(store, "C");
    const destination = join(root, "brain");

    stage(a, destination);
    stage(b, destination);
    mkdirSync(join(destination, "releases", "stale"), { recursive: true });
    writeFileSync(join(destination, "releases", "stale", "junk"), "junk");
    const result = stage(c, destination);

    expect(result.previous_release_id).toBe(b.releaseId);
    expect(readdirSync(join(destination, "releases")).sort()).toEqual([b.hex, c.hex].sort());
    expect(selector(destination)).toMatchObject({
      release_id: c.releaseId,
      previous_release_id: b.releaseId,
      previous_release: b.hex,
      previous_manifest: `/assets/brain/releases/${b.hex}/release.json`,
    });
    expect(readFileSync(join(destination, "sources.json"))).toEqual(
      readFileSync(immutablePath(destination, c.hex, "sources.json")),
    );
  });

  it("explicit previous release overrides a shadow-staged local selector", () => {
    const root = tempRoot();
    const store = join(root, "store");
    const a = makeRelease(store, "production-A");
    const b = makeRelease(store, "shadow-B");
    const c = makeRelease(store, "current-C");
    const destination = join(root, "brain");
    stage(a, destination);
    stage(b, destination);

    stage(c, destination, {
      previousManifestPath: a.manifestPath,
      previousReleaseDir: a.releaseDir,
    });

    expect(readdirSync(join(destination, "releases")).sort()).toEqual([a.hex, c.hex].sort());
    expect(selector(destination)).toMatchObject({
      release_id: c.releaseId,
      previous_release_id: a.releaseId,
    });
  });

  it("requires explicit previous release inputs as a pair", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "A");
    expect(() => stage(release, join(root, "brain"), {
      previousManifestPath: release.manifestPath,
    })).toThrow(/supplied together/);
  });

  it("restaging current retains the selector's previous release", () => {
    const root = tempRoot();
    const store = join(root, "store");
    const a = makeRelease(store, "A");
    const b = makeRelease(store, "B");
    const destination = join(root, "brain");
    stage(a, destination);
    stage(b, destination);

    stage(b, destination);

    expect(readdirSync(join(destination, "releases")).sort()).toEqual([a.hex, b.hex].sort());
    expect(selector(destination)).toMatchObject({
      release_id: b.releaseId,
      previous_release_id: a.releaseId,
    });
  });

  it("preserves the live directory when verification or limits fail", () => {
    const root = tempRoot();
    const store = join(root, "store");
    const a = makeRelease(store, "A");
    const b = makeRelease(store, "B");
    const destination = join(root, "brain");
    stage(a, destination);
    const before = readFileSync(join(destination, "current.json"));
    writeFileSync(join(b.releaseDir, "site", "assets", "brain", "sources.json"), "corrupt");

    expect(() => stage(b, destination)).toThrow(/sha256 mismatch|bytes/);
    expect(readFileSync(join(destination, "current.json"))).toEqual(before);

    const validB = makeRelease(join(root, "store-2"), "B2");
    for (const [overrides, error] of [
      [{ maxObjects: 1 }, /object count/],
      [{ maxBytes: 1 }, /byte count/],
      [{ maxFileBytes: 1 }, /configured (?:per-file )?limit|largest staged object/],
      [{ minFreeBytes: Number.MAX_SAFE_INTEGER }, /reserved headroom/],
    ] as const) {
      expect(() => stage(validB, destination, overrides)).toThrow(error);
      expect(readFileSync(join(destination, "current.json"))).toEqual(before);
    }
  });

  it("activates the frozen page with its asset release and rejects page mutation", () => {
    const root = tempRoot();
    const store = join(root, "store");
    const a = makeRelease(store, "A");
    const b = makeRelease(store, "B");
    const destination = join(root, "public", "assets", "brain");
    const pageDestination = join(root, "public", "brain.html");

    const result = stage(a, destination, { brainPageDestination: pageDestination });
    expect(result.brain_page).toMatchObject({
      destination: pageDestination,
      bytes: readFileSync(join(a.releaseDir, "site", "out", "brain.html")).byteLength,
    });
    expect(readFileSync(pageDestination)).toEqual(
      readFileSync(join(a.releaseDir, "site", "out", "brain.html")),
    );
    const selectorBefore = readFileSync(join(destination, "current.json"));
    const pageBefore = readFileSync(pageDestination);
    writeFileSync(join(b.releaseDir, "site", "out", "brain.html"), "mutated after freeze");

    expect(() => stage(b, destination, { brainPageDestination: pageDestination }))
      .toThrow(/brain\.html.*bytes|brain\.html.*sha256|page.*bytes|page.*sha256/i);
    expect(readFileSync(join(destination, "current.json"))).toEqual(selectorBefore);
    expect(readFileSync(pageDestination)).toEqual(pageBefore);
  });

  it("copies multi-chunk artifacts without materializing the namespace", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "L", 3 * 1024 * 1024 + 17);
    const destination = join(root, "brain");

    const result = stage(release, destination, {
      maxBytes: 20 * 1024 * 1024,
      maxFileBytes: 4 * 1024 * 1024,
    });

    expect(result.largest_file_bytes).toBe(3 * 1024 * 1024 + 17);
    expect(digest(readFileSync(join(destination, "cells", "aa.json")))).toBe(
      digest(readFileSync(immutablePath(destination, release.hex, "cells/aa.json"))),
    );
  });

  it("rejects a self-consistent artifact mutation under an unchanged release identity", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "A");
    const manifest = JSON.parse(readFileSync(release.manifestPath, "utf8"));
    const artifact = manifest.artifacts.find((value: { path: string }) => value.path.endsWith("sources.json"));
    const changed = Buffer.from("{\"changed\":true}\n");
    const artifactPath = join(release.releaseDir, ...artifact.path.split("/"));
    writeFileSync(artifactPath, changed);
    artifact.sha256 = digest(changed);
    artifact.bytes = changed.byteLength;
    writeFileSync(release.manifestPath, JSON.stringify(manifest) + "\n");

    expect(() => stage(release, join(root, "brain"))).toThrow(/does not identify the canonical manifest/);
  });

  it("rejects a self-consistent manifest for an unsupported release profile", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "wrong-profile", 0, {
      profile: "brain-future-v2",
    });

    expect(() => stage(release, join(root, "brain"))).toThrow(/unsupported profile/);
  });

  it("rejects a changeset-bearing release until replay verification exists", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "changeset", 0, {
      throughChangeset: "accepted-change-1",
    });

    expect(() => stage(release, join(root, "brain"))).toThrow(/through_changeset.*unsupported/);
  });

  it("rejects a self-consistent manifest missing a required internal artifact", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "missing-node-table", 0, {
      omitArtifact: "brain/data/nodes.jsonl",
    });

    expect(() => stage(release, join(root, "brain"))).toThrow(/missing required release artifacts.*nodes\.jsonl/);
  });

  it("rejects symlinked release artifacts without changing production", () => {
    const root = tempRoot();
    const store = join(root, "store");
    const a = makeRelease(store, "A");
    const b = makeRelease(store, "B");
    const destination = join(root, "brain");
    stage(a, destination);
    const before = readFileSync(join(destination, "current.json"));
    const sourcePath = join(b.releaseDir, "site", "assets", "brain", "sources.json");
    rmSync(sourcePath);
    symlinkSync(join(a.releaseDir, "site", "assets", "brain", "sources.json"), sourcePath);

    expect(() => stage(b, destination)).toThrow(/symlink/);
    expect(readFileSync(join(destination, "current.json"))).toEqual(before);
  });

  it("rejects a symlinked public destination", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "A");
    const target = join(root, "redirected");
    const destination = join(root, "brain");
    mkdirSync(target);
    symlinkSync(target, destination);

    expect(() => stage(release, destination)).toThrow(/destination must be a real directory/);
    expect(readdirSync(target)).toEqual([]);
  });

  it("rejects symlinked parent directories inside a frozen release", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "A");
    const destination = join(root, "brain");
    const realStatic = join(root, "real-static");
    const staticPath = join(release.releaseDir, "site");
    renameSync(staticPath, realStatic);
    symlinkSync(realStatic, staticPath);

    expect(() => stage(release, destination)).toThrow(/traverse a symlink/);
    expect(existsSync(destination)).toBe(false);
  });

  it("rejects unknown, missing, and partial fields in the prior selector", () => {
    const root = tempRoot();
    const store = join(root, "store");
    const a = makeRelease(store, "A");
    const b = makeRelease(store, "B");
    const destination = join(root, "brain");
    stage(a, destination);
    const selectorPath = join(destination, "current.json");
    const valid = selector(destination);

    for (const invalid of [
      { ...valid, surprise: true },
      Object.fromEntries(Object.entries(valid).filter(([key]) => key !== "manifest")),
      { ...valid, previous_release_id: b.releaseId },
    ]) {
      writeFileSync(selectorPath, JSON.stringify(invalid) + "\n");
      expect(() => stage(b, destination)).toThrow(/unknown fields|missing required fields|previous/);
      expect(readFileSync(selectorPath, "utf8")).toBe(JSON.stringify(invalid) + "\n");
    }
  });

  it("accepts a prior selector without the optional audit timestamp", () => {
    const root = tempRoot();
    const store = join(root, "store");
    const a = makeRelease(store, "A");
    const b = makeRelease(store, "B");
    const destination = join(root, "brain");
    stage(a, destination);
    const prior = selector(destination);
    delete prior.audited_at;
    writeFileSync(join(destination, "current.json"), JSON.stringify(prior) + "\n");

    stage(b, destination);

    expect(selector(destination)).toMatchObject({
      release_id: b.releaseId,
      previous_release_id: a.releaseId,
    });
  });

  it("rejects an explicit manifest outside the explicit release directory", () => {
    const root = tempRoot();
    const release = makeRelease(join(root, "store"), "A");
    const copiedManifest = join(root, "release.json");
    writeFileSync(copiedManifest, readFileSync(release.manifestPath));

    expect(() => stageBrainPublicRelease({
      manifestPath: copiedManifest,
      releaseDir: release.releaseDir,
      destination: join(root, "brain"),
    })).toThrow(/release directory's release.json/);
  });
});

describe("buildPublic", () => {
  it("keeps the legacy wiki/public default for shadow builds", () => {
    const root = tempRoot();
    const wiki = join(root, "wiki");
    const site = join(root, "site");
    mkdirSync(join(wiki, "assets"), { recursive: true });
    mkdirSync(join(wiki, "public"), { recursive: true });
    mkdirSync(join(site, "assets"), { recursive: true });
    mkdirSync(join(site, "out"), { recursive: true });
    mkdirSync(join(site, "annotations"), { recursive: true });
    writeFileSync(join(site, "assets", "style.css"), "style");
    writeFileSync(join(site, "out", "brain.html"), "mutable-page-must-not-ship");
    for (const file of ["concepts.html", "404.html", "robots.txt", "wikilean.ttl", "junk.txt"]) {
      writeFileSync(join(site, "out", file), `ignored-${file}`);
    }
    writeFileSync(join(wiki, "assets", "editor.js"), "editor");
    writeFileSync(join(wiki, "public", "about.html"), "stale");
    const release = makeRelease(join(root, "store"), "A");

    const output = invokeBuildPublic(wiki, [
      "--brain-release-manifest",
      release.manifestPath,
      "--brain-release-dir",
      release.releaseDir,
    ]);

    const metrics = JSON.parse(output);
    expect(metrics).toMatchObject({
      schema: "wikilean.public-build-result/v1",
      mathlib_declarations: 0,
      public_baseline: null,
      brain: {
        schema: "wikilean.public-stage-result/v1",
        release_id: release.releaseId,
        release: release.hex,
      },
    });
    expect(metrics.public_dir).toMatch(/\/wiki\/public$/);
    expect(metrics.brain.brain_page.destination).toMatch(/\/wiki\/public\/brain\.html$/);
    expect(metrics.duration_ms).toBeGreaterThanOrEqual(0);
    expect(metrics.max_rss_bytes).toBeGreaterThan(0);
    expect(readFileSync(join(wiki, "public", "brain.html"), "utf8")).toContain("frozen-A");
    expect(readFileSync(join(wiki, "public", "brain.html"), "utf8")).not.toContain("mutable-page");
    expect(readFileSync(join(wiki, "public", "assets", "style.css"), "utf8")).toBe("style");
    expect(readFileSync(join(wiki, "public", "assets", "editor.js"), "utf8")).toBe("editor");
    expect(existsSync(join(wiki, "public", "about.html"))).toBe(false);
    for (const file of ["concepts.html", "404.html", "robots.txt", "wikilean.ttl"]) {
      expect(readFileSync(join(wiki, "public", file), "utf8")).toBe(`ignored-${file}`);
    }
    expect(existsSync(join(wiki, "public", "junk.txt"))).toBe(false);
    expect(readFileSync(join(wiki, "public", "assets", "brain", "sources.json"))).toEqual(
      readFileSync(join(wiki, "public", "assets", "brain", "releases", release.hex, "sources.json")),
    );
    expect(JSON.parse(readFileSync(join(wiki, "public", "assets", "mathlib-index.json"), "utf8"))).toEqual([]);
  });

  it("builds a fresh external public tree with a fixed selector audit timestamp", () => {
    const root = tempRoot();
    const checkout = join(root, "checkout");
    const wiki = join(checkout, "wiki");
    const site = join(checkout, "site");
    const publicDir = join(root, "promotion", "public");
    mkdirSync(join(wiki, "assets"), { recursive: true });
    mkdirSync(join(wiki, "public"), { recursive: true });
    mkdirSync(publicDir, { recursive: true });
    mkdirSync(join(site, "assets"), { recursive: true });
    mkdirSync(join(site, "out"), { recursive: true });
    mkdirSync(join(site, "annotations"), { recursive: true });
    writeFileSync(join(site, "assets", "style.css"), "style");
    writeFileSync(join(site, "out", "brain.html"), "mutable-page-must-not-ship");
    for (const file of ["concepts.html", "404.html", "robots.txt", "wikilean.ttl", "junk.txt"]) {
      writeFileSync(join(site, "out", file), `ignored-${file}`);
    }
    writeFileSync(join(wiki, "assets", "editor.js"), "editor");
    writeFileSync(join(wiki, "public", "stale.txt"), "must remain isolated");
    const release = makeRelease(join(root, "store"), "A");
    const baseline = makePublicBaseline(join(root, "baselines"));

    const output = invokeBuildPublic(wiki, [
      "--public-dir",
      publicDir,
      ...baselineArgs(baseline),
      "--brain-release-manifest",
      release.manifestPath,
      "--brain-release-dir",
      release.releaseDir,
      "--brain-audited-at",
      "2031-02-03T04:05:06Z",
    ]);

    const metrics = JSON.parse(output);
    const physicalPublicDir = realpathSync(publicDir);
    expect(metrics.public_dir).toBe(physicalPublicDir);
    expect(metrics.mathlib_declarations).toBe(2);
    expect(metrics.public_baseline).toEqual({
      schema: PUBLIC_BASELINE_SCHEMA,
      baseline_id: baseline.baselineId,
      authority_commit: baseline.authorityCommit,
      root: realpathSync(baseline.root),
      files: baseline.files.length,
      bytes: baseline.totalBytes,
    });
    expect(metrics.brain).toMatchObject({
      release_id: release.releaseId,
      destination: join(physicalPublicDir, "assets", "brain"),
      brain_page: { destination: join(physicalPublicDir, "brain.html") },
    });
    expect(JSON.parse(readFileSync(join(publicDir, "assets", "brain", "current.json"), "utf8")))
      .toMatchObject({
        release_id: release.releaseId,
        audited_at: "2031-02-03T04:05:06Z",
      });
    expect(existsSync(join(publicDir, "stale.txt"))).toBe(false);
    expect(existsSync(join(publicDir, "junk.txt"))).toBe(false);
    expect(existsSync(join(publicDir, "manifest.json"))).toBe(false);
    for (const [path, bytes] of baseline.contents) {
      expect(readFileSync(join(publicDir, ...path.split("/")))).toEqual(bytes);
    }
    expect(readFileSync(join(publicDir, "assets", "style.css"), "utf8"))
      .not.toBe(readFileSync(join(site, "assets", "style.css"), "utf8"));
    expect(readFileSync(join(publicDir, "assets", "editor.js"), "utf8"))
      .not.toBe(readFileSync(join(wiki, "assets", "editor.js"), "utf8"));
    expect(readFileSync(join(publicDir, "concepts.html"), "utf8"))
      .not.toBe(readFileSync(join(site, "out", "concepts.html"), "utf8"));
    expect(readFileSync(join(wiki, "public", "stale.txt"), "utf8")).toBe("must remain isolated");
    expect(readFileSync(join(publicDir, "brain.html"), "utf8")).toContain("frozen-A");
  });

  it("requires a complete immutable baseline for explicit public staging", () => {
    const root = tempRoot();
    const checkout = join(root, "checkout");
    const wiki = join(checkout, "wiki");
    mkdirSync(wiki, { recursive: true });
    const release = makeRelease(join(root, "store"), "A");
    const publicDir = join(root, "promotion", "public");
    const brain = [
      "--brain-release-manifest",
      release.manifestPath,
      "--brain-release-dir",
      release.releaseDir,
    ];

    expect(() => invokeBuildPublic(wiki, ["--public-dir", publicDir, ...brain]))
      .toThrow(/requires --public-baseline-manifest and --public-baseline-dir/);
    expect(existsSync(publicDir)).toBe(false);

    const baseline = makePublicBaseline(join(root, "baselines"));
    expect(() => invokeBuildPublic(wiki, [
      "--public-dir",
      publicDir,
      "--public-baseline-manifest",
      baseline.manifestPath,
      ...brain,
    ])).toThrow(/manifest and directory must be supplied together/);
    expect(() => invokeBuildPublic(wiki, [...baselineArgs(baseline), ...brain]))
      .toThrow(/require an explicit --public-dir/);
  });

  it("rejects incomplete, tampered, unsafe, symlinked, or unlisted baseline entries", () => {
    const root = tempRoot();
    const checkout = join(root, "checkout");
    const wiki = join(checkout, "wiki");
    mkdirSync(wiki, { recursive: true });
    const release = makeRelease(join(root, "store"), "A");
    const brain = [
      "--brain-release-manifest",
      release.manifestPath,
      "--brain-release-dir",
      release.releaseDir,
    ];
    let attempt = 0;
    const invoke = (baseline: PublicBaselineFixture) => invokeBuildPublic(wiki, [
      "--public-dir",
      join(root, "promotion", `public-${attempt += 1}`),
      ...baselineArgs(baseline),
      ...brain,
    ]);

    const incomplete = makePublicBaseline(join(root, "baselines-incomplete"), {
      omit: ["assets/style.css"],
    });
    expect(() => invoke(incomplete)).toThrow(/missing required assets.*assets\/style\.css/);

    const omitted = makePublicBaseline(join(root, "baselines-omitted"));
    rmSync(join(omitted.root, "assets", "decl-index", "aa.json"));
    expect(() => invoke(omitted)).toThrow(/tree does not match its complete inventory/);

    const tampered = makePublicBaseline(join(root, "baselines-tampered"));
    const stylePath = join(tampered.root, "assets", "style.css");
    writeFileSync(stylePath, Buffer.alloc(readFileSync(stylePath).byteLength, 0x78));
    expect(() => invoke(tampered)).toThrow(/digest mismatch.*assets\/style\.css/);

    const traversal = makePublicBaseline(join(root, "baselines-traversal"), {
      manifestOnly: [{ path: "../escape", bytes: Buffer.from("escape") }],
    });
    expect(() => invoke(traversal)).toThrow(/not a normalized relative POSIX path/);

    const brainOwned = makePublicBaseline(join(root, "baselines-brain-owned"), {
      manifestOnly: [{ path: "brain.html", bytes: Buffer.from("not-release-coupled") }],
    });
    expect(() => invoke(brainOwned)).toThrow(/Brain-owned path must not enter/);

    const routeShadow = makePublicBaseline(join(root, "baselines-route-shadow"), {
      manifestOnly: [{ path: "about.html", bytes: Buffer.from("stale dynamic route") }],
    });
    expect(() => invoke(routeShadow)).toThrow(/route-shadowing or retired path/);

    const symlinked = makePublicBaseline(join(root, "baselines-symlinked"));
    const externalStyle = join(root, "external-style.css");
    writeFileSync(externalStyle, PUBLIC_BASELINE_CONTENTS["assets/style.css"]);
    rmSync(join(symlinked.root, "assets", "style.css"));
    symlinkSync(externalStyle, join(symlinked.root, "assets", "style.css"));
    expect(() => invoke(symlinked)).toThrow(/contains a symlink/);

    const unlisted = makePublicBaseline(join(root, "baselines-unlisted"));
    writeFileSync(join(unlisted.root, "unlisted.txt"), "not in manifest");
    expect(() => invoke(unlisted)).toThrow(/unlisted unlisted\.txt/);
  });

  it("rejects unsafe or stale explicit public directories without deleting them", () => {
    const root = tempRoot();
    const checkout = join(root, "checkout");
    const wiki = join(checkout, "wiki");
    const site = join(checkout, "site");
    mkdirSync(join(wiki, "assets"), { recursive: true });
    mkdirSync(join(site, "assets"), { recursive: true });
    mkdirSync(join(site, "out"), { recursive: true });
    mkdirSync(join(site, "annotations"), { recursive: true });
    const release = makeRelease(join(root, "store"), "A");
    const baseline = makePublicBaseline(join(root, "baselines"));
    const baseArgs = [
      ...baselineArgs(baseline),
      "--brain-release-manifest",
      release.manifestPath,
      "--brain-release-dir",
      release.releaseDir,
    ];
    const invoke = (args: string[]) => invokeBuildPublic(wiki, args);

    expect(() => invoke(["--public-dir", "relative-public", ...baseArgs]))
      .toThrow(/absolute path/);
    expect(() => invoke(["--public-dir", join(wiki, "promotion-public"), ...baseArgs]))
      .toThrow(/outside the repository checkout/);
    expect(() => invoke(["--public-dir", join(site, "out", "promotion-public"), ...baseArgs]))
      .toThrow(/outside the repository checkout/);

    const stale = join(root, "stale-public");
    mkdirSync(stale);
    writeFileSync(join(stale, "keep.txt"), "do not delete");
    expect(() => invoke(["--public-dir", stale, ...baseArgs]))
      .toThrow(/absent or empty/);
    expect(readFileSync(join(stale, "keep.txt"), "utf8")).toBe("do not delete");

    const real = join(root, "real-public");
    const linked = join(root, "linked-public");
    mkdirSync(real);
    symlinkSync(real, linked, "dir");
    expect(() => invoke(["--public-dir", linked, ...baseArgs]))
      .toThrow(/must not be a symlink/);
  });
});
