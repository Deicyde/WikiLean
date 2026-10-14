// Hermetic integration boundary: exercise the actual HTTP route over the same
// D1 SQLite shim as Worker tests. This fixture never reads a live database.
import { app } from "../src/index.js";
import { setup, SLUG } from "../test/helpers/fixture.js";
import { readFileSync } from "node:fs";

const fixture = JSON.parse(readFileSync(new URL("../test/fixtures/companion-export.json", import.meta.url), "utf8"));
const { db, env } = setup();
try {
  db.prepare("UPDATE articles SET wikipedia_title=?, revid=?, version=?, annotations=? WHERE slug=?").run(
    fixture.article.wikipediaTitle, fixture.article.revid, fixture.article.version,
    JSON.stringify(fixture.article.annotations), SLUG,
  );
  globalThis.fetch = async () => { throw new Error("network is forbidden in this integration fixture"); };
  const response = await app.request(`/api/article/${SLUG}/companion.jsonl`, undefined, env);
  if (response.status !== 200) throw new Error(`companion fixture HTTP ${response.status}`);
  process.stdout.write(await response.text());
} finally {
  db.close();
}
