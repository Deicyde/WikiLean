// PUT /api/article/:slug — bot-only article creation (Wave D, contract D-C1).
// The moderate.py `new` mode's write path: discovery finds an unknown slug,
// the runner annotates it, and the create endpoint materializes the article
// (articles row + 'pipeline' revision + moderation_state + 'add' events +
// per-status counts) in one call.

import { describe, it, expect } from "vitest";
import {
  setup,
  put,
  botCreate,
  articleRow,
  storedAnnotations,
  latestRevision,
  moderationRow,
  eventRows,
  blockNetwork,
  echo,
  SLUG,
  ID_RE,
  PIPELINE_TOKEN,
  SEED_ANNOTATIONS,
} from "./helpers/harness.js";

blockNetwork();

const NEW_SLUG = "Quotient_group";

const CREATE_BODY = {
  wikipedia_title: "Quotient group",
  display_title: "Quotient group",
  wikidata_qid: "Q1138961",
  revid: 555111,
  annotations: [
    {
      status: "formalized",
      kind: "definition",
      label: "Quotient group",
      provenance: "ai",
      anchor: { section: "(lead)", snippet: "quotient group" },
      mathlib: { decl: "QuotientGroup", module: "Mathlib.GroupTheory.QuotientGroup", match_kind: "exact" },
    },
    {
      status: "partial",
      kind: "theorem",
      label: "First isomorphism theorem",
      provenance: "ai",
      anchor: { section: "Isomorphism theorems", snippet: "first isomorphism theorem" },
      mathlib: { decl: null, module: null, match_kind: null },
    },
    {
      status: "not_formalized",
      kind: "example",
      label: "Z/nZ",
      provenance: "ai",
      anchor: { section: "Examples", snippet: "integers modulo n" },
      mathlib: { decl: null, module: null, match_kind: null },
    },
  ],
  comment: "discovered via WikiProject Math diff",
  meta: { run_id: "create-run-1", model: "test-model" },
};

describe("PUT /api/article/:slug (create)", () => {
  it("bot create → 201; article row, pipeline revision, moderation_state, counts, healed ids", async () => {
    const { db, env } = setup();
    const res = await botCreate(env, NEW_SLUG, echo(CREATE_BODY));
    expect(res.status).toBe(201);
    expect(await res.json()).toEqual({ ok: true, slug: NEW_SLUG, version: 1 });

    const row = articleRow(db, NEW_SLUG)!;
    expect(row.version).toBe(1);
    expect(row.schema_version).toBe(3);
    expect(row.wikipedia_title).toBe("Quotient group");
    expect(row.display_title).toBe("Quotient group");
    expect(row.wikidata_qid).toBe("Q1138961");
    expect(row.revid).toBe(555111);
    // D-C5: per-status counts computed from the persisted annotations.
    expect(row.n_formalized).toBe(1);
    expect(row.n_partial).toBe(1);
    expect(row.n_not_formalized).toBe(1);

    // Ids healed on every annotation; content otherwise verbatim.
    const stored = storedAnnotations(db, NEW_SLUG);
    expect(stored.length).toBe(3);
    for (const a of stored) expect(a.id).toMatch(ID_RE);
    expect(stored.map(({ id: _id, ...rest }) => rest)).toEqual(CREATE_BODY.annotations);

    const rev = latestRevision(db, NEW_SLUG);
    expect(rev.user_id).toBe("pipeline");
    expect(rev.kind).toBe("pipeline");
    expect(rev.comment).toBe("discovered via WikiProject Math diff");
    expect(JSON.parse(rev.meta ?? "null")).toEqual(CREATE_BODY.meta);
    expect(rev.parent_id).toBeNull();
    expect(rev.annotations).toBe(row.annotations);

    const mod = moderationRow(db, NEW_SLUG)!;
    expect(mod.last_reviewed_version).toBe(1);
    expect(mod.last_reviewed_at).toBeGreaterThan(0);

    // D-C3: one 'add' event per annotation, actor pipeline, linked to the revision.
    const events = eventRows(db, NEW_SLUG);
    expect(events.length).toBe(3);
    expect(new Set(events.map((e) => e.annotation_id))).toEqual(new Set(stored.map((a) => a.id)));
    for (const e of events) {
      expect(e.event_type).toBe("add");
      expect(e.actor_type).toBe("pipeline");
      expect(e.user_id).toBe("pipeline");
      expect(e.revision_id).toBe(rev.id);
      expect(e.field_changes).toBeNull();
    }
  });

  it("defaults: no comment → 'create'; no display_title → wikipedia_title; empty annotations ok", async () => {
    const { db, env } = setup();
    const res = await botCreate(env, NEW_SLUG, { wikipedia_title: "Quotient group", annotations: [] });
    expect(res.status).toBe(201);
    const row = articleRow(db, NEW_SLUG)!;
    expect(row.display_title).toBe("Quotient group");
    expect(row.revid).toBeNull();
    expect(row.n_formalized).toBe(0);
    expect(row.n_partial).toBe(0);
    expect(row.n_not_formalized).toBe(0);
    const rev = latestRevision(db, NEW_SLUG);
    expect(rev.comment).toBe("create");
    expect(rev.meta).toBeNull();
    expect(eventRows(db, NEW_SLUG).length).toBe(0);
  });

  it("existing slug → 409 {error:'exists'}, nothing written", async () => {
    const { db, env } = setup();
    const res = await botCreate(env, SLUG, { wikipedia_title: "Test Article", annotations: [] });
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ error: "exists" });
    expect(articleRow(db)!.version).toBe(1);
    expect(JSON.parse(articleRow(db)!.annotations)).toEqual(SEED_ANNOTATIONS);
  });

  it("session users and anonymous → 403 (bot-only), nothing created", async () => {
    const { db, env } = setup();
    for (const user of ["u-human", "u-patroller", "u-admin", undefined]) {
      const res = await put(env, `/api/article/${NEW_SLUG}`, echo(CREATE_BODY), user ? { user } : {});
      expect(res.status).toBe(403);
    }
    expect(articleRow(db, NEW_SLUG)).toBeUndefined();
  });

  it("RESERVED slug → 400", async () => {
    const { db, env } = setup();
    for (const reserved of ["flags", "sitemap.xml", "recent-changes", "api"]) {
      const res = await botCreate(env, reserved, { wikipedia_title: "X", annotations: [] });
      expect(res.status).toBe(400);
      expect(((await res.json()) as { error: string }).error).toBe("reserved slug");
      expect(articleRow(db, reserved)).toBeUndefined();
    }
  });

  it("cross-origin → 403; rate-limited → 429", async () => {
    const { env } = setup();
    const evil = await put(env, `/api/article/${NEW_SLUG}`, echo(CREATE_BODY), {
      bearer: PIPELINE_TOKEN,
      origin: "https://evil.example",
    });
    expect(evil.status).toBe(403);

    const limited = setup({ limiterAllows: false });
    const res = await botCreate(limited.env, NEW_SLUG, echo(CREATE_BODY));
    expect(res.status).toBe(429);
    expect(articleRow(limited.db, NEW_SLUG)).toBeUndefined();
  });

  it("validation: missing/bad wikipedia_title, bad revid, missing annotations, bad status → 400", async () => {
    const { db, env } = setup();
    const cases: Array<Record<string, unknown>> = [
      { annotations: [] }, // no wikipedia_title
      { wikipedia_title: "", annotations: [] },
      { wikipedia_title: 42, annotations: [] },
      { wikipedia_title: "X", revid: -5, annotations: [] },
      { wikipedia_title: "X", revid: 1.5, annotations: [] },
      { wikipedia_title: "X" }, // no annotations array
      { wikipedia_title: "X", annotations: [{ status: "bogus" }] }, // same validator as saves
      { wikipedia_title: "X", display_title: "", annotations: [] },
      { wikipedia_title: "X", meta: "not-an-object", annotations: [] },
    ];
    for (const body of cases) {
      const res = await botCreate(env, NEW_SLUG, body);
      expect(res.status).toBe(400);
    }
    expect(articleRow(db, NEW_SLUG)).toBeUndefined();
  });

  it("counts exclude tombstones posted at create time", async () => {
    const { db, env } = setup();
    const res = await botCreate(env, NEW_SLUG, {
      wikipedia_title: "Quotient group",
      annotations: [
        { status: "formalized", label: "kept", provenance: "ai" },
        { status: "rejected", label: "vetoed elsewhere", provenance: "human" },
      ],
    });
    expect(res.status).toBe(201);
    const row = articleRow(db, NEW_SLUG)!;
    expect(row.n_formalized).toBe(1);
    expect(row.n_partial).toBe(0);
    expect(row.n_not_formalized).toBe(0);
    // The tombstone still exists (and gets an 'add' event) — it's just not counted.
    expect(storedAnnotations(db, NEW_SLUG).length).toBe(2);
    expect(eventRows(db, NEW_SLUG).length).toBe(2);
  });
});
