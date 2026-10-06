// Explicit local article snapshot -> atomic FCA shard. No D1 or network access.
import { readFileSync, writeFileSync } from "node:fs";
import { exportCompanionAnnotations } from "../src/companion.js";

try {
  const [input, output] = process.argv.slice(2);
  if (!input || !output || process.argv.length !== 4) throw new Error("usage: export-companion.ts INPUT OUTPUT");
  const article = JSON.parse(readFileSync(input, "utf8"));
  if (!article || !Array.isArray(article.annotations)) throw new Error("expected one native WikiLean article snapshot");
  const result = exportCompanionAnnotations({
    slug: article.slug, wikipediaTitle: article.wikipediaTitle ?? article.wikipedia_title,
    version: article.version, revid: article.revid, annotations: article.annotations,
  });
  writeFileSync(output, JSON.stringify(result.annotations, null, 2) + "\n", { flag: "wx" });
  process.stdout.write(JSON.stringify({ annotations: result.annotations.length, diagnostics: result.diagnostics }) + "\n");
} catch (error) {
  process.stderr.write(String(error) + "\n");
  process.exitCode = 2;
}
