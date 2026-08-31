// Populates wiki/public/ (the Worker's static-asset dir) from the existing
// static-site build: shared CSS/JS and the shell pages (index/concepts/about/
// 404/sitemap/robots). Article pages are served dynamically by the Worker.
import { copyFileSync, existsSync, mkdirSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { buildMathlibIndex } from "./build-mathlib-index.ts";
import {
  stageBrainPublicRelease,
  type StageBrainPublicOptions,
  type StageBrainPublicResult,
} from "./brain-release-public.ts";

export interface BuildPublicOptions {
  wikiDir: string;
  brain: Omit<StageBrainPublicOptions, "destination">;
}

export interface BuildPublicResult {
  schema: "wikilean.public-build-result/v1";
  publicDir: string;
  mathlibDeclarations: number;
  brain: StageBrainPublicResult;
  duration_ms: number;
  max_rss_bytes: number;
}

export function buildPublic(options: BuildPublicOptions): BuildPublicResult {
  const started = process.hrtime.bigint();
  const wiki = resolve(options.wikiDir);
  const site = resolve(wiki, "..", "site");
  const pub = resolve(wiki, "public");
  const pubAssets = resolve(pub, "assets");

  mkdirSync(pubAssets, { recursive: true });

  // Shared article assets + editor styles come from the static site; the live
  // editor logic is wiki-specific.
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

  // public/ is generated-but-never-wiped: an older checkout may have left pages
  // that would shadow the Worker's intended routes.
  for (const file of [
    "map.html", "graph.html", "atlas.html", "about.html", "map_data.json", "map_data_v2.json",
  ]) {
    rmSync(resolve(pub, file), { force: true });
  }

  const mathlibDeclarations = buildMathlibIndex(site, resolve(pubAssets, "mathlib-index.json"));
  return {
    schema: "wikilean.public-build-result/v1",
    publicDir: pub,
    mathlibDeclarations,
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
    "--brain-release-manifest",
    "--brain-release-dir",
    "--brain-previous-release-manifest",
    "--brain-previous-release-dir",
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

  return buildPublic({
    wikiDir,
    brain: {
      manifestPath: brainManifest,
      releaseDir: brainReleaseDir,
      previousManifestPath,
      previousReleaseDir,
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
    duration_ms: result.duration_ms,
    max_rss_bytes: result.max_rss_bytes,
    brain: result.brain,
  }));
}
