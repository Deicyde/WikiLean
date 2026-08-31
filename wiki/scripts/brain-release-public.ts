import {
  closeSync,
  constants,
  existsSync,
  fstatSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readSync,
  readdirSync,
  renameSync,
  rmSync,
  statfsSync,
  writeSync,
} from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { basename, dirname, join, posix, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";

const RELEASE_ID_RE = /^sha256:([0-9a-f]{64})$/;
const DIGEST_RE = /^[0-9a-f]{64}$/;
const GIT_COMMIT_RE = /^[0-9a-f]{40}$/;
const EPOCH_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/;
const SELECTOR_SCHEMA = "wikilean.release-selector/v1";
const RELEASE_SCHEMA = "wikilean.release/v1";
const RELEASE_PROFILE = "brain-current-v1";
const STATIC_PREFIX = "site/assets/brain/";
const CELLS_PREFIX = `${STATIC_PREFIX}cells/`;
const REQUIRED_RELEASE_PATHS = new Set([
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
  `${STATIC_PREFIX}sources.json`,
  `${STATIC_PREFIX}xref_index.json`,
  `${CELLS_PREFIX}manifest.json`,
  `${CELLS_PREFIX}aliases.json`,
  `${CELLS_PREFIX}labels.json`,
  `${CELLS_PREFIX}supercells.json`,
  `${CELLS_PREFIX}explorer.json`,
  `${CELLS_PREFIX}frontier_graph.json`,
  "site/out/brain.html",
]);
const REQUIRED_PUBLIC_PATHS = new Set([
  `${STATIC_PREFIX}sources.json`,
  `${STATIC_PREFIX}xref_index.json`,
  `${CELLS_PREFIX}manifest.json`,
  `${CELLS_PREFIX}aliases.json`,
  `${CELLS_PREFIX}labels.json`,
  `${CELLS_PREFIX}supercells.json`,
  `${CELLS_PREFIX}explorer.json`,
  `${CELLS_PREFIX}frontier_graph.json`,
]);

export const DEFAULT_MAX_OBJECTS = 20_000;
export const DEFAULT_MAX_BYTES = 512 * 1024 * 1024;
export const DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024;
export const DEFAULT_MIN_FREE_BYTES = 256 * 1024 * 1024;
export const COPY_BUFFER_BYTES = 1024 * 1024;
const MAX_SELECTOR_BYTES = 64 * 1024;
const MAX_RELATIVE_PATH_BYTES = 4096;
const MAX_PATH_COMPONENTS = 64;

interface ReleaseArtifact {
  logical_name: string;
  path: string;
  media_type: string;
  sha256: string;
  bytes: number;
  logical_format: "json" | "jsonl-rowset" | "opaque";
  logical_root: string | null;
  uri?: string | null;
}

interface AttestationRef {
  kind: "build" | "validation";
  path: string;
  sha256: string;
  bytes: number;
}

interface ReleaseManifest {
  schema: typeof RELEASE_SCHEMA;
  profile: typeof RELEASE_PROFILE;
  release_id: string;
  authority: {
    git_commit: string;
    semantic_state_root: string;
    through_changeset?: null;
  };
  source_set_root: string;
  semantic_epoch: string;
  reducer: {
    schedule: string;
    version: string;
    git_commit: string;
    configuration_sha256: string;
    environment_sha256: string;
  };
  artifacts: ReleaseArtifact[];
  attestations: AttestationRef[];
  compatible_overlay_generation_ids: string[];
  created_at?: string;
}

export interface BrainReleaseSelector {
  schema: typeof SELECTOR_SCHEMA;
  release_id: string;
  release: string;
  manifest: string;
  previous_release_id?: string;
  previous_release?: string;
  previous_manifest?: string;
  audited_at?: string;
}

export interface StageBrainPublicOptions {
  manifestPath: string;
  releaseDir: string;
  destination: string;
  previousManifestPath?: string;
  previousReleaseDir?: string;
  brainPageDestination?: string;
  maxObjects?: number;
  maxBytes?: number;
  maxFileBytes?: number;
  minFreeBytes?: number;
  auditedAt?: string;
}

export interface StageBrainPublicResult {
  schema: "wikilean.public-stage-result/v1";
  release_id: string;
  release: string;
  previous_release_id: string | null;
  retained_release_ids: string[];
  destination: string;
  objects: number;
  bytes: number;
  largest_file_bytes: number;
  copy_buffer_bytes: number;
  duration_ms: number;
  max_rss_bytes: number;
  free_bytes_before: number;
  free_bytes_after: number;
  brain_page: { destination: string; bytes: number; sha256: string } | null;
  warnings: string[];
}

interface NamespaceFile {
  destinationPath: string;
  sourceRoot: string;
  sourcePath: string;
  sha256: string;
  bytes: number;
  label: string;
}

interface LoadedNamespace {
  releaseId: string;
  releaseHex: string;
  files: NamespaceFile[];
  brainPage: NamespaceFile | null;
}

interface TreeMeasurement {
  objects: number;
  bytes: number;
  largest: number;
}

interface ExpectedFile {
  bytes: number;
  sha256: string;
}

function fail(message: string): never {
  throw new Error(`Brain public staging: ${message}`);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function parseJsonObject(bytes: Buffer, label: string): Record<string, unknown> {
  let value: unknown;
  try {
    value = JSON.parse(bytes.toString("utf8"));
  } catch (error) {
    fail(`${label} is not valid JSON: ${String(error)}`);
  }
  if (!isRecord(value)) fail(`${label} must be a JSON object`);
  return value;
}

function releaseHex(releaseId: unknown, label: string): string {
  if (typeof releaseId !== "string") fail(`${label} must be a string`);
  const match = RELEASE_ID_RE.exec(releaseId);
  if (!match) fail(`${label} must be sha256 followed by 64 lowercase hexadecimal characters`);
  return match[1];
}

function safeRelativePath(raw: unknown, label: string): string {
  if (typeof raw !== "string" || !raw || raw.includes("\\") || raw.includes("\0")) {
    fail(`${label} is not a safe relative path`);
  }
  if (Buffer.byteLength(raw, "utf8") > MAX_RELATIVE_PATH_BYTES || raw.split("/").length > MAX_PATH_COMPONENTS) {
    fail(`${label} exceeds the supported path length or depth`);
  }
  if (posix.isAbsolute(raw) || posix.normalize(raw) !== raw || raw === "." || raw.startsWith("../")) {
    fail(`${label} is not a normalized relative path`);
  }
  return raw;
}

function publicPathForArtifact(artifactPath: string): string | null {
  if (artifactPath === `${STATIC_PREFIX}sources.json`) return "sources.json";
  if (artifactPath === `${STATIC_PREFIX}xref_index.json`) return "xref_index.json";
  if (artifactPath.startsWith(CELLS_PREFIX)) {
    return `cells/${artifactPath.slice(CELLS_PREFIX.length)}`;
  }
  return null;
}

function sha256(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") return JSON.stringify(value);
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  fail("release identity contains an unsupported JSON value");
}

function domainIdentity(domain: string, value: Record<string, unknown>, excluded: string[]): string {
  const identityValue = { ...value };
  for (const key of excluded) delete identityValue[key];
  const payload = Buffer.from(
    `wikilean\0${domain}\0canonical-json-v1\0${canonicalJson(identityValue)}`,
    "utf8",
  );
  return `sha256:${sha256(payload)}`;
}

function exactKeys(
  value: Record<string, unknown>,
  label: string,
  required: readonly string[],
  optional: readonly string[] = [],
): void {
  const requiredSet = new Set(required);
  const allowed = new Set([...required, ...optional]);
  const missing = required.filter(key => !Object.prototype.hasOwnProperty.call(value, key));
  if (missing.length) fail(`${label} is missing required fields: ${missing.join(", ")}`);
  const unknown = Object.keys(value).filter(key => !allowed.has(key));
  if (unknown.length) fail(`${label} has unknown fields: ${unknown.join(", ")}`);
  if (requiredSet.size !== required.length) fail(`${label} validator has duplicate required fields`);
}

function nonEmptyString(value: unknown, label: string): string {
  if (typeof value !== "string" || !value) fail(`${label} must be a non-empty string`);
  return value;
}

function digestValue(value: unknown, label: string): string {
  if (typeof value !== "string" || !DIGEST_RE.test(value)) {
    fail(`${label} must be 64 lowercase SHA-256 hexadecimal characters`);
  }
  return value;
}

function hashValue(value: unknown, label: string): string {
  releaseHex(value, label);
  return value as string;
}

function safeInteger(value: unknown, label: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    fail(`${label} must be a non-negative safe integer`);
  }
  return value as number;
}

function artifactContract(path: string): { mediaType: string; logicalFormat: ReleaseArtifact["logical_format"] } {
  if (path.endsWith(".jsonl")) return { mediaType: "application/x-ndjson", logicalFormat: "jsonl-rowset" };
  if (path.endsWith(".json")) return { mediaType: "application/json", logicalFormat: "json" };
  if (path.endsWith(".html")) return { mediaType: "text/html", logicalFormat: "opaque" };
  if (path.endsWith(".sqlite3")) return { mediaType: "application/vnd.sqlite3", logicalFormat: "opaque" };
  fail(`${RELEASE_PROFILE} does not support artifact path ${path}`);
}

function assertRegularFile(path: string, label: string): void {
  const info = lstatSync(path);
  if (info.isSymbolicLink()) fail(`${label} must not be a symlink`);
  if (!info.isFile()) fail(`${label} must be a regular file`);
}

function assertNoSymlinkComponents(root: string, relativePath: string, label: string): string {
  let current = resolve(root);
  for (const component of relativePath.split("/")) {
    current = join(current, component);
    const info = lstatSync(current);
    if (info.isSymbolicLink()) fail(`${label} must not traverse a symlink`);
  }
  return current;
}

function resolveContained(root: string, relativePath: string): string {
  const path = resolve(root, ...relativePath.split("/"));
  const rel = relative(root, path);
  if (!rel || rel === ".." || rel.startsWith(`..${sep}`) || resolve(path) === resolve(root)) {
    fail(`artifact path escapes the release root: ${relativePath}`);
  }
  return path;
}

function parseReleaseManifest(bytes: Buffer, label: string): ReleaseManifest {
  const raw = parseJsonObject(bytes, label);
  exactKeys(
    raw,
    label,
    [
      "schema", "profile", "release_id", "authority", "source_set_root", "semantic_epoch",
      "reducer", "artifacts", "attestations", "compatible_overlay_generation_ids",
    ],
    ["created_at"],
  );
  if (raw.schema !== RELEASE_SCHEMA) fail(`${label} has unsupported schema`);
  if (raw.profile !== RELEASE_PROFILE) fail(`${label} has unsupported profile`);
  hashValue(raw.release_id, `${label}.release_id`);

  if (!isRecord(raw.authority)) fail(`${label}.authority must be an object`);
  exactKeys(raw.authority, `${label}.authority`, ["git_commit", "semantic_state_root"], ["through_changeset"]);
  if (typeof raw.authority.git_commit !== "string" || !GIT_COMMIT_RE.test(raw.authority.git_commit)) {
    fail(`${label}.authority.git_commit must be a full lowercase Git commit`);
  }
  hashValue(raw.authority.semantic_state_root, `${label}.authority.semantic_state_root`);
  if (raw.authority.through_changeset !== undefined && raw.authority.through_changeset !== null) {
    fail(`${label}.authority.through_changeset is unsupported until accepted-changeset replay is implemented`);
  }
  hashValue(raw.source_set_root, `${label}.source_set_root`);
  if (typeof raw.semantic_epoch !== "string" || !EPOCH_RE.test(raw.semantic_epoch)) {
    fail(`${label}.semantic_epoch is invalid`);
  }

  if (!isRecord(raw.reducer)) fail(`${label}.reducer must be an object`);
  exactKeys(raw.reducer, `${label}.reducer`, [
    "schedule", "version", "git_commit", "configuration_sha256", "environment_sha256",
  ]);
  nonEmptyString(raw.reducer.schedule, `${label}.reducer.schedule`);
  nonEmptyString(raw.reducer.version, `${label}.reducer.version`);
  if (typeof raw.reducer.git_commit !== "string" || !GIT_COMMIT_RE.test(raw.reducer.git_commit)) {
    fail(`${label}.reducer.git_commit must be a full lowercase Git commit`);
  }
  digestValue(raw.reducer.configuration_sha256, `${label}.reducer.configuration_sha256`);
  digestValue(raw.reducer.environment_sha256, `${label}.reducer.environment_sha256`);

  if (!Array.isArray(raw.artifacts) || !raw.artifacts.length) {
    fail(`${label}.artifacts must be a non-empty array`);
  }

  const artifactPaths = new Set<string>();
  const logicalNames = new Set<string>();
  const artifacts: ReleaseArtifact[] = raw.artifacts.map((item, index) => {
    if (!isRecord(item)) fail(`${label}.artifacts[${index}] must be an object`);
    const itemLabel = `${label}.artifacts[${index}]`;
    exactKeys(
      item,
      itemLabel,
      ["logical_name", "path", "media_type", "sha256", "bytes", "logical_format", "logical_root"],
      ["uri"],
    );
    const logicalName = nonEmptyString(item.logical_name, `${itemLabel}.logical_name`);
    if (!/^[a-z][a-z0-9_.-]{0,127}$/.test(logicalName)) {
      fail(`${itemLabel}.logical_name is invalid`);
    }
    const path = safeRelativePath(item.path, `${itemLabel}.path`);
    const contract = artifactContract(path);
    if (item.media_type !== contract.mediaType) {
      fail(`${itemLabel}.media_type must be ${contract.mediaType} for ${path}`);
    }
    if (item.logical_format !== contract.logicalFormat) {
      fail(`${itemLabel}.logical_format must be ${contract.logicalFormat} for ${path}`);
    }
    const artifactDigest = digestValue(item.sha256, `${itemLabel}.sha256`);
    const artifactBytes = safeInteger(item.bytes, `${itemLabel}.bytes`);
    const logicalRoot = item.logical_root;
    if (contract.logicalFormat === "opaque") {
      if (logicalRoot !== null) fail(`${itemLabel}.logical_root must be null for an opaque artifact`);
    } else {
      hashValue(logicalRoot, `${itemLabel}.logical_root`);
    }
    if (item.uri !== undefined && item.uri !== null) nonEmptyString(item.uri, `${itemLabel}.uri`);
    if (artifactPaths.has(path) || logicalNames.has(logicalName)) {
      fail(`${itemLabel} duplicates an artifact path or logical_name`);
    }
    artifactPaths.add(path);
    logicalNames.add(logicalName);
    return {
      logical_name: logicalName,
      path,
      media_type: contract.mediaType,
      sha256: artifactDigest,
      bytes: artifactBytes,
      logical_format: contract.logicalFormat,
      logical_root: logicalRoot as string | null,
      ...(item.uri === undefined ? {} : { uri: item.uri as string | null }),
    };
  });
  const sortedArtifactPaths = [...artifactPaths].sort();
  if (artifacts.some((artifact, index) => artifact.path !== sortedArtifactPaths[index])) {
    fail(`${label}.artifacts must be sorted by path`);
  }
  const missingReleasePaths = [...REQUIRED_RELEASE_PATHS].filter(path => !artifactPaths.has(path)).sort();
  if (missingReleasePaths.length) {
    fail(`${label} is missing required release artifacts: ${missingReleasePaths.join(", ")}`);
  }
  if (![...artifactPaths].some(path => path.startsWith(CELLS_PREFIX) && !REQUIRED_RELEASE_PATHS.has(path))) {
    fail(`${label} requires at least one generated cell or trace shard`);
  }

  if (!Array.isArray(raw.attestations) || !raw.attestations.length) {
    fail(`${label}.attestations must be a non-empty array`);
  }
  const attestationPaths = new Set<string>();
  const attestationKinds = new Set<string>();
  const attestations: AttestationRef[] = raw.attestations.map((item, index) => {
    if (!isRecord(item)) fail(`${label}.attestations[${index}] must be an object`);
    const itemLabel = `${label}.attestations[${index}]`;
    exactKeys(item, itemLabel, ["kind", "path", "sha256", "bytes"]);
    if (item.kind !== "build" && item.kind !== "validation") fail(`${itemLabel}.kind is invalid`);
    const path = safeRelativePath(item.path, `${itemLabel}.path`);
    const attestationDigest = digestValue(item.sha256, `${itemLabel}.sha256`);
    const attestationBytes = safeInteger(item.bytes, `${itemLabel}.bytes`);
    if (attestationPaths.has(path)) fail(`${itemLabel}.path is duplicated`);
    attestationPaths.add(path);
    attestationKinds.add(item.kind);
    return { kind: item.kind, path, sha256: attestationDigest, bytes: attestationBytes };
  });
  if (attestationKinds.size !== 2 || !attestationKinds.has("build") || !attestationKinds.has("validation")) {
    fail(`${label}.attestations must contain build and validation entries`);
  }
  const sortedAttestationPaths = [...attestationPaths].sort();
  if (attestations.some((attestation, index) => attestation.path !== sortedAttestationPaths[index])) {
    fail(`${label}.attestations must be sorted by path`);
  }

  if (!Array.isArray(raw.compatible_overlay_generation_ids)) {
    fail(`${label}.compatible_overlay_generation_ids must be an array`);
  }
  const overlays = raw.compatible_overlay_generation_ids.map((value, index) =>
    nonEmptyString(value, `${label}.compatible_overlay_generation_ids[${index}]`));
  if (overlays.some((value, index) => value !== [...new Set(overlays)].sort()[index])) {
    fail(`${label}.compatible_overlay_generation_ids must be unique and sorted`);
  }
  if (raw.created_at !== undefined) nonEmptyString(raw.created_at, `${label}.created_at`);

  const expectedReleaseId = domainIdentity("wikilean.release.v1", raw, ["release_id", "attestations", "created_at"]);
  if (raw.release_id !== expectedReleaseId) fail(`${label}.release_id does not identify the canonical manifest`);
  return {
    schema: RELEASE_SCHEMA,
    profile: RELEASE_PROFILE,
    release_id: raw.release_id as string,
    authority: raw.authority as ReleaseManifest["authority"],
    source_set_root: raw.source_set_root as string,
    semantic_epoch: raw.semantic_epoch as string,
    reducer: raw.reducer as ReleaseManifest["reducer"],
    artifacts,
    attestations,
    compatible_overlay_generation_ids: overlays,
    ...(raw.created_at === undefined ? {} : { created_at: raw.created_at as string }),
  };
}

function publicArtifacts(manifest: ReleaseManifest): Array<ReleaseArtifact & { publicPath: string }> {
  const seenSource = new Set<string>();
  const seenPublic = new Set<string>();
  const foundRequired = new Set<string>();
  const selected: Array<ReleaseArtifact & { publicPath: string }> = [];

  for (const artifact of manifest.artifacts) {
    if (seenSource.has(artifact.path)) fail(`release manifest repeats artifact path ${artifact.path}`);
    seenSource.add(artifact.path);
    const publicPath = publicPathForArtifact(artifact.path);
    if (publicPath === null) continue;
    if (seenPublic.has(publicPath)) fail(`release manifest maps multiple artifacts to ${publicPath}`);
    seenPublic.add(publicPath);
    foundRequired.add(artifact.path);
    selected.push({ ...artifact, publicPath });
  }

  const missing = [...REQUIRED_PUBLIC_PATHS].filter(path => !foundRequired.has(path)).sort();
  if (missing.length) fail(`release manifest is missing required public artifacts: ${missing.join(", ")}`);
  return selected.sort((a, b) => a.publicPath.localeCompare(b.publicPath));
}

function openRegularNoFollow(path: string, label: string): number {
  let descriptor: number;
  try {
    descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  } catch (error) {
    fail(`${label} could not be opened safely: ${String(error)}`);
  }
  const info = fstatSync(descriptor);
  if (!info.isFile()) {
    closeSync(descriptor);
    fail(`${label} must be a regular file`);
  }
  return descriptor;
}

function readBoundedFile(root: string, relativePath: string, maxBytes: number, label: string): Buffer {
  const path = resolveContained(root, relativePath);
  assertNoSymlinkComponents(root, relativePath, label);
  const descriptor = openRegularNoFollow(path, label);
  try {
    const info = fstatSync(descriptor);
    if (!Number.isSafeInteger(info.size) || info.size > maxBytes) {
      fail(`${label} has ${info.size} bytes; configured per-file limit is ${maxBytes}`);
    }
    const bytes = Buffer.allocUnsafe(info.size);
    let offset = 0;
    while (offset < bytes.byteLength) {
      const count = readSync(descriptor, bytes, offset, bytes.byteLength - offset, null);
      if (count === 0) break;
      offset += count;
    }
    const extra = Buffer.allocUnsafe(1);
    if (offset !== bytes.byteLength || readSync(descriptor, extra, 0, 1, null) !== 0) {
      fail(`${label} changed size while it was read`);
    }
    return bytes;
  } finally {
    closeSync(descriptor);
  }
}

function namespaceFile(
  destinationPath: string,
  sourceRoot: string,
  sourcePath: string,
  bytes: number,
  digest: string,
  label: string,
): NamespaceFile {
  const source = resolveContained(sourceRoot, sourcePath);
  if (!existsSync(source)) fail(`${label} is missing`);
  assertNoSymlinkComponents(sourceRoot, sourcePath, label);
  assertRegularFile(source, label);
  return { destinationPath, sourceRoot, sourcePath, bytes, sha256: digest, label };
}

function loadCurrentNamespace(
  manifestPath: string,
  releaseDir: string,
  maxManifestBytes: number,
  maxObjects: number,
): LoadedNamespace {
  const root = resolve(releaseDir);
  if (!existsSync(root)) fail(`release directory does not exist: ${root}`);
  const rootInfo = lstatSync(root);
  if (rootInfo.isSymbolicLink() || !rootInfo.isDirectory()) {
    fail(`release directory must be a real directory: ${root}`);
  }

  const expectedManifest = resolve(root, "release.json");
  const explicitManifest = resolve(manifestPath);
  if (explicitManifest !== expectedManifest) {
    fail(`manifest must be the release directory's release.json (${expectedManifest})`);
  }
  const manifestBytes = readBoundedFile(root, "release.json", maxManifestBytes, "release manifest");
  const manifest = parseReleaseManifest(manifestBytes, "release manifest");
  const hex = releaseHex(manifest.release_id, "release manifest release_id");
  if (basename(root) !== hex) {
    fail(`release directory basename must equal release hex ${hex}`);
  }

  const selectedArtifacts = publicArtifacts(manifest);
  if (selectedArtifacts.length + 1 > maxObjects) {
    fail(`release public object count ${selectedArtifacts.length + 1} exceeds configured limit ${maxObjects}`);
  }
  const files: NamespaceFile[] = [namespaceFile(
    "release.json",
    root,
    "release.json",
    manifestBytes.byteLength,
    sha256(manifestBytes),
    "release manifest",
  )];
  for (const artifact of selectedArtifacts) {
    files.push(namespaceFile(
      artifact.publicPath,
      root,
      artifact.path,
      artifact.bytes,
      artifact.sha256,
      `release artifact ${artifact.path}`,
    ));
  }
  const pageArtifact = manifest.artifacts.find(artifact => artifact.path === "site/out/brain.html");
  if (!pageArtifact) fail("release manifest is missing required Brain page artifact site/out/brain.html");
  const brainPage = namespaceFile(
    "brain.html",
    root,
    pageArtifact.path,
    pageArtifact.bytes,
    pageArtifact.sha256,
    "release artifact site/out/brain.html",
  );
  return { releaseId: manifest.release_id, releaseHex: hex, files, brainPage };
}

function parseSelector(root: string): BrainReleaseSelector {
  const raw = parseJsonObject(
    readBoundedFile(root, "current.json", MAX_SELECTOR_BYTES, "prior selector"),
    "prior selector",
  );
  const required = ["schema", "release_id", "release", "manifest"];
  const previousKeys = ["previous_release_id", "previous_release", "previous_manifest"];
  const allowed = new Set([...required, ...previousKeys, "audited_at"]);
  const missing = required.filter(key => !(key in raw));
  if (missing.length) fail(`prior selector is missing required fields: ${missing.join(", ")}`);
  const unknown = Object.keys(raw).filter(key => !allowed.has(key));
  if (unknown.length) fail(`prior selector has unknown fields: ${unknown.join(", ")}`);
  if (raw.schema !== SELECTOR_SCHEMA) fail("prior selector has unsupported schema");
  const currentHex = releaseHex(raw.release_id, "prior selector release_id");
  if (raw.release !== currentHex) fail("prior selector release does not match release_id");
  if (raw.manifest !== `/assets/brain/releases/${currentHex}/release.json`) {
    fail("prior selector manifest path is inconsistent");
  }
  const presentPrevious = previousKeys.filter(key => raw[key] !== undefined);
  if (presentPrevious.length !== 0 && presentPrevious.length !== previousKeys.length) {
    fail("prior selector previous release fields must be supplied together");
  }
  if (presentPrevious.length) {
    const previousHex = releaseHex(raw.previous_release_id, "prior selector previous_release_id");
    if (raw.previous_release !== previousHex) fail("prior selector previous_release does not match previous_release_id");
    if (raw.previous_manifest !== `/assets/brain/releases/${previousHex}/release.json`) {
      fail("prior selector previous_manifest path is inconsistent");
    }
    if (raw.previous_release_id === raw.release_id) fail("prior selector previous release must differ from current");
  }
  if (raw.audited_at !== undefined && (typeof raw.audited_at !== "string" || !raw.audited_at)) {
    fail("prior selector audited_at must be a non-empty string when present");
  }
  return raw as unknown as BrainReleaseSelector;
}

function loadPublicNamespace(
  namespaceDir: string,
  expectedReleaseId: string,
  maxManifestBytes: number,
  maxObjects: number,
): LoadedNamespace {
  const root = resolve(namespaceDir);
  if (!existsSync(root)) fail(`prior public release is missing: ${root}`);
  const rootInfo = lstatSync(root);
  if (rootInfo.isSymbolicLink() || !rootInfo.isDirectory()) {
    fail(`prior public release must be a real directory: ${root}`);
  }
  const manifestPath = resolve(root, "release.json");
  if (!existsSync(manifestPath)) fail(`prior public release has no release.json: ${root}`);
  const manifestBytes = readBoundedFile(
    root,
    "release.json",
    maxManifestBytes,
    "prior public release manifest",
  );
  const manifest = parseReleaseManifest(manifestBytes, "prior public release manifest");
  if (manifest.release_id !== expectedReleaseId) {
    fail("prior public release manifest does not match the prior selector");
  }
  const hex = releaseHex(manifest.release_id, "prior public release manifest release_id");
  if (basename(root) !== hex) fail("prior public release directory does not match its release ID");

  const selectedArtifacts = publicArtifacts(manifest);
  if (selectedArtifacts.length + 1 > maxObjects) {
    fail(`prior public object count ${selectedArtifacts.length + 1} exceeds configured limit ${maxObjects}`);
  }
  const files: NamespaceFile[] = [namespaceFile(
    "release.json",
    root,
    "release.json",
    manifestBytes.byteLength,
    sha256(manifestBytes),
    "prior public release manifest",
  )];
  for (const artifact of selectedArtifacts) {
    files.push(namespaceFile(
      artifact.publicPath,
      root,
      artifact.publicPath,
      artifact.bytes,
      artifact.sha256,
      `prior public artifact ${artifact.publicPath}`,
    ));
  }
  return { releaseId: manifest.release_id, releaseHex: hex, files, brainPage: null };
}

function writeAll(descriptor: number, bytes: Buffer): void {
  let offset = 0;
  while (offset < bytes.byteLength) {
    const count = writeSync(descriptor, bytes, offset, bytes.byteLength - offset);
    if (count <= 0) fail("filesystem write made no progress");
    offset += count;
  }
}

function writeBufferFile(root: string, relativePath: string, bytes: Buffer): void {
  const target = resolveContained(root, relativePath);
  mkdirSync(dirname(target), { recursive: true });
  const descriptor = openSync(
    target,
    constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
    0o644,
  );
  try {
    writeAll(descriptor, bytes);
  } finally {
    closeSync(descriptor);
  }
}

function copyVerifiedFile(file: NamespaceFile, targetRoot: string, scratch: Buffer): void {
  const source = resolveContained(file.sourceRoot, file.sourcePath);
  assertNoSymlinkComponents(file.sourceRoot, file.sourcePath, file.label);
  const target = resolveContained(targetRoot, file.destinationPath);
  mkdirSync(dirname(target), { recursive: true });
  const sourceDescriptor = openRegularNoFollow(source, file.label);
  let targetDescriptor: number | null = null;
  try {
    const sourceInfo = fstatSync(sourceDescriptor);
    if (sourceInfo.size !== file.bytes) {
      fail(`${file.label} has ${sourceInfo.size} bytes; manifest declares ${file.bytes}`);
    }
    targetDescriptor = openSync(
      target,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
      0o644,
    );
    const digest = createHash("sha256");
    let copied = 0;
    while (true) {
      const count = readSync(sourceDescriptor, scratch, 0, scratch.byteLength, null);
      if (count === 0) break;
      copied += count;
      if (copied > file.bytes) fail(`${file.label} grew while it was copied`);
      const chunk = scratch.subarray(0, count);
      digest.update(chunk);
      writeAll(targetDescriptor, chunk);
    }
    if (copied !== file.bytes) {
      fail(`${file.label} has ${copied} bytes; manifest declares ${file.bytes}`);
    }
    if (digest.digest("hex") !== file.sha256) fail(`${file.label} sha256 mismatch`);
  } catch (error) {
    if (targetDescriptor !== null) closeSync(targetDescriptor);
    targetDescriptor = null;
    rmSync(target, { force: true });
    throw error;
  } finally {
    closeSync(sourceDescriptor);
    if (targetDescriptor !== null) closeSync(targetDescriptor);
  }
}

function copyVerifiedFiles(root: string, files: NamespaceFile[], scratch: Buffer): void {
  for (const file of files) copyVerifiedFile(file, root, scratch);
}

function checkedAdd(left: number, right: number, label: string): number {
  const sum = left + right;
  if (!Number.isSafeInteger(sum)) fail(`${label} exceeds the safe integer range`);
  return sum;
}

function measureFiles(files: Iterable<{ bytes: number }>): TreeMeasurement {
  let objects = 0;
  let bytes = 0;
  let largest = 0;
  for (const value of files) {
    objects = checkedAdd(objects, 1, "staged object count");
    bytes = checkedAdd(bytes, value.bytes, "staged byte count");
    largest = Math.max(largest, value.bytes);
  }
  return { objects, bytes, largest };
}

function verifyTree(
  root: string,
  expectedFiles: ReadonlyMap<string, ExpectedFile>,
  scratch: Buffer,
): TreeMeasurement {
  let objects = 0;
  let bytes = 0;
  let largest = 0;
  const remaining = new Set(expectedFiles.keys());
  const directories = [root];
  while (directories.length) {
    const directory = directories.pop()!;
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isSymbolicLink()) fail(`staged output contains a symlink: ${relative(root, path)}`);
      if (entry.isDirectory()) {
        directories.push(path);
      } else if (entry.isFile()) {
        const relativePath = relative(root, path).split(sep).join("/");
        const expected = expectedFiles.get(relativePath);
        if (!expected) fail(`staged output contains unexpected file: ${relativePath}`);
        const descriptor = openRegularNoFollow(path, `staged output ${relativePath}`);
        let size = 0;
        const digest = createHash("sha256");
        try {
          const info = fstatSync(descriptor);
          if (info.size !== expected.bytes) {
            fail(`staged output ${relativePath} has ${info.size} bytes; expected ${expected.bytes}`);
          }
          while (true) {
            const count = readSync(descriptor, scratch, 0, scratch.byteLength, null);
            if (count === 0) break;
            size = checkedAdd(size, count, `staged output ${relativePath} byte count`);
            digest.update(scratch.subarray(0, count));
          }
        } finally {
          closeSync(descriptor);
        }
        if (size !== expected.bytes || digest.digest("hex") !== expected.sha256) {
          fail(`staged output ${relativePath} digest mismatch`);
        }
        remaining.delete(relativePath);
        objects = checkedAdd(objects, 1, "staged object count");
        bytes = checkedAdd(bytes, size, "staged byte count");
        largest = Math.max(largest, size);
      } else {
        fail(`staged output contains a non-file object: ${relative(root, path)}`);
      }
    }
  }
  if (remaining.size) {
    fail(`staged output is missing expected files: ${[...remaining].sort().join(", ")}`);
  }
  return { objects, bytes, largest };
}

function positiveLimit(value: number | undefined, fallback: number, label: string): number {
  const result = value ?? fallback;
  if (!Number.isSafeInteger(result) || result <= 0) fail(`${label} must be a positive safe integer`);
  return result;
}

function nonNegativeLimit(value: number | undefined, fallback: number, label: string): number {
  const result = value ?? fallback;
  if (!Number.isSafeInteger(result) || result < 0) fail(`${label} must be a non-negative safe integer`);
  return result;
}

function availableBytes(path: string): bigint {
  const info = statfsSync(path, { bigint: true });
  return info.bavail * info.bsize;
}

function reportBytes(value: bigint): number {
  return Number(value > BigInt(Number.MAX_SAFE_INTEGER) ? BigInt(Number.MAX_SAFE_INTEGER) : value);
}

function replaceDirectory(candidate: string, destination: string): string | null {
  const backup = `${destination}.backup-${process.pid}-${randomUUID()}`;
  let movedOld = false;
  try {
    if (existsSync(destination)) {
      renameSync(destination, backup);
      movedOld = true;
    }
    renameSync(candidate, destination);
  } catch (error) {
    if (movedOld && !existsSync(destination) && existsSync(backup)) renameSync(backup, destination);
    throw error;
  }
  if (!movedOld) return null;
  try {
    rmSync(backup, { recursive: true, force: true });
    return null;
  } catch {
    // Activation has committed. Reporting success with a precise cleanup warning
    // is safer than claiming failure after the destination has already changed.
    return backup;
  }
}

function replaceDirectoryAndFile(
  directoryCandidate: string,
  directoryDestination: string,
  fileCandidate: string,
  fileDestination: string,
): string[] {
  const token = `${process.pid}-${randomUUID()}`;
  const directoryBackup = `${directoryDestination}.backup-${token}`;
  const fileBackup = `${fileDestination}.backup-${token}`;
  let movedOldDirectory = false;
  let movedOldFile = false;
  let movedNewDirectory = false;
  let movedNewFile = false;
  try {
    if (existsSync(directoryDestination)) {
      renameSync(directoryDestination, directoryBackup);
      movedOldDirectory = true;
    }
    if (existsSync(fileDestination)) {
      renameSync(fileDestination, fileBackup);
      movedOldFile = true;
    }
    renameSync(directoryCandidate, directoryDestination);
    movedNewDirectory = true;
    renameSync(fileCandidate, fileDestination);
    movedNewFile = true;
  } catch (error) {
    const rollbackErrors: string[] = [];
    const attempt = (action: () => void): void => {
      try {
        action();
      } catch (rollbackError) {
        rollbackErrors.push(String(rollbackError));
      }
    };
    if (movedNewFile && existsSync(fileDestination)) {
      attempt(() => rmSync(fileDestination, { force: true }));
    }
    if (movedNewDirectory && existsSync(directoryDestination)) {
      attempt(() => rmSync(directoryDestination, { recursive: true, force: true }));
    }
    if (movedOldFile && existsSync(fileBackup)) {
      attempt(() => renameSync(fileBackup, fileDestination));
    }
    if (movedOldDirectory && existsSync(directoryBackup)) {
      attempt(() => renameSync(directoryBackup, directoryDestination));
    }
    if (rollbackErrors.length) {
      fail(`coupled page/assets activation failed (${String(error)}); rollback also failed: ${rollbackErrors.join("; ")}`);
    }
    throw error;
  }

  const warnings: string[] = [];
  if (movedOldDirectory) {
    try {
      rmSync(directoryBackup, { recursive: true, force: true });
    } catch {
      warnings.push(`previous public tree cleanup failed: ${directoryBackup}`);
    }
  }
  if (movedOldFile) {
    try {
      rmSync(fileBackup, { force: true });
    } catch {
      warnings.push(`previous Brain page cleanup failed: ${fileBackup}`);
    }
  }
  return warnings;
}

export function stageBrainPublicRelease(options: StageBrainPublicOptions): StageBrainPublicResult {
  const started = process.hrtime.bigint();
  const destination = resolve(options.destination);
  const parent = dirname(destination);
  mkdirSync(parent, { recursive: true });
  const destinationInfo = lstatSync(destination, { throwIfNoEntry: false });
  if (destinationInfo?.isSymbolicLink() || (destinationInfo && !destinationInfo.isDirectory())) {
    fail(`destination must be a real directory when it exists: ${destination}`);
  }
  const pageDestination = options.brainPageDestination
    ? resolve(options.brainPageDestination)
    : null;
  let pageParent: string | null = null;
  if (pageDestination) {
    const relation = relative(destination, pageDestination);
    if (!relation || (relation !== ".." && !relation.startsWith(`..${sep}`))) {
      fail("Brain page destination must be outside the Brain asset destination");
    }
    pageParent = dirname(pageDestination);
    mkdirSync(pageParent, { recursive: true });
    const pageParentInfo = lstatSync(pageParent);
    if (pageParentInfo.isSymbolicLink() || !pageParentInfo.isDirectory()) {
      fail(`Brain page parent must be a real directory: ${pageParent}`);
    }
    const pageInfo = lstatSync(pageDestination, { throwIfNoEntry: false });
    if (pageInfo?.isSymbolicLink() || (pageInfo && !pageInfo.isFile())) {
      fail(`Brain page destination must be a regular file when it exists: ${pageDestination}`);
    }
    if (lstatSync(parent).dev !== pageParentInfo.dev) {
      fail("Brain page and asset destinations must be on the same filesystem");
    }
  }
  const maxObjects = positiveLimit(options.maxObjects, DEFAULT_MAX_OBJECTS, "maxObjects");
  const maxBytes = positiveLimit(options.maxBytes, DEFAULT_MAX_BYTES, "maxBytes");
  const maxFileBytes = positiveLimit(options.maxFileBytes, DEFAULT_MAX_FILE_BYTES, "maxFileBytes");
  const minFreeBytes = nonNegativeLimit(options.minFreeBytes, DEFAULT_MIN_FREE_BYTES, "minFreeBytes");
  const current = loadCurrentNamespace(
    options.manifestPath,
    options.releaseDir,
    maxFileBytes,
    maxObjects,
  );
  if (!current.brainPage) fail("current release has no frozen Brain page");
  if (current.brainPage.bytes > maxFileBytes) {
    fail(`frozen Brain page ${current.brainPage.bytes} bytes exceeds configured limit ${maxFileBytes}`);
  }

  const hasPreviousManifest = options.previousManifestPath !== undefined;
  const hasPreviousDir = options.previousReleaseDir !== undefined;
  if (hasPreviousManifest !== hasPreviousDir) {
    fail("previousManifestPath and previousReleaseDir must be supplied together");
  }
  let previous: LoadedNamespace | null = hasPreviousManifest && hasPreviousDir
    ? loadCurrentNamespace(
      options.previousManifestPath!,
      options.previousReleaseDir!,
      maxFileBytes,
      maxObjects,
    )
    : null;
  if (previous?.releaseId === current.releaseId) previous = null;

  const oldSelectorPath = resolve(destination, "current.json");
  if (!hasPreviousManifest && existsSync(oldSelectorPath)) {
    const oldSelector = parseSelector(destination);
    const retainedId = oldSelector.release_id === current.releaseId
      ? oldSelector.previous_release_id
      : oldSelector.release_id;
    const retainedHex = oldSelector.release_id === current.releaseId
      ? oldSelector.previous_release
      : oldSelector.release;
    if (retainedId && retainedHex && retainedId !== current.releaseId) {
      previous = loadPublicNamespace(
        resolve(destination, "releases", retainedHex),
        retainedId,
        maxFileBytes,
        maxObjects,
      );
    }
  }

  const aliasFiles = current.files.filter(file => ["cells", "sources.json", "xref_index.json"]
    .some(alias => file.destinationPath === alias || file.destinationPath.startsWith(`${alias}/`)));
  const selector: BrainReleaseSelector = {
    schema: SELECTOR_SCHEMA,
    release_id: current.releaseId,
    release: current.releaseHex,
    manifest: `/assets/brain/releases/${current.releaseHex}/release.json`,
    ...(previous ? {
      previous_release_id: previous.releaseId,
      previous_release: previous.releaseHex,
      previous_manifest: `/assets/brain/releases/${previous.releaseHex}/release.json`,
    } : {}),
    audited_at: options.auditedAt ?? new Date().toISOString(),
  };
  const selectorBytes = Buffer.from(`${JSON.stringify(selector)}\n`);
  const expectedFiles = new Map<string, ExpectedFile>();
  const expectFile = (path: string, bytes: number, digest: string): void => {
    if (expectedFiles.has(path)) fail(`multiple staged files target ${path}`);
    expectedFiles.set(path, { bytes, sha256: digest });
  };
  for (const file of current.files) {
    expectFile(`releases/${current.releaseHex}/${file.destinationPath}`, file.bytes, file.sha256);
  }
  if (previous) {
    for (const file of previous.files) {
      expectFile(`releases/${previous.releaseHex}/${file.destinationPath}`, file.bytes, file.sha256);
    }
  }
  for (const file of aliasFiles) expectFile(file.destinationPath, file.bytes, file.sha256);
  expectFile("current.json", selectorBytes.byteLength, sha256(selectorBytes));
  const planned = measureFiles([
    ...expectedFiles.values(),
    ...(pageDestination ? [{ bytes: current.brainPage.bytes }] : []),
  ]);
  if (planned.objects > maxObjects) {
    fail(`staged object count ${planned.objects} exceeds configured limit ${maxObjects}`);
  }
  if (planned.bytes > maxBytes) {
    fail(`staged byte count ${planned.bytes} exceeds configured limit ${maxBytes}`);
  }
  if (planned.largest > maxFileBytes) {
    fail(`largest staged object ${planned.largest} bytes exceeds configured limit ${maxFileBytes}`);
  }
  const freeBefore = availableBytes(parent);
  const requiredFree = BigInt(planned.bytes) + BigInt(minFreeBytes);
  if (freeBefore < requiredFree) {
    fail(
      `staging requires ${planned.bytes} bytes plus ${minFreeBytes} bytes reserved headroom; ` +
      `only ${freeBefore} bytes are available`,
    );
  }

  const staging = mkdtempSync(join(parent, `.${basename(destination)}.stage-`));
  let pageStaging: string | null = null;
  let finalized = false;
  try {
    if (pageDestination && pageParent) {
      pageStaging = mkdtempSync(join(pageParent, ".brain-page.stage-"));
    }
    const scratch = Buffer.allocUnsafe(COPY_BUFFER_BYTES);
    const currentRoot = resolve(staging, "releases", current.releaseHex);
    copyVerifiedFiles(currentRoot, current.files, scratch);
    if (previous) {
      copyVerifiedFiles(resolve(staging, "releases", previous.releaseHex), previous.files, scratch);
    }
    const stagedAliases = aliasFiles.map(file => ({
      ...file,
      sourceRoot: currentRoot,
      sourcePath: file.destinationPath,
      label: `staged immutable artifact ${file.destinationPath}`,
    }));
    copyVerifiedFiles(staging, stagedAliases, scratch);
    writeBufferFile(staging, "current.json", selectorBytes);

    const measuredAssets = verifyTree(staging, expectedFiles, scratch);
    let measured = measuredAssets;
    if (pageStaging) {
      copyVerifiedFile(current.brainPage, pageStaging, scratch);
      const measuredPage = verifyTree(
        pageStaging,
        new Map([["brain.html", {
          bytes: current.brainPage.bytes,
          sha256: current.brainPage.sha256,
        }]]),
        scratch,
      );
      measured = {
        objects: checkedAdd(measuredAssets.objects, measuredPage.objects, "staged object count"),
        bytes: checkedAdd(measuredAssets.bytes, measuredPage.bytes, "staged byte count"),
        largest: Math.max(measuredAssets.largest, measuredPage.largest),
      };
    }
    if (measured.objects !== planned.objects || measured.bytes !== planned.bytes || measured.largest !== planned.largest) {
      fail("staged tree measurement changed while writing");
    }

    const activationWarnings = pageDestination && pageStaging
      ? replaceDirectoryAndFile(
        staging,
        destination,
        resolve(pageStaging, "brain.html"),
        pageDestination,
      )
      : (() => {
        const retainedBackup = replaceDirectory(staging, destination);
        return retainedBackup ? [`previous public tree cleanup failed: ${retainedBackup}`] : [];
      })();
    finalized = true;
    const freeAfter = availableBytes(parent);
    const warnings = [...activationWarnings];
    if (freeAfter < BigInt(minFreeBytes)) {
      warnings.push(
        `free space after staging is ${freeAfter} bytes, below reserved headroom ${minFreeBytes}`,
      );
    }
    return {
      schema: "wikilean.public-stage-result/v1",
      release_id: current.releaseId,
      release: current.releaseHex,
      previous_release_id: previous?.releaseId ?? null,
      retained_release_ids: [current.releaseId, ...(previous ? [previous.releaseId] : [])],
      destination,
      objects: measured.objects,
      bytes: measured.bytes,
      largest_file_bytes: measured.largest,
      copy_buffer_bytes: COPY_BUFFER_BYTES,
      duration_ms: Math.round(Number(process.hrtime.bigint() - started) / 1e3) / 1e3,
      max_rss_bytes: Math.round(process.resourceUsage().maxRSS * 1024),
      free_bytes_before: reportBytes(freeBefore),
      free_bytes_after: reportBytes(freeAfter),
      brain_page: pageDestination ? {
        destination: pageDestination,
        bytes: current.brainPage.bytes,
        sha256: current.brainPage.sha256,
      } : null,
      warnings,
    };
  } finally {
    if (!finalized && existsSync(staging)) rmSync(staging, { recursive: true, force: true });
    if (pageStaging && existsSync(pageStaging)) rmSync(pageStaging, { recursive: true, force: true });
  }
}

function parsePositiveEnv(name: string): number | undefined {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return undefined;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) fail(`${name} must be a positive safe integer`);
  return value;
}

function optionValue(args: string[], name: string): string | undefined {
  const index = args.indexOf(name);
  if (index === -1) return undefined;
  if (index + 1 >= args.length || args[index + 1].startsWith("--")) fail(`${name} requires a value`);
  return args[index + 1];
}

export function stageOptionsFromArgs(args: string[]): StageBrainPublicOptions {
  const known = new Set([
    "--manifest", "--release-dir", "--destination", "--previous-manifest", "--previous-release-dir",
    "--brain-page-destination", "--max-objects", "--max-bytes", "--max-file-bytes", "--min-free-bytes",
  ]);
  const seen = new Set<string>();
  for (let index = 0; index < args.length; index += 2) {
    if (!known.has(args[index])) fail(`unknown option ${args[index]}`);
    if (seen.has(args[index])) fail(`option ${args[index]} may be supplied only once`);
    if (index + 1 >= args.length) fail(`${args[index]} requires a value`);
    if (args[index + 1].startsWith("--")) fail(`${args[index]} requires a value`);
    seen.add(args[index]);
  }
  const manifestPath = optionValue(args, "--manifest") ?? process.env.BRAIN_RELEASE_MANIFEST;
  const releaseDir = optionValue(args, "--release-dir") ?? process.env.BRAIN_RELEASE_DIR;
  const destination = optionValue(args, "--destination") ?? process.env.BRAIN_PUBLIC_DESTINATION;
  const previousManifestPath = optionValue(args, "--previous-manifest") ?? process.env.BRAIN_PREVIOUS_RELEASE_MANIFEST;
  const previousReleaseDir = optionValue(args, "--previous-release-dir") ?? process.env.BRAIN_PREVIOUS_RELEASE_DIR;
  if (!manifestPath || !releaseDir || !destination) {
    fail("--manifest, --release-dir, and --destination (or matching environment variables) are required");
  }
  if ((previousManifestPath === undefined) !== (previousReleaseDir === undefined)) {
    fail("--previous-manifest and --previous-release-dir must be supplied together");
  }
  const numeric = (flag: string, env: string): number | undefined => {
    const raw = optionValue(args, flag);
    if (raw === undefined) return parsePositiveEnv(env);
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value <= 0) fail(`${flag} must be a positive safe integer`);
    return value;
  };
  const nonNegativeNumeric = (flag: string, env: string): number | undefined => {
    const raw = optionValue(args, flag) ?? process.env[env];
    if (raw === undefined || raw === "") return undefined;
    const value = Number(raw);
    if (!Number.isSafeInteger(value) || value < 0) fail(`${flag} must be a non-negative safe integer`);
    return value;
  };
  return {
    manifestPath,
    releaseDir,
    destination,
    brainPageDestination: optionValue(args, "--brain-page-destination") ?? process.env.BRAIN_PAGE_DESTINATION,
    previousManifestPath,
    previousReleaseDir,
    maxObjects: numeric("--max-objects", "BRAIN_PUBLIC_MAX_OBJECTS"),
    maxBytes: numeric("--max-bytes", "BRAIN_PUBLIC_MAX_BYTES"),
    maxFileBytes: numeric("--max-file-bytes", "BRAIN_PUBLIC_MAX_FILE_BYTES"),
    minFreeBytes: nonNegativeNumeric("--min-free-bytes", "BRAIN_PUBLIC_MIN_FREE_BYTES"),
  };
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const result = stageBrainPublicRelease(stageOptionsFromArgs(process.argv.slice(2)));
  process.stdout.write(`${JSON.stringify(result)}\n`);
}
