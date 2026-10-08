import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { exportCompanionAnnotations, ANCHOR_PROFILE, type CompanionArticle } from "../src/companion.js";
import { MATHLIB_REPOSITORY } from "../src/native-companion.js";
import { setup, get, articleRow, blockNetwork, SLUG, PIPELINE_TOKEN } from "./helpers/harness.js";

blockNetwork();

const article: CompanionArticle = {
  slug: "Group", wikipediaTitle: "Group (mathematics)", revid: 12345, version: 7, wikidataQid: "Q83478",
  annotations: [{
    id: "aaaaaaaaaaaa", status: "formalized", kind: "definition", label: "Group",
    anchor: { section: "(lead)", snippet: "a group" },
    mathlib: { decl: "Group", module: "Mathlib.Algebra.Group.Defs", match_kind: "exact" },
  }],
};
const annotation = article.annotations[0] as Record<string, unknown>;

describe("atomic companion export", () => {
  it("maps the item's facts onto the format and pins both ends without asserting a proof", () => {
    const result = exportCompanionAnnotations(article);
    expect(result.diagnostics).toEqual([]);
    expect(result.annotations).toHaveLength(1);
    expect(result.annotations[0]).not.toHaveProperty("version");
    const source = "https://en.wikipedia.org/w/index.php?title=Group_(mathematics)&oldid=12345";
    expect(result.annotations[0]).toEqual({
      id: "aaaaaaaaaaaa", kind: "definition", label: "Group", status: "formalized",
      source: { type: "generic", uri: source, pin: {
        type: "website", title: "Group (mathematics)", owner: "Wikipedia", url: source, revision: "12345",
        identifiers: { wikidata: "Q83478" },
      } },
      location: { profile: ANCHOR_PROFILE, anchor: annotation.anchor, resolution: "not_attempted" },
      target: {
        uri: "https://leanprover-community.github.io/mathlib4_docs/Mathlib/Algebra/Group/Defs.html#Group",
        pin: { type: "repository", url: MATHLIB_REPOSITORY, module: "Mathlib.Algebra.Group.Defs", declaration: "Group" },
      },
      extensions: { "org.wikilean": { match_kind: "exact" } },
    });
    // The legacy item never says which Mathlib commit it was judged against.
    expect(result.annotations[0].target.pin).not.toHaveProperty("commit");
    const unpinned = exportCompanionAnnotations({ ...article, wikidataQid: null });
    expect(unpinned.annotations[0].source.pin).not.toHaveProperty("identifiers");
  });

  it("each source span is independent; duplicate native IDs and content are not collapsed", () => {
    const multiple = { ...annotation, anchors: [
      { type: "prose_range", from: "one", to: "two" },
      { type: "math_alttext", value: "x+y" },
    ] };
    const result = exportCompanionAnnotations({ ...article, annotations: [multiple, multiple] });
    expect(result.annotations).toHaveLength(4);
    expect(result.annotations.map((a) => a.location.anchor.type)).toEqual(["prose_range", "math_alttext", "prose_range", "math_alttext"]);
    expect(result.annotations.map((a) => a.id)).toEqual(["aaaaaaaaaaaa", "aaaaaaaaaaaa", "aaaaaaaaaaaa", "aaaaaaaaaaaa"]);
  });

  it("excludes vetoes and unlinked statuses; keeps note and provenance but never private fields", () => {
    const input = { ...article, annotations: [
      { ...annotation, author: "private-author", provenance: "human", note: "a public note", review: { token: "secret" },
        anchor: { snippet: "visible", editor_private: "secret" } },
      { ...annotation, status: "rejected", label: "secret veto" },
      { ...annotation, status: "pending", label: "secret proposal" },
      { ...annotation, status: "not_formalized" },
      { ...annotation, mathlib: { decl: "Group" } },
    ] };
    const result = exportCompanionAnnotations(input);
    expect(result.annotations).toHaveLength(1);
    expect(result.annotations[0]).toMatchObject({ note: "a public note", extensions: { "org.wikilean": { provenance: "human", match_kind: "exact" } } });
    expect(result.diagnostics).toEqual([
      { annotation: 3, code: "NO_FORMAL_TARGET" }, { annotation: 4, code: "NO_FORMAL_TARGET" },
    ]);
    expect(JSON.stringify(result)).not.toMatch(/private-author|secret|review|editor_private/);
  });

  it("keeps unresolved anchors opaque; missing or malformed anchors are explicit omissions", () => {
    const result = exportCompanionAnnotations({ ...article, annotations: [
      { ...annotation, anchor: {} },
      { ...annotation, anchor: undefined },
      { ...annotation, anchors: [null, { snippet: "\ud800" }, { snippet: "🧮" }] },
    ] });
    expect(result.annotations).toHaveLength(2);
    expect(result.annotations[0].location).toEqual({ profile: ANCHOR_PROFILE, anchor: {}, resolution: "not_attempted" });
    expect(result.annotations[1].location.anchor.snippet).toBe("🧮");
    expect(result.diagnostics.map((d) => d.code)).toEqual(["NO_ANCHOR", "INVALID_ANCHOR", "INVALID_ANCHOR"]);
    expect(result.annotations.every((a) => a.location !== undefined)).toBe(true);
  });

  it("does not guess formal targets and URL-encodes explicit declaration names", () => {
    const result = exportCompanionAnnotations({ ...article, annotations: [
      { ...annotation, mathlib: { decl: "x\"<># &😀", module: "Mathlib.Group" } },
      { ...annotation, mathlib: { decl: "x", module: "Mathlib/../../evil" } },
      { ...annotation, mathlib: { decl: "\udfff", module: "Mathlib.Group" } },
    ] });
    expect(result.annotations).toHaveLength(1);
    expect(result.annotations[0].target.uri).toBe("https://leanprover-community.github.io/mathlib4_docs/Mathlib/Group.html#x%22%3C%3E%23%20%26%F0%9F%98%80");
    expect(result.annotations[0].target.pin).toMatchObject({ module: "Mathlib.Group", declaration: "x\"<># &😀" });
    expect(result.diagnostics).toHaveLength(2);
  });

  it("supports legacy flat declaration metadata and rejects an unpinned article", () => {
    expect(exportCompanionAnnotations({ ...article, annotations: [{
      ...annotation, mathlib: undefined, decl: "Group", module: "Mathlib.Algebra.Group.Defs",
    }] }).annotations).toHaveLength(1);
    for (const revid of [null, 0, -1, 1.5, Number.MAX_SAFE_INTEGER + 1]) {
      expect(() => exportCompanionAnnotations({ ...article, revid })).toThrow("pinned");
    }
  });

  it("rejects malformed article metadata instead of emitting an invalid core record", () => {
    for (const invalid of [{ wikipediaTitle: "\ud800" }, { slug: "\udfff" }, { version: Infinity }]) {
      expect(() => exportCompanionAnnotations({ ...article, ...invalid })).toThrow("invalid article metadata");
    }
  });

  it("matches the shared fixture validated by the Python reference checker", () => {
    const fixture = JSON.parse(readFileSync(new URL("./fixtures/companion-export.json", import.meta.url), "utf8"));
    expect(exportCompanionAnnotations(fixture.article)).toEqual(fixture.result);
  });
});

describe("GET /api/article/:slug/companion.json", () => {
  it("returns a shard from one read with the pinned revid, not latest_revid; no writes or auth side effects", async () => {
    const { db, env } = setup();
    db.prepare("UPDATE articles SET latest_revid=99999 WHERE slug=?").run(SLUG);
    const before = articleRow(db);
    const response = await get(env, `/api/article/${SLUG}/companion.json`);
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    expect(response.headers.get("X-Companion-Annotations")).toBe("1");
    expect(response.headers.get("X-Companion-Omissions")).toBe("1");
    const records = await response.json() as Array<{ source: { uri: string } }>;
    expect(records).toHaveLength(1);
    expect(records[0].source.uri).toContain("oldid=12345");
    expect(records[0]).toMatchObject({ status: "formalized", kind: "definition", label: "Abelian group",
      source: { pin: { type: "website", title: "Test Article", revision: "12345" } },
      target: { pin: { type: "repository", module: "Mathlib.Algebra.Group.Defs", declaration: "AddCommGroup" } },
      extensions: { "org.wikilean": { provenance: "ai", match_kind: "exact" } } });
    expect(articleRow(db)).toEqual(before);
    const report = await get(env, `/api/article/${SLUG}/companion.json?report=1`);
    expect(await report.json()).toMatchObject({ annotations: records, diagnostics: [{ code: "NO_FORMAL_TARGET" }] });
  });

  it("never exposes tombstones even to the bot and distinguishes empty/unknown/unpinned articles", async () => {
    const { db, env } = setup();
    db.prepare("UPDATE articles SET annotations=? WHERE slug=?").run(JSON.stringify([{ ...annotation, status: "rejected" }]), SLUG);
    const response = await get(env, `/api/article/${SLUG}/companion.json?report=1`, { bearer: PIPELINE_TOKEN });
    expect(await response.json()).toEqual({ annotations: [], diagnostics: [] });
    expect((await get(env, "/api/article/unknown/companion.json")).status).toBe(404);
    db.prepare("UPDATE articles SET revid=NULL WHERE slug=?").run(SLUG);
    expect((await get(env, `/api/article/${SLUG}/companion.json`)).status).toBe(409);
    expect((await get(env, `/api/article/${SLUG}.json`)).status).toBe(200);
  });
});
