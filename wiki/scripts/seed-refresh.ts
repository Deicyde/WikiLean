// Refreshes annotations on remote D1 for slugs whose local
// site/annotations/<slug>.json content differs from the live D1 row.
// Skips slugs that have ANY user-attributed revision (so wiki edits are safe).
//
//   npm run seed:refresh
//   npx wrangler d1 execute wikilean --remote --file=refresh.sql
//
// Idempotent: re-running after apply produces an empty refresh.

import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { buildSeedRows } from "../src/seed/build.ts";

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

const siteDir = resolve(process.cwd(), "..", "site");
const local = buildSeedRows(siteDir);
console.log(`local annotations    : ${local.length}`);

const userEdited = new Set<string>(
  (wranglerJson("SELECT DISTINCT slug FROM revisions WHERE user_id IS NOT NULL") as Array<{ slug: string }>).map(
    (r) => r.slug,
  ),
);
console.log(`slugs with user edits: ${userEdited.size}  (skipped if their content differs)`);

const remoteRows = wranglerJson("SELECT slug, annotations FROM articles") as Array<{
  slug: string;
  annotations: string;
}>;
const byRemote = new Map(remoteRows.map((r) => [r.slug, r.annotations]));
console.log(`remote articles      : ${byRemote.size}`);

const updates: string[] = [];
const skipped: string[] = [];
const now = Date.now();
for (const r of local) {
  const remoteAnn = byRemote.get(r.slug);
  if (remoteAnn === undefined) continue; // new ones are handled by seed:delta
  if (remoteAnn === r.annotations) continue; // already in sync
  if (userEdited.has(r.slug)) {
    skipped.push(r.slug);
    continue;
  }
  updates.push(
    `UPDATE articles SET annotations=${q(r.annotations)}, version=version+1, updated_at=${now}, ` +
      `revid=COALESCE(revid, ${r.revid ?? "NULL"}) WHERE slug=${q(r.slug)};`,
  );
  updates.push(
    `INSERT INTO revisions (slug, user_id, annotations, comment, created_at) VALUES (` +
      `${q(r.slug)}, NULL, ${q(r.annotations)}, 'pipeline refresh', ${now});`,
  );
}

console.log(`refresh updates      : ${updates.length / 2}`);
if (skipped.length) {
  console.log(`SKIPPED (have user edits, content differs): ${skipped.join(", ")}`);
}
if (!updates.length) {
  console.log("nothing to refresh — remote matches local for all non-user-edited slugs");
  process.exit(0);
}

const outPath = resolve(process.cwd(), "refresh.sql");
writeFileSync(outPath, updates.join("\n") + "\n");
console.log(`wrote ${outPath} — ${updates.length / 2} articles, ${updates.length} statements`);
console.log("apply with: npx wrangler d1 execute wikilean --remote --file=refresh.sql");
