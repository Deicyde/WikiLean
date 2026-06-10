import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { describe, it, expect } from "vitest";
import { buildSeedRows } from "../src/seed/build.js";

// Validates the migration SQL and seed builder locally against Node's built-in
// SQLite — no wrangler/remote needed.

const SITE = resolve(process.cwd(), "../site");
const MIGRATION = resolve(process.cwd(), "migrations/0001_init.sql");

describe("D1 schema + seed", () => {
  it("applies migration and seeds articles + one revision each", () => {
    const db = new DatabaseSync(":memory:");
    db.exec(readFileSync(MIGRATION, "utf8"));

    const rows = buildSeedRows(SITE);
    expect(rows.length).toBeGreaterThan(250);

    const insA = db.prepare(
      "INSERT INTO articles (slug, wikipedia_title, display_title, wikidata_qid, revid, annotations, version, created_at, updated_at) VALUES (?,?,?,?,?,?,1,?,?)",
    );
    const insR = db.prepare(
      "INSERT INTO revisions (slug, user_id, annotations, comment, created_at) VALUES (?,NULL,?,?,?)",
    );
    const now = Date.now();
    for (const r of rows) {
      insA.run(r.slug, r.wikipedia_title, r.display_title, r.wikidata_qid, r.revid, r.annotations, now, now);
      insR.run(r.slug, r.annotations, "seed import", now);
    }

    const nArticles = (db.prepare("SELECT COUNT(*) AS n FROM articles").get() as { n: number }).n;
    const nRevisions = (db.prepare("SELECT COUNT(*) AS n FROM revisions").get() as { n: number }).n;
    expect(nArticles).toBe(rows.length);
    expect(nRevisions).toBe(rows.length);

    // Spot-check a known article round-trips with valid annotations JSON.
    const ag = db.prepare("SELECT * FROM articles WHERE slug = ?").get("Abelian_group") as
      | Record<string, unknown>
      | undefined;
    expect(ag).toBeTruthy();
    const anns = JSON.parse(ag!.annotations as string);
    expect(Array.isArray(anns)).toBe(true);
    expect(anns.length).toBeGreaterThan(0);

    // QIDs populated for at least some articles.
    const withQid = (db.prepare("SELECT COUNT(*) AS n FROM articles WHERE wikidata_qid IS NOT NULL").get() as { n: number }).n;
    expect(withQid).toBeGreaterThan(0);

    db.close();
  });
});
