// Guarded `npm run deploy`: a manual Worker deploy from this checkout.
//
// The production path is site/ops/brain-promote-release.sh. The promoter
// builds and uploads the Worker by running wrangler directly, never this
// script, so nothing here can block or alter a promotion.
//
// A plain `wrangler deploy` uploads ./public as the entire asset tree,
// including the Brain release selector (public/assets/brain/current.json). To
// keep a manual deploy from swapping the production Brain release, this
// wrapper refuses unless the local selector names the same release and
// manifest as the live one. It also stamps the Worker with the checkout's git
// SHA (the WIKILEAN_GIT_SHA var, served by GET /api/version).
//
// Usage (from wiki/): npm run deploy [-- <extra wrangler deploy args>]
// With --dry-run nothing is uploaded, so the selector check is skipped.
// This is a guard against the documented shortcut, not a security boundary:
// `npx wrangler deploy` bypasses it.

import { execFileSync, spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

export const LIVE_SELECTOR_URL = "https://wikilean.jackmccarthy.org/assets/brain/current.json";
const LOCAL_SELECTOR = new URL("../public/assets/brain/current.json", import.meta.url);

// The fields that pick which release production serves. audited_at and the
// previous_* rollback pointers legitimately differ between a fresh
// build-public run and the promoted selector, so bytes are not compared.
const IDENTITY_KEYS = ["schema", "release_id", "manifest_sha256", "manifest"] as const;

export type SelectorCheck = { ok: true } | { ok: false; reason: string };

function identity(raw: string): Record<string, unknown> | null {
  try {
    const o = JSON.parse(raw) as Record<string, unknown>;
    if (typeof o !== "object" || o === null || o.schema !== "wikilean.release-selector/v2") return null;
    return Object.fromEntries(IDENTITY_KEYS.map((k) => [k, o[k]]));
  } catch {
    return null;
  }
}

export function checkSelector(local: string | null, live: string | null): SelectorCheck {
  if (local === null) {
    return { ok: false, reason: "public/assets/brain/current.json is missing; run build-public with a verified Brain release first" };
  }
  if (live === null) return { ok: false, reason: `could not fetch the live selector (${LIVE_SELECTOR_URL})` };
  const a = identity(local);
  const b = identity(live);
  if (a === null) return { ok: false, reason: "local current.json is not a v2 release selector" };
  if (b === null) return { ok: false, reason: "live current.json is not a v2 release selector" };
  for (const k of IDENTITY_KEYS) {
    if (a[k] !== b[k]) {
      return {
        ok: false,
        reason: `local selector ${k}=${String(a[k])} but production serves ${String(b[k])}; ` +
          "a plain deploy would replace the production Brain release (use site/ops/brain-promote-release.sh)",
      };
    }
  }
  return { ok: true };
}

export function gitStamp(sha: string, dirty: boolean): string {
  if (!/^[0-9a-f]{40}$/.test(sha)) throw new Error(`not a git commit: ${sha}`);
  return dirty ? `${sha}-dirty` : sha;
}

export function wranglerArgs(stamp: string, extra: string[]): string[] {
  return ["deploy", "--var", `WIKILEAN_GIT_SHA:${stamp}`, ...extra];
}

async function fetchText(url: string): Promise<string | null> {
  try {
    const res = await fetch(url, { headers: { Accept: "application/json" } });
    return res.ok ? await res.text() : null;
  } catch {
    return null;
  }
}

async function main(extra: string[]): Promise<number> {
  if (!extra.includes("--dry-run")) {
    let local: string | null = null;
    try {
      local = readFileSync(LOCAL_SELECTOR, "utf8");
    } catch {
      local = null;
    }
    const check = checkSelector(local, local === null ? null : await fetchText(LIVE_SELECTOR_URL));
    if (!check.ok) {
      console.error(`deploy refused: ${check.reason}`);
      return 1;
    }
  }
  const git = (args: string[]) => execFileSync("git", args, { encoding: "utf8" }).trim();
  const stamp = gitStamp(git(["rev-parse", "HEAD"]), git(["status", "--porcelain"]) !== "");
  console.log(`deploying with WIKILEAN_GIT_SHA=${stamp}`);
  const run = spawnSync("wrangler", wranglerArgs(stamp, extra), { stdio: "inherit" });
  return run.status ?? 1;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exitCode = await main(process.argv.slice(2));
}
