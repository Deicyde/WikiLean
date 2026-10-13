// `npm run deploy` guard (scripts/deploy.ts). Pins the decision logic only:
// no network, no git, no wrangler (the promoter runs this unit suite under a
// restricted environment). A manual deploy proceeds only when the local Brain
// selector equals the live one in every field (the upload replaces it) and the
// local release directory holds the named manifest; real deploys accept only
// label flags; the deploy is stamped with the checkout's git SHA.

import { createHash } from "node:crypto";
import { describe, it, expect } from "vitest";
import { checkManifest, checkSelector, gitStamp, parseExtra, wranglerArgs } from "../scripts/deploy.js";

const MANIFEST_BYTES = new TextEncoder().encode('{"schema":"wikilean.release/v1"}');
const MANIFEST_SHA = createHash("sha256").update(MANIFEST_BYTES).digest("hex");
const LIVE = {
  schema: "wikilean.release-selector/v2",
  release_id: "sha256:" + "8".repeat(64),
  release: "8".repeat(64),
  manifest_sha256: MANIFEST_SHA,
  manifest: `/assets/brain/releases/${MANIFEST_SHA}/release.json`,
  audited_at: "2026-10-04T17:25:04Z",
};
const live = JSON.stringify(LIVE);

describe("checkSelector", () => {
  it("passes when the local selector equals the live one, whatever the key order or whitespace", () => {
    const reordered = Object.fromEntries(Object.entries(LIVE).reverse());
    expect(checkSelector(JSON.stringify(reordered, null, 2), live)).toEqual({ ok: true });
  });

  it("refuses any differing field: release, manifest, audit time, or rollback pointers", () => {
    const variants: Array<[string, Record<string, unknown>]> = [
      ["release_id", { ...LIVE, release_id: "sha256:" + "9".repeat(64) }],
      ["manifest_sha256", { ...LIVE, manifest_sha256: "e".repeat(64) }],
      ["audited_at", { ...LIVE, audited_at: "2026-10-13T00:00:00Z" }],
      ["previous_release_id", { ...LIVE, previous_release_id: "sha256:" + "1".repeat(64) }],
    ];
    for (const [key, local] of variants) {
      const r = checkSelector(JSON.stringify(local), live);
      expect(r.ok, key).toBe(false);
      if (!r.ok) {
        expect(r.reason).toContain(key);
        expect(r.reason).toContain("brain-promote-release.sh");
        expect(r.reason).toContain("--brain-audited-at");
      }
    }
  });

  it("fails closed: missing local selector, unreachable live selector, malformed or v1 selectors", () => {
    expect(checkSelector(null, live).ok).toBe(false);
    expect(checkSelector(live, null).ok).toBe(false);
    expect(checkSelector("{not json", live).ok).toBe(false);
    expect(checkSelector(live, "<html>").ok).toBe(false);
    expect(checkSelector(JSON.stringify({ ...LIVE, schema: "wikilean.release-selector/v1" }), live).ok).toBe(false);
    expect(checkSelector("null", live).ok).toBe(false);
  });
});

describe("checkManifest", () => {
  it("requires the release directory to hold the exact manifest the selector names", () => {
    expect(checkManifest(live, MANIFEST_BYTES)).toEqual({ ok: true });
    expect(checkManifest(live, null).ok).toBe(false);
    const tampered = checkManifest(live, new TextEncoder().encode("{}"));
    expect(tampered.ok).toBe(false);
    if (!tampered.ok) expect(tampered.reason).toContain("hashes to");
  });
});

describe("parseExtra", () => {
  it("real deploys accept only --message/--tag with values", () => {
    expect(parseExtra([])).toEqual({ dryRun: false });
    expect(parseExtra(["--message", "hotfix", "--tag", "manual-1"])).toEqual({ dryRun: false });
    for (const bad of [["--assets", "/tmp/x"], ["--config", "x.jsonc"], ["-c", "x"], ["--env", "prod"], ["src/index.ts"], ["--message"]]) {
      expect("error" in parseExtra(bad), bad.join(" ")).toBe(true);
    }
  });

  it("--dry-run alone passes anything through; ambiguous forms are refused", () => {
    expect(parseExtra(["--dry-run", "--assets", "/tmp/x", "--outdir", "/tmp/o"])).toEqual({ dryRun: true });
    for (const bad of [["--dry-run", "false"], ["--dry-run=false"], ["--dry-run", "--no-dry-run"]]) {
      expect("error" in parseExtra(bad), bad.join(" ")).toBe(true);
    }
  });
});

describe("git stamp", () => {
  it("is the 40-hex HEAD, suffixed -dirty for an unclean tree, and passed as the LAST wrangler arg", () => {
    const sha = "a".repeat(40);
    expect(gitStamp(sha, false)).toBe(sha);
    expect(gitStamp(sha, true)).toBe(`${sha}-dirty`);
    expect(() => gitStamp("HEAD", false)).toThrow();
    // --var is a greedy array in wrangler: anything after it would be eaten.
    expect(wranglerArgs(sha, ["--message", "m"])).toEqual(["deploy", "--message", "m", "--var", `WIKILEAN_GIT_SHA:${sha}`]);
  });
});
