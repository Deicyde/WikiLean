// Guarded `npm run deploy`: a manual Worker deploy from this checkout.
//
// The production path is site/ops/brain-promote-release.sh. The promoter
// builds and uploads the Worker by running wrangler directly and never
// invokes this script. Running a manual deploy while a promotion attempt is
// open still changes what the promoter observes, so don't.
//
// A plain `wrangler deploy` uploads ./public as the entire asset tree,
// including the Brain release selector (public/assets/brain/current.json). To
// keep a manual deploy from changing which Brain release production serves,
// this wrapper refuses unless:
//   - the local selector equals the live one in every field (release,
//     manifest, audited_at and the previous_* rollback pointers, all of which
//     the upload would replace), and
//   - the local release directory holds the manifest the selector names
//     (sha256 of releases/<manifest_sha256>/release.json).
// It does NOT verify the rest of ./public (decl/suffix/premise indexes,
// editor.js, stray files); build it with build-public from a verified release.
// It stamps the Worker with the checkout's git SHA (the WIKILEAN_GIT_SHA var,
// served by GET /api/version; "-dirty" when wiki/src or the Worker config has
// uncommitted changes).
//
// Usage (from wiki/): npm run deploy [-- --message <text>] [-- --tag <tag>]
// `npm run deploy -- --dry-run [any wrangler args]` uploads nothing and skips
// the checks. This guards the documented shortcut; it is not a security
// boundary (`npx wrangler deploy` bypasses it).

import { execFileSync, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, realpathSync } from "node:fs";
import { pathToFileURL } from "node:url";

export const LIVE_SELECTOR_URL = "https://wikilean.jackmccarthy.org/assets/brain/current.json";
const PUBLIC_DIR = new URL("../public/", import.meta.url);
const LOCAL_SELECTOR = new URL("assets/brain/current.json", PUBLIC_DIR);

export type Check = { ok: true } | { ok: false; reason: string };

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  if (value !== null && typeof value === "object") {
    const o = value as Record<string, unknown>;
    return `{${Object.keys(o).sort().map((k) => `${JSON.stringify(k)}:${canonical(o[k])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "undefined";
}

function parseSelector(raw: string): Record<string, unknown> | null {
  try {
    const o = JSON.parse(raw) as unknown;
    if (typeof o !== "object" || o === null || Array.isArray(o)) return null;
    const r = o as Record<string, unknown>;
    return r.schema === "wikilean.release-selector/v2" && typeof r.manifest_sha256 === "string" ? r : null;
  } catch {
    return null;
  }
}

export function checkSelector(local: string | null, live: string | null): Check {
  if (local === null) {
    return { ok: false, reason: "public/assets/brain/current.json is missing; run build-public with a verified Brain release first" };
  }
  if (live === null) return { ok: false, reason: `could not fetch the live selector (${LIVE_SELECTOR_URL})` };
  const a = parseSelector(local);
  const b = parseSelector(live);
  if (a === null) return { ok: false, reason: "local current.json is not a v2 release selector" };
  if (b === null) return { ok: false, reason: "live current.json is not a v2 release selector" };
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (canonical(a[k]) !== canonical(b[k])) {
      return {
        ok: false,
        reason: `local selector ${k}=${canonical(a[k])} but production serves ${canonical(b[k])}; ` +
          "the upload would replace it. Use site/ops/brain-promote-release.sh, or rebuild public with build-public " +
          "--brain-audited-at and --brain-previous-release-manifest/-dir set to reproduce the live selector",
      };
    }
  }
  return { ok: true };
}

// The release directory the selector points at must hold that exact manifest.
export function checkManifest(selectorRaw: string, manifestBytes: Uint8Array | null): Check {
  const sel = parseSelector(selectorRaw);
  if (sel === null) return { ok: false, reason: "local current.json is not a v2 release selector" };
  if (manifestBytes === null) return { ok: false, reason: `public${String(sel.manifest)} is missing` };
  const digest = createHash("sha256").update(manifestBytes).digest("hex");
  return digest === sel.manifest_sha256
    ? { ok: true }
    : { ok: false, reason: `public${String(sel.manifest)} hashes to ${digest}, not manifest_sha256 ${String(sel.manifest_sha256)}` };
}

// Real deploys accept only labels; anything that could retarget the upload
// (--assets, --config, --env, --name, a positional script path, ...) is refused.
const LABEL_FLAGS = new Set(["--message", "--tag"]);

export function parseExtra(extra: string[]): { dryRun: boolean } | { error: string } {
  if (extra.some((a) => a === "--no-dry-run" || a.startsWith("--dry-run=") || a === "false")) {
    return { error: "ambiguous --dry-run; pass it alone" };
  }
  if (extra.includes("--dry-run")) return { dryRun: true };
  for (let i = 0; i < extra.length; i += 2) {
    if (!LABEL_FLAGS.has(extra[i]) || i + 1 >= extra.length) {
      return { error: `unsupported argument ${extra[i]}; a real deploy accepts only --message <text> and --tag <tag>` };
    }
  }
  return { dryRun: false };
}

export function gitStamp(sha: string, dirty: boolean): string {
  if (!/^[0-9a-f]{40}$/.test(sha)) throw new Error(`not a git commit: ${sha}`);
  return dirty ? `${sha}-dirty` : sha;
}

// The var goes last: wrangler's --var is a greedy array and would swallow a
// following positional.
export function wranglerArgs(stamp: string, extra: string[]): string[] {
  return ["deploy", ...extra, "--var", `WIKILEAN_GIT_SHA:${stamp}`];
}

async function fetchText(url: string): Promise<string | null> {
  try {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    if (!res.ok) {
      console.error(`GET ${url}: HTTP ${res.status}`);
      return null;
    }
    return await res.text();
  } catch (error) {
    console.error(`GET ${url}: ${String(error)}`);
    return null;
  }
}

function readOrNull(url: URL): Buffer | null {
  try {
    return readFileSync(url);
  } catch {
    return null;
  }
}

async function main(extra: string[]): Promise<number> {
  if (process.env.npm_config_dry_run) {
    console.error("deploy refused: npm consumed --dry-run; use `npm run deploy -- --dry-run`");
    return 1;
  }
  const parsed = parseExtra(extra);
  if ("error" in parsed) {
    console.error(`deploy refused: ${parsed.error}`);
    return 1;
  }
  if (!parsed.dryRun) {
    const local = readOrNull(LOCAL_SELECTOR)?.toString("utf8") ?? null;
    let check = checkSelector(local, local === null ? null : await fetchText(LIVE_SELECTOR_URL));
    if (check.ok && local !== null) {
      const manifestPath = String(parseSelector(local)?.manifest ?? "").replace(/^\//, "");
      check = checkManifest(local, readOrNull(new URL(manifestPath, PUBLIC_DIR)));
    }
    if (!check.ok) {
      console.error(`deploy refused: ${check.reason}`);
      return 1;
    }
  }
  const git = (args: string[]) => execFileSync("git", args, { encoding: "utf8" }).trim();
  const dirty = git(["status", "--porcelain", "--", "src", "wrangler.jsonc", "package.json", "package-lock.json"]) !== "";
  const stamp = gitStamp(git(["rev-parse", "HEAD"]), dirty);
  console.log(`deploying with WIKILEAN_GIT_SHA=${stamp}`);
  const run = spawnSync("wrangler", wranglerArgs(stamp, extra), { stdio: "inherit" });
  if (run.error) {
    console.error(`could not run wrangler: ${String(run.error)}`);
    return 1;
  }
  return run.status ?? 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href) {
  process.exitCode = await main(process.argv.slice(2));
}
