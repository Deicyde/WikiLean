// GET /api/version (src/version.ts): the deploy stamp (WIKILEAN_GIT_SHA from
// `npm run deploy`), the Cloudflare Worker Version metadata, and the selected
// Brain release with the commit it was built from. Pins the null fallbacks
// (promoter deploys carry no stamp; a Brain outage never 503s this route),
// the stamp validation, and no-store caching.

import { beforeEach, describe, it, expect } from "vitest";
import { setup, get, blockNetwork } from "./helpers/harness.js";
import { installBrainFixture } from "./helpers/brain-fixture.js";
import { _resetBrainAssetMemo } from "../src/brain.js";
import { parseGitStamp } from "../src/version.js";

blockNetwork();
beforeEach(() => _resetBrainAssetMemo());

const SHA = "37b7036d0123456789abcdef0123456789abcdef";

describe("GET /api/version", () => {
  it("reports the git stamp, Worker Version metadata, and Brain release commit", async () => {
    const h = setup();
    const { releaseId, manifestSha256 } = installBrainFixture(h.env);
    h.env.WIKILEAN_GIT_SHA = SHA;
    h.env.CF_VERSION_METADATA = { id: "ver-123", tag: "brain-8305822b0083-27c7c461cb", timestamp: "2026-10-04T17:25:30Z" };
    const res = await get(h.env, "/api/version");
    expect(res.status).toBe(200);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    expect(await res.json()).toEqual({
      ok: true,
      worker: {
        git_sha: SHA,
        git_dirty: false,
        version_id: "ver-123",
        version_tag: "brain-8305822b0083-27c7c461cb",
        uploaded_at: "2026-10-04T17:25:30Z",
      },
      brain_release: { release_id: releaseId, manifest_sha256: manifestSha256, git_commit: "0".repeat(40) },
    });
  });

  it("promoter-style deploy: no stamp, no metadata binding, no Brain release → nulls, still 200", async () => {
    const h = setup(); // harness ASSETS 404s every path, so no selector resolves
    const res = await get(h.env, "/api/version");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      ok: true,
      worker: { git_sha: null, git_dirty: null, version_id: null, version_tag: null, uploaded_at: null },
      brain_release: null,
    });
  });
});

describe("parseGitStamp", () => {
  it("accepts a 40-hex SHA with an optional -dirty suffix and rejects anything else", () => {
    expect(parseGitStamp(SHA)).toEqual({ git_sha: SHA, git_dirty: false });
    expect(parseGitStamp(`${SHA}-dirty`)).toEqual({ git_sha: SHA, git_dirty: true });
    for (const bad of [undefined, "", "abc123", SHA.toUpperCase(), `${SHA}-clean`, `${SHA}\n`, `<script>${SHA}`]) {
      expect(parseGitStamp(bad)).toEqual({ git_sha: null, git_dirty: null });
    }
  });
});
