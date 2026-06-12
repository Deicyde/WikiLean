// One-shot annotation-count backfill (Wave D, contract D-C5). Computes
// n_formalized / n_partial / n_not_formalized for every article from its
// live annotations (excluding status='rejected' tombstones, matching the
// write-path computation) and emits backfill-counts.sql of plain UPDATEs.
//
// Deliberately NOT the bot POST path and deliberately NOT a versioned write:
// the counts are derived metadata (D-C5: "these never appear in cache keys"),
// so there is no version bump, no revision row, and no cache impact. Plain
// idempotent UPDATEs; re-applying is harmless, and every write path keeps the
// columns current from here on (null = not yet computed).
//
//   npm run backfill-counts
//   npx wrangler d1 execute wikilean --remote --file=backfill-counts.sql
//
// Requires migration 0005 (the columns) to be applied first. This script
// performs NO remote writes — the SELECT is read-only.

import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

// Pure per-article computation (the Worker's write paths use its own twin in
// the save handler): count by status, skipping 'rejected' human vetoes.
export function computeCounts(annotations: Array<Record<string, unknown>>): {
  n_formalized: number;
  n_partial: number;
  n_not_formalized: number;
} {
  const c = { n_formalized: 0, n_partial: 0, n_not_formalized: 0 };
  for (const a of annotations) {
    switch (a.status) {
      case "formalized":
        c.n_formalized++;
        break;
      case "partial":
        c.n_partial++;
        break;
      case "not_formalized":
        c.n_not_formalized++;
        break;
      // 'rejected' (tombstones) and anything else: excluded.
    }
  }
  return c;
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

function main(): void {
  const rows = wranglerJson("SELECT slug, annotations FROM articles") as Array<{
    slug: string;
    annotations: string;
  }>;
  rows.sort((a, b) => a.slug.localeCompare(b.slug));
  console.log(`articles read from D1 : ${rows.length}`);

  const statements: string[] = [];
  let totalAnnotations = 0;
  let totalRejected = 0;

  for (const r of rows) {
    if (r.slug.includes("/") || r.slug.includes("\\") || r.slug.includes("..")) {
      throw new Error(`refusing to process suspicious slug ${JSON.stringify(r.slug)}`);
    }
    let anns: unknown;
    try {
      anns = JSON.parse(r.annotations);
    } catch {
      throw new Error(`articles.annotations for ${r.slug} is not valid JSON`);
    }
    if (!Array.isArray(anns)) throw new Error(`articles.annotations for ${r.slug} is not an array`);
    totalAnnotations += anns.length;
    const c = computeCounts(anns as Array<Record<string, unknown>>);
    totalRejected += anns.length - c.n_formalized - c.n_partial - c.n_not_formalized;
    statements.push(
      `UPDATE articles SET n_formalized=${c.n_formalized}, n_partial=${c.n_partial}, ` +
        `n_not_formalized=${c.n_not_formalized} WHERE slug=${q(r.slug)};`,
    );
  }

  console.log(`annotations total     : ${totalAnnotations}`);
  console.log(`excluded (rejected)   : ${totalRejected}`);

  const outPath = resolve(process.cwd(), "backfill-counts.sql");
  writeFileSync(outPath, statements.join("\n") + "\n");
  console.log(`wrote ${outPath} — ${statements.length} UPDATEs (no version bump, no revision rows)`);

  console.log("");
  console.log("apply with : npx wrangler d1 execute wikilean --remote --file=backfill-counts.sql");
  console.log("           (migration 0005 must be applied first — it adds the columns)");
  console.log("verify with: npx wrangler d1 execute wikilean --remote --command \\");
  console.log('  "SELECT COUNT(*) AS missing FROM articles WHERE n_formalized IS NULL" — expect 0');
}

// Only run when invoked directly (node scripts/backfill-counts.ts), not when
// the pure computation is imported by the test suite.
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  main();
}
