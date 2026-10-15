// GET /api/version — which Worker version and which Brain release are live.
// Public, uncached, no D1. (Under /api, so the RESERVED set already covers it.)
//
// worker: the Cloudflare Worker Version from the version_metadata binding
// (id, tag, upload timestamp). The Brain promoter
// (site/ops/brain-promote-release.sh) tags its deploys
// "brain-<release hex12>-<attempt>". That tag is evidence of a promoter
// deploy, not proof: it is a free-form wrangler --tag that anyone with deploy
// credentials could set. For a promoter deploy the Worker source is
// brain_release.git_commit's: the promoter only deploys a checkout whose
// release-affecting paths (wiki/src included) match the release's authority
// commit (site/ops/brain_release_authority.py).
//
// brain_release: the release the asset selector names (logical id plus the
// exact manifest digest) and the WikiLean commit it was built from
// (release.json authority.git_commit). Null when the
// release cannot be resolved; this endpoint never 503s on a Brain outage.

import type { Hono } from "hono";
import type { Env } from "./env.js";
import { resolveBrainRelease } from "./brain.js";

export function registerVersionRoutes(app: Hono<{ Bindings: Env }>): void {
  app.get("/api/version", async (c) => {
    const meta = c.env.CF_VERSION_METADATA;
    const release = await resolveBrainRelease(c);
    return c.json(
      {
        ok: true,
        worker: {
          version_id: meta?.id || null,
          version_tag: meta?.tag || null,
          uploaded_at: meta?.timestamp || null,
        },
        brain_release: release
          ? {
            release_id: release.releaseId,
            // release_id excludes attestations/created_at, so two manifests
            // can share it; manifest_sha256 pins the exact one served.
            manifest_sha256: release.manifestSha256,
            git_commit: release.manifest.authority.git_commit,
          }
          : null,
      },
      200,
      { "Cache-Control": "no-store" },
    );
  });
}
