// Emits delta.sql — INSERT statements for slugs in site/annotations/ that are
// NOT yet in the remote D1. Never clobbers existing rows, so any wiki edits
// (revisions made through the live editor) are safe.
//
// After the v3-refresh pipeline finishes locally, run:
//   npm run seed:delta
//   npx wrangler d1 execute wikilean --remote --file=delta.sql
//
// Idempotent: re-running just produces an empty delta once everything is in sync.

import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { buildSeedRows } from "../src/seed/build.ts";

function q(s: string | null): string {
  return s === null ? "NULL" : "'" + s.replace(/'/g, "''") + "'";
}

function existingRemoteSlugs(): Set<string> {
  const stdout = execFileSync(
    "npx",
    [
      "wrangler",
      "d1",
      "execute",
      "wikilean",
      "--remote",
      "--json",
      "--command",
      "SELECT slug FROM articles",
    ],
    { encoding: "utf8", stdio: ["ignore", "pipe", "inherit"] },
  );

  // wrangler emits an array of result blocks; tolerate a couple of shapes.
  const parsed: unknown = JSON.parse(stdout);
  let rows: Array<{ slug: string }> = [];
  if (Array.isArray(parsed) && parsed[0] && typeof parsed[0] === "object" && "results" in parsed[0]) {
    rows = (parsed[0] as { results: Array<{ slug: string }> }).results;
  } else if (parsed && typeof parsed === "object" && "results" in parsed) {
    rows = (parsed as { results: Array<{ slug: string }> }).results;
  } else {
    throw new Error("unexpected wrangler --json output shape");
  }
  return new Set(rows.map((r) => r.slug));
}

const siteDir = resolve(process.cwd(), "..", "site");
const rows = buildSeedRows(siteDir);
const existing = existingRemoteSlugs();

const newRows = rows.filter((r) => !existing.has(r.slug));
console.log(`local annotations : ${rows.length}`);
console.log(`already in remote : ${existing.size}`);
console.log(`new for delta     : ${newRows.length}`);

if (newRows.length === 0) {
  console.log("nothing to do — remote D1 is in sync with site/annotations/");
  process.exit(0);
}

const now = Date.now();
const lines: string[] = [];
for (const r of newRows) {
  lines.push(
    "INSERT INTO articles (slug, wikipedia_title, display_title, wikidata_qid, revid, annotations, version, created_at, updated_at) VALUES (" +
      `${q(r.slug)}, ${q(r.wikipedia_title)}, ${q(r.display_title)}, ${q(r.wikidata_qid)}, ${r.revid ?? "NULL"}, ${q(r.annotations)}, 1, ${now}, ${now});`,
  );
  lines.push(
    "INSERT INTO revisions (slug, user_id, annotations, comment, created_at) VALUES (" +
      `${q(r.slug)}, NULL, ${q(r.annotations)}, 'orphan refresh import', ${now});`,
  );
}

const outPath = resolve(process.cwd(), "delta.sql");
writeFileSync(outPath, lines.join("\n") + "\n");
console.log(`wrote ${outPath} — ${newRows.length} articles, ${lines.length} statements`);
console.log("apply with: npx wrangler d1 execute wikilean --remote --file=delta.sql");
