// `npm run deploy` guard (scripts/deploy.ts). Pins the decision logic only:
// no network, no git, no wrangler (the promoter runs this unit suite under a
// restricted environment). A manual deploy proceeds only when the local
// Brain selector names the same release and manifest as production; the
// deploy is stamped with the checkout's git SHA.

import { describe, it, expect } from "vitest";
import { checkSelector, gitStamp, wranglerArgs } from "../scripts/deploy.js";

const LIVE = {
  schema: "wikilean.release-selector/v2",
  release_id: "sha256:" + "8".repeat(64),
  release: "8".repeat(64),
  manifest_sha256: "f".repeat(64),
  manifest: `/assets/brain/releases/${"f".repeat(64)}/release.json`,
  audited_at: "2026-10-04T17:25:04Z",
};
const live = JSON.stringify(LIVE);

describe("checkSelector", () => {
  it("passes when the local selector names the live release, ignoring audit time and rollback pointers", () => {
    const local = JSON.stringify({
      ...LIVE,
      audited_at: "2026-10-13T00:00:00Z",
      previous_release_id: "sha256:" + "1".repeat(64),
    }, null, 2);
    expect(checkSelector(local, live)).toEqual({ ok: true });
  });

  it("refuses a different release or manifest", () => {
    const otherRelease = JSON.stringify({ ...LIVE, release_id: "sha256:" + "9".repeat(64) });
    const r1 = checkSelector(otherRelease, live);
    expect(r1.ok).toBe(false);
    if (!r1.ok) {
      expect(r1.reason).toContain("release_id");
      expect(r1.reason).toContain("brain-promote-release.sh");
    }
    const otherManifest = JSON.stringify({ ...LIVE, manifest_sha256: "e".repeat(64), manifest: `/assets/brain/releases/${"e".repeat(64)}/release.json` });
    expect(checkSelector(otherManifest, live).ok).toBe(false);
  });

  it("fails closed: missing local selector, unreachable live selector, malformed or v1 selectors", () => {
    expect(checkSelector(null, live).ok).toBe(false);
    expect(checkSelector(live, null).ok).toBe(false);
    expect(checkSelector("{not json", live).ok).toBe(false);
    expect(checkSelector(live, "<html>").ok).toBe(false);
    const v1 = JSON.stringify({ ...LIVE, schema: "wikilean.release-selector/v1" });
    expect(checkSelector(v1, live).ok).toBe(false);
    expect(checkSelector("null", live).ok).toBe(false);
  });
});

describe("git stamp", () => {
  it("is the 40-hex HEAD, suffixed -dirty for an unclean tree, and passed as a wrangler var", () => {
    const sha = "a".repeat(40);
    expect(gitStamp(sha, false)).toBe(sha);
    expect(gitStamp(sha, true)).toBe(`${sha}-dirty`);
    expect(() => gitStamp("HEAD", false)).toThrow();
    expect(wranglerArgs(sha, ["--dry-run"])).toEqual(["deploy", "--var", `WIKILEAN_GIT_SHA:${sha}`, "--dry-run"]);
  });
});
