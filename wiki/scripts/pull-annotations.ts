// One-time rescue pull: materialize live D1 article state back to disk.
// D1 is canonical — every articles row becomes site/annotations/<slug>.json,
// keeping the existing disk envelope (schema_version, annotation_style, ...)
// when present and replacing the annotations array with the D1 value verbatim.
// Also writes site/annotations/.d1_pull_manifest.json (slug -> {version, revid, pulled_at}).
//
//   npm run pull
//
// Read-only against remote D1; idempotent (re-running reports everything unchanged).

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";

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

interface ArticleRow {
  slug: string;
  wikipedia_title: string;
  display_title: string;
  wikidata_qid: string | null;
  revid: number | null;
  annotations: string;
  version: number;
}

const annotDir = resolve(process.cwd(), "..", "site", "annotations");
const pulledAt = new Date().toISOString();

const userEdited = (
  wranglerJson("SELECT DISTINCT slug FROM revisions WHERE user_id IS NOT NULL") as Array<{ slug: string }>
)
  .map((r) => r.slug)
  .sort();
console.log(`slugs with user edits: ${userEdited.length}  (${userEdited.join(", ")})`);

const rows = wranglerJson(
  "SELECT slug, wikipedia_title, display_title, wikidata_qid, revid, annotations, version FROM articles",
) as ArticleRow[];
rows.sort((a, b) => a.slug.localeCompare(b.slug));
console.log(`rows pulled          : ${rows.length}`);

const d1Slugs = new Set(rows.map((r) => r.slug));
let created = 0;
let updated = 0;
let newlineOnly = 0;
let unchanged = 0;
const manifest: Record<string, { version: number; revid: number | null; pulled_at: string }> = {};
const humanReport: string[] = [];

for (const r of rows) {
  if (r.slug.includes("/") || r.slug.includes("\\") || r.slug.includes("..")) {
    throw new Error(`refusing to write suspicious slug ${JSON.stringify(r.slug)}`);
  }
  let anns: unknown;
  try {
    anns = JSON.parse(r.annotations);
  } catch {
    throw new Error(`articles.annotations for ${r.slug} is not valid JSON`);
  }
  if (!Array.isArray(anns)) throw new Error(`articles.annotations for ${r.slug} is not an array`);

  const path = resolve(annotDir, `${r.slug}.json`);
  let prior: string | null = null;
  let model: Record<string, unknown> | null = null;
  if (existsSync(path)) {
    prior = readFileSync(path, "utf8");
    try {
      const parsed: unknown = JSON.parse(prior);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        model = parsed as Record<string, unknown>;
      }
    } catch {
      console.warn(`WARN ${r.slug}: existing sidecar is not valid JSON — rebuilding a fresh envelope`);
    }
  }
  const priorAnnJson = model && Array.isArray(model.annotations) ? JSON.stringify(model.annotations) : null;
  if (!model) {
    model = {
      slug: r.slug,
      wikipedia_title: r.wikipedia_title,
      display_title: r.display_title,
      schema_version: 3,
    };
  }
  model.slug = r.slug;
  model.annotations = anns; // D1 is canonical

  const next = JSON.stringify(model, null, 2) + "\n";
  if (prior === null) {
    created++;
    writeFileSync(path, next);
  } else if (prior === next) {
    unchanged++;
  } else if (prior + "\n" === next) {
    newlineOnly++;
    writeFileSync(path, next);
  } else {
    updated++;
    writeFileSync(path, next);
  }

  manifest[r.slug] = { version: r.version, revid: r.revid, pulled_at: pulledAt };

  if (userEdited.includes(r.slug)) {
    const d1AnnJson = JSON.stringify(anns);
    const human = anns.filter(
      (a) => a && typeof a === "object" && (a as Record<string, unknown>).provenance === "human",
    ).length;
    const diskCount = priorAnnJson === null ? "none" : String(JSON.parse(priorAnnJson).length);
    const verdict = priorAnnJson === d1AnnJson ? "matches disk" : "DIFFERS from disk (human edits rescued)";
    humanReport.push(
      `  ${r.slug}: ${verdict} — disk ${diskCount} -> d1 ${anns.length} annotations, ${human} provenance:"human"`,
    );
  }
}

const manifestPath = resolve(annotDir, ".d1_pull_manifest.json");
writeFileSync(manifestPath, JSON.stringify(manifest, null, 2) + "\n");

const diskOnly = readdirSync(annotDir)
  .filter((f) => f.endsWith(".json") && !f.startsWith(".") && !f.endsWith(".agent1.json"))
  .map((f) => f.slice(0, -5))
  .filter((slug) => !d1Slugs.has(slug));

console.log(`files created        : ${created}  (no prior sidecar)`);
console.log(`files updated        : ${updated}  (content changed)`);
console.log(`newline-only         : ${newlineOnly}  (trailing newline normalized)`);
console.log(`files unchanged      : ${unchanged}`);
console.log(`manifest             : ${manifestPath}`);
console.log(`user-edited slugs (D1 vs what disk had):`);
for (const line of humanReport) console.log(line);
if (diskOnly.length) {
  console.log(`disk-only sidecars (no D1 row, left untouched): ${diskOnly.join(", ")}`);
}
