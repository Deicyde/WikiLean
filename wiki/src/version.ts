// GET /api/version — which code and which Brain release are live. Public,
// uncached, no D1. (Under /api, so the RESERVED set already covers it.)
//
// worker: the Cloudflare Worker Version from the version_metadata binding
// (id, tag, upload timestamp), plus git_sha when the deploy was stamped by
// `npm run deploy` (scripts/deploy.ts). The Brain promoter
// (site/ops/brain-promote-release.sh) runs wrangler directly, so its versions
// carry git_sha null and a "brain-<release>-<attempt>" version_tag (that tag
// is how to tell a promoter deploy apart; a bare `npx wrangler deploy` also
// leaves git_sha null, with no tag). Their Worker
// source is brain_release.git_commit's: the promoter only deploys a checkout
// whose release-affecting paths (wiki/src included) match the release's
// authority commit (site/ops/brain_release_authority.py).
//
// brain_release: the release the asset selector names (logical id plus the
// exact manifest digest) and the WikiLean commit it was built from
// (release.json authority.git_commit). Null when the
// release cannot be resolved; this endpoint never 503s on a Brain outage.

import type { Hono } from "hono";
import type { Env } from "./env.js";
import { resolveBrainRelease } from "./brain.js";

const GIT_STAMP_RE = /^([0-9a-f]{40})(-dirty)?$/;

export function parseGitStamp(raw: string | undefined): { git_sha: string | null; git_dirty: boolean | null } {
  const m = raw ? GIT_STAMP_RE.exec(raw) : null;
  return m ? { git_sha: m[1], git_dirty: m[2] !== undefined } : { git_sha: null, git_dirty: null };
}

export function registerVersionRoutes(app: Hono<{ Bindings: Env }>): void {
  app.get("/api/version", async (c) => {
    const meta = c.env.CF_VERSION_METADATA;
    const release = await resolveBrainRelease(c);
    return c.json(
      {
        ok: true,
        worker: {
          ...parseGitStamp(c.env.WIKILEAN_GIT_SHA),
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
