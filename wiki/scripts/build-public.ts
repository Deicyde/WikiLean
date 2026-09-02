// Builds the Worker's static-asset tree. Shadow builds retain the historical
// wiki/public destination; promotion builds use a fresh explicit directory
// outside the checkout so ignored files cannot leak into a deployment.
import {
  closeSync,
  constants,
  copyFileSync,
  existsSync,
  fstatSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  readdirSync,
  realpathSync,
  rmSync,
  statSync,
  writeSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { basename, dirname, isAbsolute, posix, relative, resolve, sep } from "node:path";
import { pathToFileURL } from "node:url";
import { buildMathlibIndex } from "./build-mathlib-index.ts";
import {
  stageBrainPublicRelease,
  type StageBrainPublicOptions,
  type StageBrainPublicResult,
} from "./brain-release-public.ts";

export interface BuildPublicOptions {
  wikiDir: string;
  publicDir?: string;
  publicBaselineManifest?: string;
  publicBaselineDir?: string;
  brain: Omit<StageBrainPublicOptions, "destination">;
}

export interface PublicBaselineBuildResult {
  schema: "wikilean.public-asset-baseline/v1";
  baseline_id: string;
  authority_commit: string;
  root: string;
  files: number;
  bytes: number;
}

export interface BuildPublicResult {
  schema: "wikilean.public-build-result/v1";
  publicDir: string;
  mathlibDeclarations: number;
  publicBaseline: PublicBaselineBuildResult | null;
  brain: StageBrainPublicResult;
  duration_ms: number;
  max_rss_bytes: number;
}

const PUBLIC_BASELINE_SCHEMA = "wikilean.public-asset-baseline/v1";
const PUBLIC_BASELINE_DOMAIN = "wikilean.public-asset-baseline.v1";
const BASELINE_ID_RE = /^sha256:([0-9a-f]{64})$/;
const DIGEST_RE = /^[0-9a-f]{64}$/;
const GIT_COMMIT_RE = /^[0-9a-f]{40}$/;
const MAX_BASELINE_MANIFEST_BYTES = 32 * 1024 * 1024;
const MAX_BASELINE_PATH_BYTES = 4096;
const MAX_BASELINE_PATH_COMPONENTS = 64;
const BASELINE_COPY_BUFFER_BYTES = 1024 * 1024;
const REQUIRED_BASELINE_PATHS = new Set([
  "404.html",
  "robots.txt",
  "wikilean.ttl",
  "concepts.html",
  "assets/style.css",
  "assets/script.js",
  "assets/review.css",
  "assets/editor.js",
  "assets/mathlib-index.json",
  "assets/decl-index/manifest.json",
  "assets/suffix-index/manifest.json",
  "assets/premise-index/manifest.json",
]);
const BASELINE_INDEX_FAMILIES = [
  "assets/decl-index/",
  "assets/suffix-index/",
  "assets/premise-index/",
] as const;
const FORBIDDEN_BASELINE_PATHS = new Set([
  "index.html",
  "sitemap.xml",
  "about.html",
  "map.html",
  "map-v2.html",
  "graph.html",
  "graph_data.json",
  "atlas.html",
  "atlas_data.json",
  "article-graph.html",
  "article-graph-data.json",
  "map_data.json",
  "map_data_v2.json",
]);
const ALLOWED_BASELINE_TOP_LEVEL_FILES = new Set([
  "404.html",
  "robots.txt",
  "wikilean.ttl",
  "concepts.html",
]);

interface PublicBaselineFile {
  path: string;
  bytes: number;
  sha256: string;
}

interface LoadedPublicBaseline {
  schema: typeof PUBLIC_BASELINE_SCHEMA;
  baselineId: string;
  authorityCommit: string;
  root: string;
  manifestSha256: string;
  files: PublicBaselineFile[];
  bytes: number;
}

function isWithin(root: string, candidate: string): boolean {
  const relation = relative(root, candidate);
  return relation === "" || (relation !== ".." && !relation.startsWith(`..${sep}`));
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function exactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
  label: string,
): void {
  const allowed = new Set(expected);
  const missing = expected.filter(key => !Object.prototype.hasOwnProperty.call(value, key));
  const unknown = Object.keys(value).filter(key => !allowed.has(key));
  if (missing.length || unknown.length) {
    const details = [
      ...(missing.length ? [`missing ${missing.join(", ")}`] : []),
      ...(unknown.length ? [`unknown ${unknown.join(", ")}`] : []),
    ];
    throw new Error(`${label} fields are invalid (${details.join("; ")})`);
  }
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "string") {
    return JSON.stringify(value) as string;
  }
  if (typeof value === "number" && Number.isSafeInteger(value)) return String(value);
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  throw new Error("public baseline manifest contains an unsupported JSON value");
}

function sha256(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function hasUnpairedSurrogate(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) return true;
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      return true;
    }
  }
  return false;
}

function compareUnicodeCodePoints(left: string, right: string): number {
  const leftPoints = Array.from(left, value => value.codePointAt(0) as number);
  const rightPoints = Array.from(right, value => value.codePointAt(0) as number);
  const common = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < common; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index];
  }
  return leftPoints.length - rightPoints.length;
}

function safeBaselinePath(value: unknown, label: string, allowManifest = false): string {
  if (typeof value !== "string" || !value || value.includes("\\") || value.includes("\0")) {
    throw new Error(`${label} is not a safe relative POSIX path`);
  }
  if (
    value.normalize("NFC") !== value
    || hasUnpairedSurrogate(value)
    || Buffer.byteLength(value, "utf8") > MAX_BASELINE_PATH_BYTES
    || value.split("/").length > MAX_BASELINE_PATH_COMPONENTS
    || posix.isAbsolute(value)
    || posix.normalize(value) !== value
    || value.split("/").some(part => part === "" || part === "." || part === "..")
  ) {
    throw new Error(`${label} is not a normalized relative POSIX path`);
  }
  if (!allowManifest && value === "manifest.json") {
    throw new Error("manifest.json is reserved for baseline metadata");
  }
  return value;
}

function isBrainOwned(path: string): boolean {
  return path === "brain.html" || path === "assets/brain" || path.startsWith("assets/brain/");
}

function isRouteShadowingOrRetired(path: string): boolean {
  return FORBIDDEN_BASELINE_PATHS.has(path)
    || (!path.startsWith("assets/") && !ALLOWED_BASELINE_TOP_LEVEL_FILES.has(path));
}

function resolveContained(root: string, relativePath: string): string {
  const path = resolve(root, ...relativePath.split("/"));
  if (!isWithin(root, path) || path === root) {
    throw new Error(`public baseline path escapes its root: ${relativePath}`);
  }
  return path;
}

function assertNoSymlinkComponents(root: string, relativePath: string, label: string): string {
  let current = root;
  for (const component of relativePath.split("/")) {
    current = resolve(current, component);
    const info = lstatSync(current);
    if (info.isSymbolicLink()) throw new Error(`${label} must not traverse a symlink`);
  }
  return current;
}

function expectedDirectories(paths: Iterable<string>): Set<string> {
  const result = new Set<string>();
  for (const path of paths) {
    const parts = path.split("/");
    for (let length = 1; length < parts.length; length += 1) {
      result.add(parts.slice(0, length).join("/"));
    }
  }
  return result;
}

function scanBaselineTree(root: string): { files: Set<string>; directories: Set<string> } {
  const files = new Set<string>();
  const directories = new Set<string>();
  const walk = (directory: string, prefix: string): void => {
    const names = readdirSync(directory).sort();
    for (const name of names) {
      const relativePath = prefix ? `${prefix}/${name}` : name;
      safeBaselinePath(relativePath, "public baseline tree path", true);
      const path = resolve(directory, name);
      const info = lstatSync(path);
      if (info.isSymbolicLink()) {
        throw new Error(`public baseline tree contains a symlink: ${relativePath}`);
      }
      if (info.isDirectory()) {
        directories.add(relativePath);
        walk(path, relativePath);
      } else if (info.isFile()) {
        files.add(relativePath);
      } else {
        throw new Error(`public baseline tree contains a non-regular entry: ${relativePath}`);
      }
    }
  };
  walk(root, "");
  return { files, directories };
}

function sameStringSet(left: Set<string>, right: Set<string>): boolean {
  return left.size === right.size && [...left].every(value => right.has(value));
}

function loadPublicBaseline(
  repository: string,
  manifestInput: string,
  rootInput: string,
): LoadedPublicBaseline {
  if (!isAbsolute(manifestInput) || !isAbsolute(rootInput)) {
    throw new Error("public baseline manifest and directory must be absolute paths");
  }
  const suppliedRoot = resolve(rootInput);
  const rootInfo = lstatSync(suppliedRoot, { throwIfNoEntry: false });
  if (!rootInfo || rootInfo.isSymbolicLink() || !rootInfo.isDirectory()) {
    throw new Error(`public baseline directory must be a real directory: ${suppliedRoot}`);
  }
  const root = realpathSync(suppliedRoot);
  const physicalRepository = realpathSync(repository);
  if (isWithin(physicalRepository, root) || isWithin(root, physicalRepository)) {
    throw new Error(`public baseline directory must be outside the repository checkout: ${root}`);
  }

  const suppliedManifest = resolve(manifestInput);
  const manifestInfo = lstatSync(suppliedManifest, { throwIfNoEntry: false });
  if (!manifestInfo || manifestInfo.isSymbolicLink() || !manifestInfo.isFile()) {
    throw new Error("public baseline manifest must be a regular non-symlink file");
  }
  const manifest = realpathSync(suppliedManifest);
  if (manifest !== resolve(root, "manifest.json")) {
    throw new Error(`public baseline manifest must be ${resolve(root, "manifest.json")}`);
  }
  if (manifestInfo.size > MAX_BASELINE_MANIFEST_BYTES) {
    throw new Error("public baseline manifest exceeds the supported size limit");
  }
  const manifestBytes = readFileSync(manifest);
  const manifestText = manifestBytes.toString("utf8");
  if (!Buffer.from(manifestText, "utf8").equals(manifestBytes)) {
    throw new Error("public baseline manifest is not valid UTF-8");
  }
  let raw: unknown;
  try {
    raw = JSON.parse(manifestText);
  } catch (error) {
    throw new Error(`public baseline manifest is not valid JSON: ${String(error)}`);
  }
  if (!isRecord(raw)) throw new Error("public baseline manifest must be an object");
  exactKeys(raw, ["schema", "baseline_id", "authority", "files"], "public baseline manifest");
  if (raw.schema !== PUBLIC_BASELINE_SCHEMA) throw new Error("public baseline manifest schema mismatch");
  if (`${canonicalJson(raw)}\n` !== manifestText) {
    throw new Error("public baseline manifest must be canonical JSON without duplicate keys");
  }

  if (!isRecord(raw.authority)) throw new Error("public baseline authority must be an object");
  exactKeys(raw.authority, ["git_commit"], "public baseline authority");
  const authorityCommit = raw.authority.git_commit;
  if (typeof authorityCommit !== "string" || !GIT_COMMIT_RE.test(authorityCommit)) {
    throw new Error("public baseline authority.git_commit must be a full lowercase Git commit");
  }
  if (!Array.isArray(raw.files)) throw new Error("public baseline files must be an array");
  const files: PublicBaselineFile[] = [];
  let previousPath: string | null = null;
  for (const [index, item] of raw.files.entries()) {
    if (!isRecord(item)) throw new Error(`public baseline files[${index}] must be an object`);
    exactKeys(item, ["path", "sha256", "bytes"], `public baseline files[${index}]`);
    const path = safeBaselinePath(item.path, `public baseline files[${index}].path`);
    if (isBrainOwned(path)) throw new Error(`Brain-owned path must not enter a public baseline: ${path}`);
    if (isRouteShadowingOrRetired(path)) {
      throw new Error(`route-shadowing or retired path must not enter a public baseline: ${path}`);
    }
    if (previousPath !== null && compareUnicodeCodePoints(path, previousPath) <= 0) {
      throw new Error(path === previousPath
        ? `public baseline contains duplicate file path ${path}`
        : "public baseline files must be sorted by path");
    }
    previousPath = path;
    if (typeof item.sha256 !== "string" || !DIGEST_RE.test(item.sha256)) {
      throw new Error(`public baseline files[${index}].sha256 must be 64 lowercase hexadecimal characters`);
    }
    if (!Number.isSafeInteger(item.bytes) || (item.bytes as number) < 0) {
      throw new Error(`public baseline files[${index}].bytes must be a non-negative safe integer`);
    }
    files.push({ path, sha256: item.sha256, bytes: item.bytes as number });
  }

  const byPath = new Map(files.map(file => [file.path, file]));
  const missingRequired = [...REQUIRED_BASELINE_PATHS].filter(path => !byPath.has(path)).sort();
  if (missingRequired.length) {
    throw new Error(`public baseline is missing required assets: ${missingRequired.join(", ")}`);
  }
  const emptyRequired = [...REQUIRED_BASELINE_PATHS]
    .filter(path => byPath.get(path)?.bytes === 0)
    .sort();
  if (emptyRequired.length) {
    throw new Error(`required public baseline assets must be nonempty: ${emptyRequired.join(", ")}`);
  }
  for (const prefix of BASELINE_INDEX_FAMILIES) {
    const payload = files.filter(file => file.path.startsWith(prefix) && file.path !== `${prefix}manifest.json`);
    if (!payload.length) {
      throw new Error(`public baseline index family ${prefix.slice(0, -1)} has no payload file`);
    }
    const empty = payload.filter(file => file.bytes === 0).map(file => file.path);
    if (empty.length) {
      throw new Error(`public baseline index family ${prefix.slice(0, -1)} has empty payload: ${empty.join(", ")}`);
    }
  }

  const identity = {
    schema: PUBLIC_BASELINE_SCHEMA,
    authority: { git_commit: authorityCommit },
    files: files.map(file => ({ path: file.path, sha256: file.sha256, bytes: file.bytes })),
  };
  const expectedId = `sha256:${sha256(Buffer.from(
    `wikilean\0${PUBLIC_BASELINE_DOMAIN}\0canonical-json-v1\0${canonicalJson(identity)}`,
    "utf8",
  ))}`;
  if (typeof raw.baseline_id !== "string" || !BASELINE_ID_RE.test(raw.baseline_id)) {
    throw new Error("public baseline baseline_id must be sha256:<64 lowercase hex>");
  }
  if (raw.baseline_id !== expectedId) {
    throw new Error(`public baseline identity mismatch: declared ${raw.baseline_id}, computed ${expectedId}`);
  }
  const match = BASELINE_ID_RE.exec(raw.baseline_id);
  if (!match || basename(root) !== match[1]) {
    throw new Error("public baseline directory basename must match baseline_id");
  }

  const actual = scanBaselineTree(root);
  const expectedFiles = new Set(["manifest.json", ...files.map(file => file.path)]);
  const expectedDirs = expectedDirectories(expectedFiles);
  if (!sameStringSet(actual.files, expectedFiles) || !sameStringSet(actual.directories, expectedDirs)) {
    const missing = [...expectedFiles].filter(path => !actual.files.has(path)).sort();
    const extra = [...actual.files].filter(path => !expectedFiles.has(path)).sort();
    const missingDirs = [...expectedDirs].filter(path => !actual.directories.has(path)).sort();
    const extraDirs = [...actual.directories].filter(path => !expectedDirs.has(path)).sort();
    throw new Error(
      "public baseline tree does not match its complete inventory "
      + `(missing ${missing.join(", ") || "none"}; unlisted ${extra.join(", ") || "none"}; `
      + `missing directories ${missingDirs.join(", ") || "none"}; `
      + `unlisted directories ${extraDirs.join(", ") || "none"})`,
    );
  }
  return {
    schema: PUBLIC_BASELINE_SCHEMA,
    baselineId: raw.baseline_id,
    authorityCommit,
    root,
    manifestSha256: sha256(manifestBytes),
    files,
    bytes: files.reduce((sum, file) => sum + file.bytes, 0),
  };
}

function writeAll(descriptor: number, buffer: Buffer, length: number): void {
  let offset = 0;
  while (offset < length) {
    const written = writeSync(descriptor, buffer, offset, length - offset);
    if (written <= 0) throw new Error("short write while copying public baseline");
    offset += written;
  }
}

function copyPublicBaseline(baseline: LoadedPublicBaseline, destination: string): void {
  const scratch = Buffer.allocUnsafe(BASELINE_COPY_BUFFER_BYTES);
  for (const file of baseline.files) {
    const source = assertNoSymlinkComponents(
      baseline.root,
      file.path,
      `public baseline file ${file.path}`,
    );
    const target = resolveContained(destination, file.path);
    mkdirSync(dirname(target), { recursive: true });
    const sourceFd = openSync(source, constants.O_RDONLY | constants.O_NOFOLLOW);
    let targetFd: number | null = null;
    try {
      const before = fstatSync(sourceFd);
      if (!before.isFile() || before.size !== file.bytes) {
        throw new Error(`public baseline byte count mismatch for ${file.path}`);
      }
      targetFd = openSync(
        target,
        constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW,
        0o644,
      );
      const digest = createHash("sha256");
      let copied = 0;
      while (true) {
        const count = readSync(sourceFd, scratch, 0, scratch.byteLength, null);
        if (count === 0) break;
        writeAll(targetFd, scratch, count);
        digest.update(scratch.subarray(0, count));
        copied += count;
      }
      const after = fstatSync(sourceFd);
      if (
        before.dev !== after.dev
        || before.ino !== after.ino
        || before.size !== after.size
        || before.mtimeMs !== after.mtimeMs
        || before.ctimeMs !== after.ctimeMs
        || copied !== file.bytes
      ) {
        throw new Error(`public baseline file changed while copying: ${file.path}`);
      }
      if (digest.digest("hex") !== file.sha256) {
        throw new Error(`public baseline digest mismatch for ${file.path}`);
      }
    } catch (error) {
      if (targetFd !== null) closeSync(targetFd);
      targetFd = null;
      rmSync(target, { force: true });
      throw error;
    } finally {
      closeSync(sourceFd);
      if (targetFd !== null) closeSync(targetFd);
    }
  }
  const after = scanBaselineTree(baseline.root);
  const expectedFiles = new Set(["manifest.json", ...baseline.files.map(file => file.path)]);
  const expectedDirs = expectedDirectories(expectedFiles);
  if (
    !sameStringSet(after.files, expectedFiles)
    || !sameStringSet(after.directories, expectedDirs)
    || sha256(readFileSync(resolve(baseline.root, "manifest.json"))) !== baseline.manifestSha256
  ) {
    throw new Error("public baseline tree changed while copying");
  }
}

function prospectiveRealPath(path: string): string {
  let ancestor = path;
  const missing: string[] = [];
  while (!existsSync(ancestor)) {
    const parent = dirname(ancestor);
    if (parent === ancestor) throw new Error(`public directory has no existing ancestor: ${path}`);
    missing.unshift(basename(ancestor));
    ancestor = parent;
  }
  const physicalAncestor = realpathSync(ancestor);
  if (!statSync(physicalAncestor).isDirectory()) {
    throw new Error(`public directory ancestor must be a directory: ${ancestor}`);
  }
  return resolve(physicalAncestor, ...missing);
}

function prepareIsolatedPublicDir(
  repository: string,
  requested: string,
  forbiddenRoot?: string,
): string {
  if (!isAbsolute(requested)) {
    throw new Error("--public-dir must be an absolute path");
  }
  const requestedPath = resolve(requested);
  const physicalRepository = realpathSync(repository);
  const existing = lstatSync(requestedPath, { throwIfNoEntry: false });
  if (existing?.isSymbolicLink()) {
    throw new Error(`--public-dir must not be a symlink: ${requestedPath}`);
  }
  if (existing && !existing.isDirectory()) {
    throw new Error(`--public-dir must be a directory when it exists: ${requestedPath}`);
  }
  if (existing && readdirSync(requestedPath).length !== 0) {
    throw new Error(`--public-dir must be absent or empty: ${requestedPath}`);
  }
  const prospective = existing ? realpathSync(requestedPath) : prospectiveRealPath(requestedPath);
  if (isWithin(physicalRepository, prospective)) {
    throw new Error(`--public-dir must be outside the repository checkout: ${requestedPath}`);
  }
  if (forbiddenRoot && (isWithin(forbiddenRoot, prospective) || isWithin(prospective, forbiddenRoot))) {
    throw new Error("--public-dir must not overlap the public baseline directory");
  }

  mkdirSync(requestedPath, { recursive: true });
  const physicalPublic = realpathSync(requestedPath);
  if (isWithin(physicalRepository, physicalPublic)) {
    throw new Error(`--public-dir resolved inside the repository checkout: ${requestedPath}`);
  }
  return physicalPublic;
}

function countMathlibDeclarations(path: string): number {
  let value: unknown;
  try {
    value = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(`public baseline mathlib index is not valid JSON: ${String(error)}`);
  }
  if (!Array.isArray(value)) {
    throw new Error("public baseline mathlib index must be an array");
  }
  return value.length;
}

export function buildPublic(options: BuildPublicOptions): BuildPublicResult {
  const started = process.hrtime.bigint();
  const wiki = resolve(options.wikiDir);
  const repository = resolve(wiki, "..");
  const site = resolve(repository, "site");
  const isolated = options.publicDir !== undefined;
  const hasBaselineManifest = options.publicBaselineManifest !== undefined;
  const hasBaselineDir = options.publicBaselineDir !== undefined;
  if (hasBaselineManifest !== hasBaselineDir) {
    throw new Error("public baseline manifest and directory must be supplied together");
  }
  if (isolated && !hasBaselineManifest) {
    throw new Error(
      "--public-dir requires --public-baseline-manifest and --public-baseline-dir",
    );
  }
  if (!isolated && hasBaselineManifest) {
    throw new Error("public baseline inputs are only valid with an explicit --public-dir");
  }
  const loadedBaseline = isolated
    ? loadPublicBaseline(
      repository,
      options.publicBaselineManifest as string,
      options.publicBaselineDir as string,
    )
    : null;
  // Promotion builds use an explicit empty directory outside the checkout so
  // ignored/stale wiki/public files can never leak into the deployment. Keep
  // the historical in-checkout default for the shadow/nightly build path.
  const pub = options.publicDir === undefined
    ? resolve(wiki, "public")
    : prepareIsolatedPublicDir(repository, options.publicDir, loadedBaseline?.root);
  const pubAssets = resolve(pub, "assets");

  mkdirSync(pubAssets, { recursive: true });

  let publicBaseline: PublicBaselineBuildResult | null = null;
  if (loadedBaseline) {
    // Promotion builds consume only the immutable, content-addressed baseline.
    // No mutable or ignored checkout output participates in this tree.
    copyPublicBaseline(loadedBaseline, pub);
    publicBaseline = {
      schema: loadedBaseline.schema,
      baseline_id: loadedBaseline.baselineId,
      authority_commit: loadedBaseline.authorityCommit,
      root: loadedBaseline.root,
      files: loadedBaseline.files.length,
      bytes: loadedBaseline.bytes,
    };
  } else {
    // Preserve the historical shadow-build behavior for wiki/public.
    const fromSite = ["style.css", "script.js", "review.css"];
    for (const file of fromSite) {
      const source = resolve(site, "assets", file);
      if (existsSync(source)) copyFileSync(source, resolve(pubAssets, file));
    }
    for (const file of ["editor.js"]) {
      const source = resolve(wiki, "assets", file);
      if (existsSync(source)) copyFileSync(source, resolve(pubAssets, file));
    }

    // index.html + sitemap.xml + about.html are served dynamically from D1 (src/
    // home.ts via GET /, GET /sitemap.xml, GET /about) and must not shadow Worker routes.
    const shellFiles = [
      "concepts.html", "404.html", "robots.txt", "wikilean.ttl",
    ];
    for (const file of shellFiles) {
      const source = resolve(site, "out", file);
      if (existsSync(source)) copyFileSync(source, resolve(pub, file));
    }
  }

  // Brain assets come only from one explicit, verified frozen release. The staging
  // module publishes immutable current/previous namespaces and byte-identical
  // compatibility aliases as one filesystem transaction.
  const brain = stageBrainPublicRelease({
    ...options.brain,
    destination: resolve(pubAssets, "brain"),
    // The page is release-coupled too: copy it from the frozen release, never
    // from mutable site/out, and activate it with the matching asset tree.
    brainPageDestination: resolve(pub, "brain.html"),
  });

  let mathlibDeclarations: number;
  if (isolated) {
    mathlibDeclarations = countMathlibDeclarations(resolve(pubAssets, "mathlib-index.json"));
  } else {
    // wiki/public is generated-but-never-wiped: an older checkout may have left
    // pages that would shadow the Worker's intended routes.
    for (const file of [
      "map.html", "graph.html", "atlas.html", "about.html", "map_data.json", "map_data_v2.json",
    ]) {
      rmSync(resolve(pub, file), { force: true });
    }
    mathlibDeclarations = buildMathlibIndex(site, resolve(pubAssets, "mathlib-index.json"));
  }

  return {
    schema: "wikilean.public-build-result/v1",
    publicDir: pub,
    mathlibDeclarations,
    publicBaseline,
    brain,
    duration_ms: Math.round(Number(process.hrtime.bigint() - started) / 1e3) / 1e3,
    max_rss_bytes: Math.round(process.resourceUsage().maxRSS * 1024),
  };
}

function option(args: string[], name: string, env: string): string | undefined {
  const index = args.indexOf(name);
  if (index !== -1) {
    const value = args[index + 1];
    if (!value || value.startsWith("--")) throw new Error(`${name} requires a value`);
    return value;
  }
  return process.env[env];
}

function positiveOption(args: string[], name: string, env: string): number | undefined {
  const raw = option(args, name, env);
  if (raw === undefined || raw === "") return undefined;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${name} must be a positive safe integer`);
  return value;
}

function nonNegativeOption(args: string[], name: string, env: string): number | undefined {
  const raw = option(args, name, env);
  if (raw === undefined || raw === "") return undefined;
  const value = Number(raw);
  if (!Number.isSafeInteger(value) || value < 0) throw new Error(`${name} must be a non-negative safe integer`);
  return value;
}

export function buildPublicFromArgs(args: string[], wikiDir = process.cwd()): BuildPublicResult {
  const known = new Set([
    "--public-dir",
    "--public-baseline-manifest",
    "--public-baseline-dir",
    "--brain-release-manifest",
    "--brain-release-dir",
    "--brain-previous-release-manifest",
    "--brain-previous-release-dir",
    "--brain-audited-at",
    "--brain-max-objects",
    "--brain-max-bytes",
    "--brain-max-file-bytes",
    "--brain-min-free-bytes",
  ]);
  const seen = new Set<string>();
  for (let index = 0; index < args.length; index += 2) {
    const flag = args[index];
    if (!known.has(flag)) throw new Error(`unknown option ${flag}`);
    if (seen.has(flag)) throw new Error(`option ${flag} may be supplied only once`);
    if (index + 1 >= args.length || args[index + 1].startsWith("--")) {
      throw new Error(`${flag} requires a value`);
    }
    seen.add(flag);
  }
  const brainManifest = option(args, "--brain-release-manifest", "BRAIN_RELEASE_MANIFEST");
  const brainReleaseDir = option(args, "--brain-release-dir", "BRAIN_RELEASE_DIR");
  if (!brainManifest || !brainReleaseDir) {
    throw new Error("--brain-release-manifest and --brain-release-dir are required for verified Brain staging");
  }
  const previousManifestPath = option(
    args,
    "--brain-previous-release-manifest",
    "BRAIN_PREVIOUS_RELEASE_MANIFEST",
  );
  const previousReleaseDir = option(
    args,
    "--brain-previous-release-dir",
    "BRAIN_PREVIOUS_RELEASE_DIR",
  );
  if ((previousManifestPath === undefined) !== (previousReleaseDir === undefined)) {
    throw new Error("previous Brain manifest and release directory must be supplied together");
  }
  const publicDir = option(args, "--public-dir", "WIKILEAN_PUBLIC_DIR");
  const publicBaselineManifest = option(
    args,
    "--public-baseline-manifest",
    "WIKILEAN_PUBLIC_BASELINE_MANIFEST",
  );
  const publicBaselineDir = option(
    args,
    "--public-baseline-dir",
    "WIKILEAN_PUBLIC_BASELINE_DIR",
  );
  if ((publicBaselineManifest === undefined) !== (publicBaselineDir === undefined)) {
    throw new Error("public baseline manifest and directory must be supplied together");
  }
  if (publicDir !== undefined && publicBaselineManifest === undefined) {
    throw new Error(
      "--public-dir requires --public-baseline-manifest and --public-baseline-dir",
    );
  }
  if (publicDir === undefined && publicBaselineManifest !== undefined) {
    throw new Error("public baseline inputs require an explicit --public-dir");
  }

  return buildPublic({
    wikiDir,
    publicDir,
    publicBaselineManifest,
    publicBaselineDir,
    brain: {
      manifestPath: brainManifest,
      releaseDir: brainReleaseDir,
      previousManifestPath,
      previousReleaseDir,
      auditedAt: option(args, "--brain-audited-at", "BRAIN_AUDITED_AT"),
      maxObjects: positiveOption(args, "--brain-max-objects", "BRAIN_PUBLIC_MAX_OBJECTS"),
      maxBytes: positiveOption(args, "--brain-max-bytes", "BRAIN_PUBLIC_MAX_BYTES"),
      maxFileBytes: positiveOption(args, "--brain-max-file-bytes", "BRAIN_PUBLIC_MAX_FILE_BYTES"),
      minFreeBytes: nonNegativeOption(args, "--brain-min-free-bytes", "BRAIN_PUBLIC_MIN_FREE_BYTES"),
    },
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const result = buildPublicFromArgs(process.argv.slice(2));
  console.log(JSON.stringify({
    schema: result.schema,
    public_dir: result.publicDir,
    mathlib_declarations: result.mathlibDeclarations,
    public_baseline: result.publicBaseline,
    duration_ms: result.duration_ms,
    max_rss_bytes: result.max_rss_bytes,
    brain: result.brain,
  }));
}
