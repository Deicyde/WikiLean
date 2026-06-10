// Generates seed.sql from the static-site artifacts. Apply to D1 with:
//   wrangler d1 execute wikilean --file=seed.sql            (local)
//   wrangler d1 execute wikilean --file=seed.sql --remote   (production)
import { writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { buildSeedRows } from "../src/seed/build.ts";

function q(s: string | null): string {
  return s === null ? "NULL" : "'" + s.replace(/'/g, "''") + "'";
}

const siteDir = resolve(process.cwd(), "..", "site");
const rows = buildSeedRows(siteDir);
const now = Date.now();

const lines: string[] = [];
for (const r of rows) {
  lines.push(
    "INSERT INTO articles (slug, wikipedia_title, display_title, wikidata_qid, revid, annotations, version, created_at, updated_at) VALUES (" +
      `${q(r.slug)}, ${q(r.wikipedia_title)}, ${q(r.display_title)}, ${q(r.wikidata_qid)}, ${r.revid ?? "NULL"}, ${q(r.annotations)}, 1, ${now}, ${now});`,
  );
  lines.push(
    "INSERT INTO revisions (slug, user_id, annotations, comment, created_at) VALUES (" +
      `${q(r.slug)}, NULL, ${q(r.annotations)}, 'seed import', ${now});`,
  );
}

const out = resolve(process.cwd(), "seed.sql");
writeFileSync(out, lines.join("\n") + "\n");
console.log(`wrote ${out} — ${rows.length} articles`);
