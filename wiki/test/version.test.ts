// GET /api/version (src/version.ts): the Cloudflare Worker Version metadata
// and the selected Brain release with the commit it was built from. Pins the
// null fallbacks (no metadata binding; a Brain outage never 503s this route)
// and no-store caching.

import { beforeEach, describe, it, expect } from "vitest";
import { setup, get, blockNetwork } from "./helpers/harness.js";
import { installBrainFixture } from "./helpers/brain-fixture.js";
import { _resetBrainAssetMemo } from "../src/brain.js";

blockNetwork();
beforeEach(() => _resetBrainAssetMemo());

describe("GET /api/version", () => {
  it("reports Worker Version metadata and the Brain release commit, uncached", async () => {
    const h = setup();
    const { releaseId, manifestSha256 } = installBrainFixture(h.env);
    h.env.CF_VERSION_METADATA = { id: "ver-123", tag: "brain-8305822b0083-27c7c461cb", timestamp: "2026-10-04T17:25:30Z" };
    const res = await get(h.env, "/api/version");
    expect(res.status).toBe(200);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    expect(await res.json()).toEqual({
      ok: true,
      worker: {
        version_id: "ver-123",
        version_tag: "brain-8305822b0083-27c7c461cb",
        uploaded_at: "2026-10-04T17:25:30Z",
      },
      brain_release: { release_id: releaseId, manifest_sha256: manifestSha256, git_commit: "0".repeat(40) },
    });
  });

  it("no metadata binding and no Brain release → nulls, still 200 and uncached", async () => {
    const h = setup(); // harness ASSETS 404s every path, so no selector resolves
    const res = await get(h.env, "/api/version");
    expect(res.status).toBe(200);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    expect(await res.json()).toEqual({
      ok: true,
      worker: { version_id: null, version_tag: null, uploaded_at: null },
      brain_release: null,
    });
  });
});
