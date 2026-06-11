// One-shot stable-annotation-id backfill (P1, C1). Assigns a 12-char
// lowercase-hex `id` to every annotation that lacks a valid one, across the
// whole corpus. D1 is canonical: rows are read live (read-only SELECT via
// wrangler) and the transform is emitted as backfill-ids.sql — the sanctioned
// in-place D1 pattern (CAS UPDATE + revisions INSERT). It can NOT go through
// the bot POST path: adding ids alters stored provenance='human' annotations,
// which the 422 human-preservation check correctly rejects.
//
// The same transformed arrays are written into the disk sidecars
// site/annotations/<slug>.json (envelope kept, indent=2 + trailing newline)
// so disk and the emitted SQL agree byte-for-byte on annotation content.
// site/annotations/.d1_pull_manifest.json provides the pull-time versions:
// any article whose live version differs gets a DRIFT warning (re-pull and
// re-run before applying).
//
//   npm run backfill-ids
//   npx wrangler d1 execute wikilean --remote --file=backfill-ids.sql
//
// This script performs NO remote writes. Idempotent: re-running after apply
// finds every annotation already carrying a valid id and emits nothing.

import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import type { AnnRecord } from "../src/validation.js";

// ID1 contract: 12 lowercase hex chars = 6 crypto-random bytes
// (Python twin: secrets.token_hex(6); Worker twin: crypto.getRandomValues).
export const ANNOTATION_ID_RE = /^[0-9a-f]{12}$/;

export function randomHexId(): string {
  return randomBytes(6).toString("hex");
}

function hasValidId(a: AnnRecord): boolean {
  return typeof a.id === "string" && ANNOTATION_ID_RE.test(a.id);
}

// The pure transform (unit-tested in test/ids.test.ts): walk the array in
// order and assign a fresh id to every annotation lacking a valid 12-hex one
// (a malformed/non-string id is replaced — the corpus has zero ids today, so
// nothing real can be renamed). Collision-checked against every id already
// present in the article. Annotations that already carry a valid id pass
// through by reference, so a second run reports changed: false.
export function assignAnnotationIds(
  annotations: AnnRecord[],
  genId: () => string = randomHexId,
): { annotations: AnnRecord[]; changed: boolean } {
  const taken = new Set<string>();
  for (const a of annotations) {
    if (hasValidId(a)) taken.add(a.id as string);
  }
  let changed = false;
  const out = annotations.map((a) => {
    if (hasValidId(a)) return a;
    let id = genId();
    while (taken.has(id)) id = genId();
    taken.add(id);
    changed = true;
    return { ...a, id };
  });
  return { annotations: out, changed };
}

// ---------------------------------------------------------------------------
// Script body (skipped when imported by tests).

function q(s: string): string {
  return "'" + s.replace(/'/g, "''") + "'";
}

function wranglerJson(sql: string): unknown[] {
  const out = execFileSync(
    "npx",
    ["wrangler", "d1", "execute", "wikilean", "--remote", "--json", "--command", sql],
    { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"], maxBuffer: 128 * 1024 * 1024 },
  );
  const parsed: unknown = JSON.parse(out);
  if (Array.isArray(parsed) && parsed[0] && typeof parsed[0] === "object" && "results" in parsed[0]) {
    return (parsed[0] as { results: unknown[] }).results;
  }
  if (parsed && typeof parsed === "object" && "results" in parsed) {
    return (parsed as { results: unknown[] }).results;
  }
  throw new Error("unexpected wrangler --json output shape");
}

// D1 caps a single SQL statement at ~100 KB; the largest article today
// projects to ~82 KB post-ids. Fail loudly rather than emit a file that
// half-applies.
const MAX_STATEMENT_BYTES = 97 * 1024;

function main(): void {
  const annotDir = resolve(process.cwd(), "..", "site", "annotations");
  const manifestPath = resolve(annotDir, ".d1_pull_manifest.json");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8")) as Record<
    string,
    { version: number; revid: number | null; pulled_at: string }
  >;

  const rows = wranglerJson("SELECT slug, annotations, version FROM articles") as Array<{
    slug: string;
    annotations: string;
    version: number;
  }>;
  rows.sort((a, b) => a.slug.localeCompare(b.slug));
  console.log(`articles read from D1 : ${rows.length}`);

  const drifted: string[] = [];
  const sidecarMissing: string[] = [];
  const sidecarStale: string[] = [];
  const statements: string[] = [];
  const now = Date.now();
  let changedArticles = 0;
  let assignedIds = 0;
  let totalAnnotations = 0;

  for (const r of rows) {
    if (r.slug.includes("/") || r.slug.includes("\\") || r.slug.includes("..")) {
      throw new Error(`refusing to process suspicious slug ${JSON.stringify(r.slug)}`);
    }
    const m = manifest[r.slug];
    if (!m || m.version !== r.version) drifted.push(`${r.slug} (manifest ${m?.version ?? "absent"} vs live ${r.version})`);

    let anns: unknown;
    try {
      anns = JSON.parse(r.annotations);
    } catch {
      throw new Error(`articles.annotations for ${r.slug} is not valid JSON`);
    }
    if (!Array.isArray(anns)) throw new Error(`articles.annotations for ${r.slug} is not an array`);
    totalAnnotations += anns.length;

    const before = (anns as AnnRecord[]).filter((a) => !hasValidId(a)).length;
    const { annotations: withIds, changed } = assignAnnotationIds(anns as AnnRecord[]);
    if (!changed) continue;
    changedArticles++;
    assignedIds += before;

    // Deterministic pass order: slug-sorted articles, in-array order within
    // each. The CAS UPDATE pins the version this script read; if the article
    // moves before apply, the UPDATE no-ops (and the guarded INSERT below
    // writes no revision row either).
    const annJson = JSON.stringify(withIds);
    const update =
      `UPDATE articles SET annotations=${q(annJson)}, version=version+1, updated_at=${now} ` +
      `WHERE slug=${q(r.slug)} AND version=${r.version};`;
    // Revision log, matching the Worker's bot-write convention (src/index.ts):
    // user_id 'pipeline', kind 'pipeline', parent_id = latest prior revision.
    // The WHERE EXISTS ties it to the CAS above — version is now r.version+1
    // only if our UPDATE actually fired.
    const insert =
      `INSERT INTO revisions (slug, user_id, annotations, comment, kind, parent_id, created_at) ` +
      `SELECT ${q(r.slug)}, 'pipeline', ${q(annJson)}, 'id backfill', 'pipeline', ` +
      `(SELECT MAX(id) FROM revisions WHERE slug=${q(r.slug)}), ${now} ` +
      `WHERE EXISTS (SELECT 1 FROM articles WHERE slug=${q(r.slug)} AND version=${r.version + 1});`;
    if (update.length > MAX_STATEMENT_BYTES || insert.length > MAX_STATEMENT_BYTES) {
      throw new Error(`statement for ${r.slug} exceeds ${MAX_STATEMENT_BYTES} bytes (D1 statement cap)`);
    }
    statements.push(update, insert);

    // Mirror the ids into the disk sidecar (D1 annotations are canonical —
    // same policy as pull-annotations.ts; the envelope is preserved).
    const sidecarPath = resolve(annotDir, `${r.slug}.json`);
    if (!existsSync(sidecarPath)) {
      sidecarMissing.push(r.slug);
      continue;
    }
    const model = JSON.parse(readFileSync(sidecarPath, "utf8")) as Record<string, unknown>;
    if (JSON.stringify(model.annotations) !== r.annotations) sidecarStale.push(r.slug);
    model.annotations = withIds;
    writeFileSync(sidecarPath, JSON.stringify(model, null, 2) + "\n");
  }

  console.log(`annotations total     : ${totalAnnotations}`);
  console.log(`ids assigned          : ${assignedIds}`);
  console.log(`articles changed      : ${changedArticles}`);
  if (sidecarStale.length) {
    console.log(`sidecars that were stale vs D1 (now refreshed to D1+ids): ${sidecarStale.join(", ")}`);
  }
  if (sidecarMissing.length) {
    console.log(`WARN: no disk sidecar for ${sidecarMissing.join(", ")} — run 'npm run pull' to materialize them`);
  }

  if (!statements.length) {
    console.log("nothing to backfill — every annotation already has a valid 12-hex id");
    return;
  }

  const outPath = resolve(process.cwd(), "backfill-ids.sql");
  writeFileSync(outPath, statements.join("\n") + "\n");
  console.log(`wrote ${outPath} — ${changedArticles} articles, ${statements.length} statements`);

  if (drifted.length) {
    console.log("");
    console.log(`DRIFT WARNING: ${drifted.length} article(s) moved since the manifest pull:`);
    for (const d of drifted) console.log(`  ${d}`);
    console.log("Their CAS UPDATEs target the version read just now (still safe), but disk");
    console.log("sidecars may not reflect what the manifest pinned. Re-run 'npm run pull'");
    console.log("then re-run 'npm run backfill-ids' before applying.");
  }

  console.log("");
  console.log("apply with : npx wrangler d1 execute wikilean --remote --file=backfill-ids.sql");
  console.log("verify with: npx wrangler d1 execute wikilean --remote --command \\");
  console.log("  \"SELECT COUNT(*) AS backfill_revs FROM revisions WHERE comment='id backfill'\"");
  console.log("then re-run 'npm run backfill-ids' — it should report 0 articles changed.");
}

// Only run when invoked directly (node scripts/backfill-ids.ts), not when the
// pure transform is imported by the test suite.
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main();
}
