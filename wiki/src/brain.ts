// Shared BRAIN asset plumbing + the community-edit node-existence oracle.
//
// BRAIN data is selected once per request through /assets/brain/current.json.
// Every release-derived read then uses the resulting immutable namespace; the
// selector itself is deliberately never isolate-cached.
import type { Context } from "hono";
import type { Env } from "./env.js";
import { atomIdForOrgan } from "./brain-api.js";

// Interior spaces are legal (lit anchors like "lit:2110.15741#Theorem 2");
// only control chars and blank/overlong ids are rejected.
export const BRAIN_ID_RE = /^(?!\s*$)[^\p{C}]{1,400}$/u;

const RELEASE_ID_RE = /^sha256:([0-9a-f]{64})$/;
const RELEASE_HEX_RE = /^[0-9a-f]{64}$/;
const DIGEST_RE = /^[0-9a-f]{64}$/;
const GIT_COMMIT_RE = /^[0-9a-f]{40}$/;
const SELECTOR_PATH = "/assets/brain/current.json";
const RELEASE_KEYS = new Set([
  "schema",
  "profile",
  "release_id",
  "authority",
  "source_set_root",
  "semantic_epoch",
  "reducer",
  "artifacts",
  "attestations",
  "compatible_overlay_generation_ids",
  "created_at",
]);
const ARTIFACT_KEYS = new Set([
  "logical_name",
  "path",
  "uri",
  "media_type",
  "sha256",
  "bytes",
  "logical_format",
  "logical_root",
]);
const REQUIRED_RUNTIME_ARTIFACTS = new Set([
  "site/assets/brain/sources.json",
  "site/assets/brain/xref_index.json",
  "site/assets/brain/cells/manifest.json",
  "site/assets/brain/cells/aliases.json",
  "site/assets/brain/cells/labels.json",
  "site/assets/brain/cells/supercells.json",
  "site/assets/brain/cells/explorer.json",
  "site/assets/brain/cells/frontier_graph.json",
  "site/out/brain.html",
]);
const SELECTOR_KEYS = new Set([
  "schema",
  "release_id",
  "release",
  "manifest_sha256",
  "manifest",
  "previous_release_id",
  "previous_release",
  "previous_manifest_sha256",
  "previous_manifest",
  "audited_at",
]);
const LEGACY_SELECTOR_KEYS = new Set(
  [...SELECTOR_KEYS].filter((key) => !key.includes("manifest_sha256")),
);

export interface BrainReleaseSelector {
  schema: "wikilean.release-selector/v2";
  release_id: string;
  release: string;
  manifest_sha256: string;
  manifest: string;
  previous_release_id?: string;
  previous_release?: string;
  previous_manifest_sha256?: string;
  previous_manifest?: string;
  audited_at?: string;
}

interface LegacyBrainReleaseSelector {
  schema: "wikilean.release-selector/v1";
  release_id: string;
  release: string;
  manifest: string;
  previous_release_id?: string;
  previous_release?: string;
  previous_manifest?: string;
  audited_at?: string;
}

type ReadableBrainReleaseSelector = BrainReleaseSelector | LegacyBrainReleaseSelector;

interface BrainReleaseManifestCommon {
  schema: "wikilean.release/v1";
  release_id: string;
  artifacts: BrainReleaseArtifact[];
  attestations: unknown[];
}

export interface BrainReplayBinding {
  authority_root: string;
  offline_pack_id: string;
  reducer_inventory_id: string;
  generation_id: string;
  prior_state_root: string | null;
}

export type BrainReleaseManifest = BrainReleaseManifestCommon & (
  | { profile: "brain-current-v1"; replay?: never }
  | { profile: "brain-offline-replay-v1"; replay: BrainReplayBinding }
);

export interface BrainReleaseArtifact {
  logical_name: string;
  path: string;
  uri?: string | null;
  media_type: string;
  sha256: string;
  bytes: number;
  logical_format: "json" | "jsonl-rowset" | "opaque";
  logical_root: string | null;
}

export interface BrainReleaseContext {
  releaseId: string;
  release: string;
  manifestSha256: string;
  assetBase: string;
  manifestPath: string;
  manifest: BrainReleaseManifest;
  artifactPaths: ReadonlySet<string>;
  artifactsByPath: ReadonlyMap<string, BrainReleaseArtifact>;
}

type Ctx = Context<{ Bindings: Env }>;

function record(v: unknown): Record<string, unknown> | null {
  return typeof v === "object" && v !== null && !Array.isArray(v)
    ? (v as Record<string, unknown>)
    : null;
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  const o = record(value);
  if (o) {
    return `{${Object.keys(o).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(o[key])}`).join(",")}}`;
  }
  throw new TypeError("release identity contains an unsupported JSON value");
}

async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function sha256Buffer(value: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", value);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function releaseIdentity(value: Record<string, unknown>): Promise<string> {
  const identityValue = { ...value };
  delete identityValue.release_id;
  delete identityValue.attestations;
  delete identityValue.created_at;
  const canonical = canonicalJson(identityValue);
  return `sha256:${await sha256Hex(`wikilean\0wikilean.release.v1\0canonical-json-v1\0${canonical}`)}`;
}

function validRelativePath(path: unknown): path is string {
  return (
    typeof path === "string" &&
    path.length > 0 &&
    !path.startsWith("/") &&
    !path.includes("\\") &&
    !path.includes("\0") &&
    !/(?:^|\/)\.{1,2}(?:\/|$)/.test(path)
  );
}

function validHash(value: unknown): value is string {
  return typeof value === "string" && RELEASE_ID_RE.test(value);
}

function validDigest(value: unknown): value is string {
  return typeof value === "string" && DIGEST_RE.test(value);
}

function validAttestations(value: unknown, exactPair: boolean): boolean {
  if (!Array.isArray(value) || value.length === 0 || (exactPair && value.length !== 2)) {
    return false;
  }
  const paths: string[] = [];
  const kinds = new Set<string>();
  for (const entry of value) {
    const ref = record(entry);
    if (!ref || Object.keys(ref).length !== 4 ||
        Object.keys(ref).some((key) => !["kind", "path", "sha256", "bytes"].includes(key)) ||
        (ref.kind !== "build" && ref.kind !== "validation") ||
        !validRelativePath(ref.path) || ref.path.split("/").some((part) => part.length === 0) ||
        !validDigest(ref.sha256) || !Number.isSafeInteger(ref.bytes) || (ref.bytes as number) < 0) return false;
    paths.push(ref.path);
    kinds.add(ref.kind);
  }
  return kinds.size === 2 && paths.every(
    (path, index) => index === 0 || paths[index - 1] < path,
  );
}

function validReleaseProfile(o: Record<string, unknown>): boolean {
  if (o.profile === "brain-current-v1") {
    return !("replay" in o) && validAttestations(o.attestations, false);
  }
  if (o.profile !== "brain-offline-replay-v1") return false;
  const replay = record(o.replay);
  const hashes = ["authority_root", "offline_pack_id", "reducer_inventory_id", "generation_id"];
  return validAttestations(o.attestations, true) && replay !== null && Object.keys(replay).length === 5 &&
    Object.keys(replay).every((key) => key === "prior_state_root" || hashes.includes(key)) &&
    hashes.every((key) => validHash(replay[key])) &&
    (replay.prior_state_root === null || validHash(replay.prior_state_root));
}

function validReleaseMetadata(o: Record<string, unknown>): boolean {
  const authority = record(o.authority);
  const reducer = record(o.reducer);
  return (
    authority !== null &&
    Object.keys(authority).every((key) => ["git_commit", "semantic_state_root", "through_changeset"].includes(key)) &&
    GIT_COMMIT_RE.test(String(authority.git_commit ?? "")) &&
    validHash(authority.semantic_state_root) &&
    (authority.through_changeset === undefined || authority.through_changeset === null) &&
    validHash(o.source_set_root) &&
    typeof o.semantic_epoch === "string" &&
    /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(o.semantic_epoch) &&
    reducer !== null &&
    Object.keys(reducer).every((key) => [
      "schedule", "version", "git_commit", "configuration_sha256", "environment_sha256",
    ].includes(key)) &&
    typeof reducer.schedule === "string" && reducer.schedule.length > 0 &&
    typeof reducer.version === "string" && reducer.version.length > 0 &&
    GIT_COMMIT_RE.test(String(reducer.git_commit ?? "")) &&
    validDigest(reducer.configuration_sha256) &&
    validDigest(reducer.environment_sha256) &&
    Array.isArray(o.compatible_overlay_generation_ids) &&
    o.compatible_overlay_generation_ids.every((value) => typeof value === "string" && value.length > 0) &&
    (o.created_at === undefined || (typeof o.created_at === "string" && o.created_at.length > 0))
  );
}

function releaseParts(releaseId: unknown, release: unknown): { id: string; hex: string } | null {
  if (typeof releaseId !== "string" || typeof release !== "string") return null;
  const match = RELEASE_ID_RE.exec(releaseId);
  if (!match || !RELEASE_HEX_RE.test(release) || match[1] !== release) return null;
  return { id: releaseId, hex: release };
}

export async function parseBrainReleaseSelector(
  raw: unknown,
): Promise<ReadableBrainReleaseSelector | null> {
  const o = record(raw);
  if (!o) return null;
  const v2 = o.schema === "wikilean.release-selector/v2";
  const v1 = o.schema === "wikilean.release-selector/v1";
  if (!v1 && !v2) return null;
  const allowedKeys = v2 ? SELECTOR_KEYS : LEGACY_SELECTOR_KEYS;
  if (Object.keys(o).some((key) => !allowedKeys.has(key))) return null;
  const current = releaseParts(o.release_id, o.release);
  const currentNamespace = v2 ? o.manifest_sha256 : current?.hex;
  if (!current || !validDigest(currentNamespace) ||
      o.manifest !== `/assets/brain/releases/${currentNamespace}/release.json`) return null;
  const previousValues = [
    o.previous_release_id,
    o.previous_release,
    ...(v2 ? [o.previous_manifest_sha256] : []),
    o.previous_manifest,
  ];
  const hasPrevious = previousValues.some((value) => value !== undefined);
  let previous: { id: string; hex: string } | null = null;
  if (hasPrevious) {
    if (previousValues.some((value) => value === undefined)) return null;
    previous = releaseParts(o.previous_release_id, o.previous_release);
    const previousNamespace = v2 ? o.previous_manifest_sha256 : previous?.hex;
    if (
      !previous ||
      !validDigest(previousNamespace) ||
      o.previous_manifest !== `/assets/brain/releases/${previousNamespace}/release.json` ||
      (v2 ? previousNamespace === currentNamespace : previous.id === current.id)
    ) return null;
  }
  if (o.audited_at !== undefined && (typeof o.audited_at !== "string" || !o.audited_at)) return null;
  const common = {
    release_id: current.id,
    release: current.hex,
    manifest: o.manifest as string,
    ...(previous ? {
      previous_release_id: previous.id,
      previous_release: previous.hex,
      previous_manifest: o.previous_manifest as string,
    } : {}),
    ...(o.audited_at === undefined ? {} : { audited_at: o.audited_at as string }),
  };
  if (v2) {
    return {
      schema: "wikilean.release-selector/v2",
      ...common,
      manifest_sha256: o.manifest_sha256 as string,
      ...(previous
        ? { previous_manifest_sha256: o.previous_manifest_sha256 as string }
        : {}),
    };
  }
  return { schema: "wikilean.release-selector/v1", ...common };
}

async function parseReleaseManifest(
  raw: unknown,
  releaseId: string,
): Promise<{
  manifest: BrainReleaseManifest;
  artifactPaths: ReadonlySet<string>;
  artifactsByPath: ReadonlyMap<string, BrainReleaseArtifact>;
} | null> {
  const o = record(raw);
  if (
    !o ||
    Object.keys(o).some((key) => !RELEASE_KEYS.has(key) && !(key === "replay" && o.profile === "brain-offline-replay-v1")) ||
    o.schema !== "wikilean.release/v1" ||
    !validReleaseProfile(o) ||
    o.release_id !== releaseId ||
    !Array.isArray(o.artifacts) ||
    !Array.isArray(o.attestations) ||
    !validReleaseMetadata(o)
  ) {
    return null;
  }
  if (await releaseIdentity(o) !== releaseId) return null;

  const artifactPaths = new Set<string>();
  const logicalNames = new Set<string>();
  const artifacts: BrainReleaseArtifact[] = [];
  for (const rawArtifact of o.artifacts) {
    const artifact = record(rawArtifact);
    if (
      !artifact ||
      Object.keys(artifact).some((key) => !ARTIFACT_KEYS.has(key)) ||
      typeof artifact.logical_name !== "string" ||
      !/^[a-z][a-z0-9_.-]{0,127}$/.test(artifact.logical_name) ||
      !validRelativePath(artifact.path) ||
      (artifact.uri !== undefined && artifact.uri !== null && typeof artifact.uri !== "string") ||
      typeof artifact.media_type !== "string" ||
      !/^[^/\s]+\/[^/\s]+$/.test(artifact.media_type) ||
      !validDigest(artifact.sha256) ||
      !Number.isSafeInteger(artifact.bytes) ||
      (artifact.bytes as number) < 0 ||
      !["json", "jsonl-rowset", "opaque"].includes(String(artifact.logical_format)) ||
      !(artifact.logical_root === null || validHash(artifact.logical_root)) ||
      artifactPaths.has(artifact.path) ||
      logicalNames.has(artifact.logical_name)
    ) {
      return null;
    }
    artifactPaths.add(artifact.path);
    logicalNames.add(artifact.logical_name);
    if (
      artifact.path.startsWith("site/assets/brain/") &&
      (artifact.media_type !== "application/json" || artifact.logical_format !== "json" || !validHash(artifact.logical_root))
    ) return null;
    if (
      artifact.path === "site/out/brain.html" &&
      (artifact.media_type !== "text/html" || artifact.logical_format !== "opaque" || artifact.logical_root !== null)
    ) return null;
    artifacts.push(artifact as unknown as BrainReleaseArtifact);
  }
  if ([...REQUIRED_RUNTIME_ARTIFACTS].some((path) => !artifactPaths.has(path))) return null;
  return {
    manifest: { ...o, artifacts } as unknown as BrainReleaseManifest,
    artifactPaths,
    artifactsByPath: new Map(artifacts.map((artifact) => [artifact.path, artifact])),
  };
}

// Shared by non-Brain assets such as declaration and premise indexes.
export async function assetJson<T>(c: Ctx, path: string): Promise<T | null> {
  const res = await c.env.ASSETS.fetch(new Request(new URL(path, c.req.url)));
  if (!res.ok) return null;
  return (await res.json()) as T;
}

async function exactAssetJson<T>(
  c: Ctx,
  path: string,
  expectedSha256: string,
): Promise<T | null> {
  const loaded = await assetJsonWithDigest<T>(c, path);
  if (loaded === null) return null;
  if (loaded.sha256 !== expectedSha256) {
    throw new TypeError(`asset bytes do not match selector digest: ${path}`);
  }
  return loaded.value;
}

async function assetJsonWithDigest<T>(
  c: Ctx,
  path: string,
): Promise<{ value: T; sha256: string } | null> {
  const res = await c.env.ASSETS.fetch(new Request(new URL(path, c.req.url)));
  if (!res.ok) return null;
  const bytes = await res.arrayBuffer();
  const sha256 = await sha256Buffer(bytes);
  const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes);
  return { value: JSON.parse(text) as T, sha256 };
}

// Isolate-lifetime memo for parsed immutable assets. Brain keys include the
// logical release and exact manifest digest; unrelated indexes keep their
// historical path-only keys. Brain
// release data is bounded to the selector's current/previous overlap window;
// failed loads are removed so transient failures remain retryable.
const _assetMemo = new Map<string, Promise<unknown>>();
const _releaseMemo = new Map<string, Promise<BrainReleaseContext | null>>();
const MAX_MEMOIZED_BRAIN_RELEASES = 2;
const _brainReleaseLru = new Map<string, string>();
const _brainReleaseEvictors = new Set<(releaseId: string) => void>();

function evictBrainRelease(cacheIdentity: string, releaseId: string): void {
  _releaseMemo.delete(cacheIdentity);
  const prefix = `brain:${cacheIdentity}:`;
  for (const key of _assetMemo.keys()) {
    if (key.startsWith(prefix)) _assetMemo.delete(key);
  }
  for (const evict of _brainReleaseEvictors) evict(releaseId);
}

// brain-edits registers its release-keyed derived indexes here so every Brain
// cache observes one overlap window rather than retaining a different pair.
export function registerBrainReleaseCacheEvictor(evict: (releaseId: string) => void): void {
  _brainReleaseEvictors.add(evict);
}

function touchBrainRelease(cacheIdentity: string, releaseId: string): void {
  _brainReleaseLru.delete(cacheIdentity);
  _brainReleaseLru.set(cacheIdentity, releaseId);
  while (_brainReleaseLru.size > MAX_MEMOIZED_BRAIN_RELEASES) {
    const oldest = _brainReleaseLru.entries().next().value as [string, string] | undefined;
    if (oldest === undefined) break;
    _brainReleaseLru.delete(oldest[0]);
    evictBrainRelease(oldest[0], oldest[1]);
  }
}

export function memoAssetJson<T>(c: Ctx, path: string): Promise<T | null> {
  return memoJson<T>(c, path, path);
}

function memoJson<T>(c: Ctx, key: string, path: string): Promise<T | null> {
  const hit = _assetMemo.get(key);
  if (hit) return hit as Promise<T | null>;
  let pending: Promise<T | null>;
  pending = assetJson<T>(c, path).then(
    (value) => {
      if (value === null && _assetMemo.get(key) === pending) _assetMemo.delete(key);
      return value;
    },
    (error) => {
      if (_assetMemo.get(key) === pending) _assetMemo.delete(key);
      throw error;
    },
  );
  _assetMemo.set(key, pending);
  return pending;
}

function memoExactJson<T>(
  c: Ctx,
  key: string,
  path: string,
  expectedSha256: string,
): Promise<T | null> {
  const hit = _assetMemo.get(key);
  if (hit) return hit as Promise<T | null>;
  let pending: Promise<T | null>;
  pending = exactAssetJson<T>(c, path, expectedSha256).then(
    (value) => {
      if (value === null && _assetMemo.get(key) === pending) _assetMemo.delete(key);
      return value;
    },
    (error) => {
      if (_assetMemo.get(key) === pending) _assetMemo.delete(key);
      throw error;
    },
  );
  _assetMemo.set(key, pending);
  return pending;
}

function immutableRelativePath(path: string): string | null {
  if (!path || path.startsWith("/") || path.includes("\\") || /(?:^|\/)\.{1,2}(?:\/|$)/.test(path)) return null;
  return path;
}

export function brainAssetPath(release: BrainReleaseContext, path: string): string {
  const relative = immutableRelativePath(path);
  if (!relative) throw new Error(`invalid Brain asset path: ${path}`);
  return `${release.assetBase}/${relative}`;
}

export function brainAssetJson<T>(c: Ctx, release: BrainReleaseContext, path: string): Promise<T | null> {
  const relative = immutableRelativePath(path);
  if (!relative) return Promise.resolve(null);
  const artifactPath = sourceArtifactPath(relative);
  const artifact = artifactPath ? release.artifactsByPath.get(artifactPath) : undefined;
  if (!artifact) return Promise.resolve(null);
  const cacheIdentity = `${release.releaseId}:${release.manifestSha256}`;
  touchBrainRelease(cacheIdentity, release.releaseId);
  const key = `brain:${cacheIdentity}:${relative}`;
  const hit = _assetMemo.get(key);
  if (hit) return hit as Promise<T | null>;
  let pending: Promise<T | null>;
  pending = (async (): Promise<T | null> => {
    const response = await c.env.ASSETS.fetch(
      new Request(new URL(`${release.assetBase}/${relative}`, c.req.url)),
    );
    if (!response.ok) return null;
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength !== artifact.bytes || await sha256Buffer(bytes) !== artifact.sha256) {
      throw new BrainReleaseUnavailableError(
        release.releaseId,
        relative,
        "asset bytes do not match the signed inventory",
      );
    }
    return JSON.parse(
      new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(bytes),
    ) as T;
  })().then(
    (value) => {
      if (value === null && _assetMemo.get(key) === pending) _assetMemo.delete(key);
      return value;
    },
    (error) => {
      if (_assetMemo.get(key) === pending) _assetMemo.delete(key);
      throw error;
    },
  );
  _assetMemo.set(key, pending);
  return pending;
}

export class BrainReleaseUnavailableError extends Error {
  readonly releaseId: string;
  readonly assetPath: string;

  constructor(releaseId: string, assetPath: string, reason: string) {
    super(`Brain release ${releaseId} unavailable: ${reason} (${assetPath})`);
    this.name = "BrainReleaseUnavailableError";
    this.releaseId = releaseId;
    this.assetPath = assetPath;
  }
}

export function isBrainReleaseUnavailableError(error: unknown): error is BrainReleaseUnavailableError {
  return error instanceof BrainReleaseUnavailableError;
}

function sourceArtifactPath(path: string): string | null {
  const relative = immutableRelativePath(path);
  return relative ? `site/assets/brain/${relative}` : null;
}

export async function requiredBrainAssetJson<T>(
  c: Ctx,
  release: BrainReleaseContext,
  path: string,
): Promise<T> {
  const artifactPath = sourceArtifactPath(path);
  if (!artifactPath || !release.artifactPaths.has(artifactPath)) {
    throw new BrainReleaseUnavailableError(release.releaseId, path, "asset is absent from the signed inventory");
  }
  try {
    const value = await brainAssetJson<T>(c, release, path);
    if (value === null) {
      throw new BrainReleaseUnavailableError(release.releaseId, path, "declared asset could not be loaded");
    }
    return value;
  } catch (error) {
    if (isBrainReleaseUnavailableError(error)) throw error;
    throw new BrainReleaseUnavailableError(
      release.releaseId,
      path,
      `declared asset is not valid JSON: ${String(error)}`,
    );
  }
}

// Resolve current once at each HTTP entry point. The selector is fetched fresh;
// its immutable manifest is memoized only under the selector's exact byte
// identity. Logical release IDs intentionally exclude attestations and time.
export async function resolveBrainRelease(c: Ctx): Promise<BrainReleaseContext | null> {
  try {
    const selector = await parseBrainReleaseSelector(await assetJson<unknown>(c, SELECTOR_PATH));
    if (!selector) return null;
    const legacyManifest = selector.schema === "wikilean.release-selector/v1"
      ? await assetJsonWithDigest<unknown>(c, selector.manifest)
      : null;
    if (selector.schema === "wikilean.release-selector/v1" && legacyManifest === null) return null;
    const manifestSha256 = selector.schema === "wikilean.release-selector/v2"
      ? selector.manifest_sha256
      : legacyManifest!.sha256;
    const cacheIdentity = `${selector.release_id}:${manifestSha256}`;
    touchBrainRelease(cacheIdentity, selector.release_id);
    let hit = _releaseMemo.get(cacheIdentity);
    if (!hit) {
      hit = (async () => {
        const manifestKey = `brain:${cacheIdentity}:release.json`;
        const manifestPromise = selector.schema === "wikilean.release-selector/v2"
          ? memoExactJson<unknown>(
            c,
            manifestKey,
            selector.manifest,
            selector.manifest_sha256,
          )
          : Promise.resolve(legacyManifest!.value);
        const rawManifest = await manifestPromise;
        let parsed: Awaited<ReturnType<typeof parseReleaseManifest>>;
        try {
          parsed = await parseReleaseManifest(rawManifest, selector.release_id);
        } catch (error) {
          if (_assetMemo.get(manifestKey) === manifestPromise) _assetMemo.delete(manifestKey);
          throw error;
        }
        if (!parsed) {
          if (_assetMemo.get(manifestKey) === manifestPromise) _assetMemo.delete(manifestKey);
          return null;
        }
        return {
          releaseId: selector.release_id,
          release: selector.release,
          manifestSha256,
          assetBase: selector.manifest.slice(0, -"/release.json".length),
          manifestPath: selector.manifest,
          manifest: parsed.manifest,
          artifactPaths: parsed.artifactPaths,
          artifactsByPath: parsed.artifactsByPath,
        };
      })();
      _releaseMemo.set(cacheIdentity, hit);
      hit.then(
        (value) => {
          if (value === null && _releaseMemo.get(cacheIdentity) === hit) {
            _releaseMemo.delete(cacheIdentity);
          }
        },
        () => {
          if (_releaseMemo.get(cacheIdentity) === hit) _releaseMemo.delete(cacheIdentity);
        },
      );
    }
    return await hit;
  } catch {
    return null;
  }
}

// test-only (mirrors brain-edits' _resetBrainEditCaches)
export function _resetBrainAssetMemo(): void {
  for (const [cacheIdentity, releaseId] of _brainReleaseLru) {
    evictBrainRelease(cacheIdentity, releaseId);
  }
  _brainReleaseLru.clear();
  _assetMemo.clear();
  _releaseMemo.clear();
}

// One labels.json row. The v3 cell index ships `{id, label, f?, aka?, p?}`.
export interface BrainLabelRow {
  id: string;
  label: string;
  aka?: string[];
  p?: string;
  type?: string;
  slug?: string;
  status?: string;
  n_decls?: number;
  f?: number;
}

export function searchLabels(
  labels: BrainLabelRow[],
  q: string,
  type: string,
  limit: number,
): BrainLabelRow[] {
  const isQid = /^q[1-9][0-9]{0,11}$/.test(q);
  const starts: BrainLabelRow[] = [], contains: BrainLabelRow[] = [];
  for (const r of labels) {
    if (type && r.type !== type) continue;
    const names = [(r.label || "").toLowerCase(), ...(r.aka ?? []).map((a) => a.toLowerCase())];
    if (names.some((n) => n.startsWith(q)) || (isQid && r.id.toLowerCase() === q)) starts.push(r);
    else if (names.some((n) => n.includes(q))) contains.push(r);
    if (starts.length >= limit) break;
  }
  return [...starts, ...contains].slice(0, limit);
}

// Strict node-existence oracle used by community edits. The caller supplies the
// request's already-resolved release so validation cannot mix releases.
export async function brainNodeExists(c: Ctx, release: BrainReleaseContext, id: string): Promise<boolean> {
  if (!BRAIN_ID_RE.test(id)) return false;
  return (await atomIdForOrgan(c, release, id)) !== null;
}
