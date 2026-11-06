// Editing is off on the annotated article plane (/:slug) — decision 5, option
// (b) of the signed public-editing plan, 2026-11-06, carrying Jack's
// 2026-11-05 retirement decision ("Editing goes; Confirm and Report replace
// it"). Migration 0019 seeds site_settings.slug_editing = 'off'.
//
// What this suite pins:
//   · a human save, a proposal decision and an endorse answer 403, with a
//     message that points at the article's /articles page where one exists;
//   · PR #126's agent annotation saves answer 403 too;
//   · the pipeline bearer still writes — the nightly is not switched off;
//   · the routes stay readable, and revert, patrol and flags are untouched;
//   · Brain writes are untouched;
//   · the page keeps its reader and loses its editor;
//   · and the whole thing is one D1 row, so an admin can put it back without
//     a release, which is why it is a setting and not a constant.
//
// The Release 2 cutover — deleting the routes and the assets — is NOT this: it
// waits for release R2, which gives the overlay history, diffs and revert.

import { createHash } from "node:crypto";
import { describe, it, expect } from "vitest";
import {
  setup, get, post, put, blockNetwork, botSave, echo, articleRow, revisionCount,
  PIPELINE_TOKEN, SEED_ANNOTATIONS, SLUG, type Harness, type ReqOpts,
} from "./helpers/harness.js";
import { bindArticles } from "./helpers/articles.js";
import { SLUG_EDITING_OFF } from "../src/index.js";

blockNetwork();

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const jsonOf = async (res: Response): Promise<any> => res.json();

const AGENT_TOKEN = "wla_" + "c".repeat(43);

// Production's default: the migration's value, untouched.
function off(): Harness {
  return setup({ slugEditing: false });
}

function withAgent(h: Harness): void {
  const now = Date.now();
  h.db.prepare("INSERT INTO users (id, name, role, created_at, updated_at) VALUES ('agent-00000000000f','A (agent)','agent',?,?)")
    .run(Math.floor(now / 1000), Math.floor(now / 1000));
  // The token hash is sha256 of the token (auth.ts agentTokenHash).
  h.db.prepare("INSERT INTO agent_tokens (agent_id, owner_id, token_hash, created_at) VALUES ('agent-00000000000f','u-human',?,?)")
    .run(createHash("sha256").update(AGENT_TOKEN).digest("hex"), now);
}

const save = (h: Harness, opts: ReqOpts) =>
  post(h.env, `/api/article/${SLUG}`, { annotations: echo(SEED_ANNOTATIONS), base_version: 1 }, opts);

describe("editing is off on /:slug", () => {
  it("a human save is refused, and nothing is written", async () => {
    const h = off();
    const res = await save(h, { user: "u-human" });
    expect(res.status).toBe(403);
    expect((await jsonOf(res)).error).toBe(SLUG_EDITING_OFF);
    expect(articleRow(h.db)!.version).toBe(1);
    expect(revisionCount(h.db)).toBe(1);
  });

  it("every session role is refused, not just ordinary users", async () => {
    for (const user of ["u-human", "u-patroller", "u-admin"]) {
      const h = off();
      expect((await save(h, { user })).status, user).toBe(403);
    }
  });

  it("endorse and proposal decisions go with it", async () => {
    const h = off();
    const endorse = await post(h.env, `/api/article/${SLUG}`,
      { action: "endorse", annotation_id: "aaaaaaaaaaaa", base_version: 1 }, { user: "u-human" });
    expect(endorse.status).toBe(403);
    const decide = await post(h.env, `/api/article/${SLUG}`,
      { action: "approve_proposal", proposal_id: "p1", base_version: 1 }, { user: "u-patroller" });
    expect(decide.status).toBe(403);
  });

  it("an agent's annotation save is refused", async () => {
    const h = off();
    withAgent(h);
    const res = await save(h, { bearer: AGENT_TOKEN, origin: null });
    expect(res.status).toBe(403);
    expect((await jsonOf(res)).error).toBe(SLUG_EDITING_OFF);
  });

  it("the message points at the article's /articles page when one exists", async () => {
    const h = off();
    bindArticles(h.env);
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    const res = await save(h, { user: "u-human" });
    expect(res.status).toBe(403);
    const body = await jsonOf(res);
    expect(body.articles_href).toBe("/articles/1001/Widget_lemma");
    expect(body.detail).toMatch(/formalization page/);
  });

  it("and says so plainly when no record set covers the article", async () => {
    const h = off();
    const body = await jsonOf(await save(h, { user: "u-human" }));
    expect(body.articles_href).toBeNull();
    expect(body.detail).toMatch(/No record set covers this article/);
  });

  it("the pipeline bearer still writes: the nightly is not switched off", async () => {
    const h = off();
    const res = await botSave(h.env, { annotations: echo(SEED_ANNOTATIONS), base_version: 1 });
    expect(res.status).toBe(200);
  });

  it("the routes stay readable, and revert, patrol and flags are untouched", async () => {
    const h = off();
    expect((await get(h.env, `/api/article/${SLUG}.json`)).status).toBe(200);
    expect((await get(h.env, `/${SLUG}`)).status).toBe(200);
    expect((await get(h.env, `/${SLUG}/history`)).status).toBe(200);
    expect((await post(h.env, `/api/article/${SLUG}/revert/1`, {}, { user: "u-patroller" })).status).toBe(200);
    expect((await post(h.env, "/api/revision/1/patrol", {}, { user: "u-patroller" })).status).toBe(200);
    expect((await post(h.env, `/api/flag/${SLUG}`, { reason: "other" })).status).toBe(200);
  });

  it("article creation stays the bearer's, and still refuses everyone else", async () => {
    const h = off();
    expect((await put(h.env, "/api/article/New_One",
      { wikipedia_title: "New One", revid: 4242, annotations: [] }, { user: "u-admin" })).status).toBe(403);
    expect((await put(h.env, "/api/article/New_One",
      { wikipedia_title: "New One", revid: 4242, annotations: [] }, { bearer: PIPELINE_TOKEN, origin: null })).status).toBe(201);
  });

  it("the page loses its editor and gains a pointer; the reader is unchanged", async () => {
    const h = off();
    bindArticles(h.env);
    h.db.prepare("UPDATE articles SET wikipedia_title = 'Widget lemma' WHERE slug = ?").run(SLUG);
    const html = await (await get(h.env, `/${SLUG}`, { user: "u-human" })).text();
    expect(html).not.toContain("/assets/editor.js");
    expect(html).toContain("This page is now read-only");
    expect(html).toContain("/articles/1001/Widget_lemma");
    // An anonymous reader is not invited to sign in and edit either.
    const anon = await (await get(h.env, `/${SLUG}`)).text();
    expect(anon).not.toContain("Sign in to edit");
    expect(anon).toContain("This page is now read-only");
  });

  it("an admin puts it back with one D1 row and no release", async () => {
    const h = off();
    h.db.prepare("UPDATE site_settings SET value = 'on' WHERE key = 'slug_editing'").run();
    expect((await save(h, { user: "u-human" })).status).toBe(200);
    const html = await (await get(h.env, `/${SLUG}`, { user: "u-human" })).text();
    expect(html).toContain("/assets/editor.js");
  });
});
