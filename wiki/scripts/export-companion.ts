// Explicit local article snapshot -> one JSONL annotation file. No D1 or network access.
import { readFileSync, writeFileSync } from "node:fs";
import { exportCompanionAnnotations, toJsonl } from "../src/companion.js";

try {
  const [input, output] = process.argv.slice(2);
  if (!input || !output || process.argv.length !== 4) throw new Error("usage: export-companion.ts INPUT OUTPUT.jsonl");
  if (!/\.(jsonl|ndjson)$/i.test(output)) throw new Error("OUTPUT holds one annotation per line; name it .jsonl");
  const article = JSON.parse(readFileSync(input, "utf8"));
  if (!article || !Array.isArray(article.annotations)) throw new Error("expected one native WikiLean article snapshot");
  const result = exportCompanionAnnotations({
    slug: article.slug, wikipediaTitle: article.wikipediaTitle ?? article.wikipedia_title,
    version: article.version, revid: article.revid, wikidataQid: article.wikidataQid ?? article.wikidata_qid ?? null,
    annotations: article.annotations,
  });
  writeFileSync(output, toJsonl(result.annotations), { flag: "wx" });
  process.stdout.write(JSON.stringify({ annotations: result.annotations.length, diagnostics: result.diagnostics }) + "\n");
} catch (error) {
  process.stderr.write(String(error) + "\n");
  process.exitCode = 2;
}
