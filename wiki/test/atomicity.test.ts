import { describe, expect, it } from "vitest";
import {
  NEW_REVID,
  PIPELINE_TOKEN,
  REVID,
  SEED_ANNOTATIONS,
  SLUG,
  articleRow,
  blockNetwork,
  echo,
  eventRows,
  post,
  revisionCount,
  save,
  setup,
} from "./helpers/harness.js";

blockNetwork();

function bumpBeforeBatch(env: unknown, bump: () => void): void {
  const d1 = env as { batch: (statements: unknown[]) => Promise<unknown> };
  const originalBatch = d1.batch.bind(d1);
  let bumped = false;
  d1.batch = async (statements) => {
    if (!bumped) {
      bumped = true;
      bump();
    }
    return originalBatch(statements);
  };
}

describe("article mutation atomicity", () => {
  it("a stale guarded save appends no revision or annotation event", async () => {
    const { db, env } = setup();
    const annotations = echo(SEED_ANNOTATIONS) as Array<Record<string, unknown>>;
    annotations[0].note = "racing edit";
    bumpBeforeBatch(env.DB, () => {
      db.prepare("UPDATE articles SET version = version + 1 WHERE slug = ?").run(SLUG);
    });

    const response = await save(env, { annotations, base_version: 1 }, { user: "u-human" });

    expect(response.status).toBe(409);
    expect(await response.json()).toMatchObject({ error: "stale", version: 2 });
    expect(revisionCount(db)).toBe(1);
    expect(eventRows(db)).toEqual([]);
    expect(JSON.parse(articleRow(db)!.annotations)).toEqual(SEED_ANNOTATIONS);
  });

  it("a later batch failure rolls the guarded article write back", async () => {
    const { db, env } = setup();
    db.exec("CREATE TRIGGER fail_revision BEFORE INSERT ON revisions BEGIN SELECT RAISE(ABORT, 'forced revision failure'); END");
    const before = articleRow(db)!;
    const annotations = echo(SEED_ANNOTATIONS) as Array<Record<string, unknown>>;
    annotations[0].note = "must roll back";

    const response = await save(env, { annotations, base_version: 1 }, { user: "u-human" });

    expect(response.status).toBe(500);
    expect(articleRow(db)).toEqual(before);
    expect(revisionCount(db)).toBe(1);
    expect(eventRows(db)).toEqual([]);
  });

  it("a failed bot re-pin keeps the old Wikipedia HTML cache entry", async () => {
    const { db, env, wpHtml } = setup();
    db.exec("CREATE TRIGGER fail_revision BEFORE INSERT ON revisions BEGIN SELECT RAISE(ABORT, 'forced revision failure'); END");
    const annotations = [...echo(SEED_ANNOTATIONS), {
      id: "cccccccccccc",
      status: "formalized",
      kind: "theorem",
      label: "Subgroups of abelian groups are normal",
      provenance: "ai",
      anchor: { section: "Properties", snippet: "Every subgroup of an abelian group is normal" },
      mathlib: { decl: "Subgroup.Normal", module: "Mathlib.GroupTheory.Subgroup.Basic", match_kind: "exact" },
    }];

    const response = await post(
      env,
      `/api/article/${SLUG}`,
      { annotations, base_version: 1, revid: NEW_REVID },
      { bearer: PIPELINE_TOKEN, origin: null },
    );

    expect(response.status).toBe(500);
    expect(articleRow(db)!.revid).toBe(REVID);
    expect(wpHtml.store.has(`wp:${SLUG}:${REVID}`)).toBe(true);
  });
});
