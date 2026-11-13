// The claims API (src/claims.ts, migration 0023): publishing work packages, claiming one,
// the lease, the package download, linking intake receipts, release, and the coordinator's
// view and switches.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, it, expect } from "vitest";
import { app } from "../src/index.js";
import { CC0_AGREEMENT_VERSION } from "../src/articles-overlay.js";
import { CLAIM_LEASE_MS, CLAIM_LINKS_MAX, CLAIM_MAX_LIFE_MS, CLAIMS_DAILY_PER_OWNER } from "../src/claims.js";
import { setup, post, get, blockNetwork, PIPELINE_TOKEN, type Harness } from "./helpers/harness.js";

blockNetwork();
const sha256 = (bytes: string | Uint8Array) => createHash("sha256").update(bytes).digest("hex");
const encoder = new TextEncoder();
const BUNDLE = "b".repeat(64), UNIVERSE = "a".repeat(64), INDEX = "c".repeat(64);

interface Bucket {
  objects: Map<string, Uint8Array>;
  gets: string[];
  get(key: string): Promise<unknown>;
  head(key: string): Promise<unknown>;
}
function fakeContrib(): Bucket {
  const objects = new Map<string, Uint8Array>();
  const gets: string[] = [];
  return {
    objects, gets,
    async get(key) {
      gets.push(key);
      const bytes = objects.get(key);
      return bytes ? { key, size: bytes.length, body: new Blob([bytes]).stream() } : null;
    },
    async head(key) {
      const bytes = objects.get(key);
      return bytes ? { key, size: bytes.length } : null;
    },
  };
}

function lever(h: Harness, value: string) {
  h.db.prepare("UPDATE site_settings SET value=? WHERE key='claims'").run(value);
}
// A deployment with the bucket bound, one live index bundle and the claims lever open.
function world(opts: { siteWriteMode?: string } = {}) {
  const h = setup(opts);
  const bucket = fakeContrib();
  (h.env as unknown as { CONTRIB: Bucket }).CONTRIB = bucket;
  h.db.prepare(
    "INSERT INTO index_bundles (manifest_sha256,bundle,universe_sha256,index_path,index_sha256,manifest_bytes,bytes," +
    "files_json,restricted_json,state,registered_at) VALUES (?,?,?,?,?,?,?,?,?,'live',?)",
  ).run(BUNDLE, "u-test/v1", UNIVERSE, "index/decl-index.jsonl", INDEX, 100, 1000, "{}", "[]", Date.now());
  lever(h, "open");
  return { h, bucket };
}
function envelope(run: string) {
  return encoder.encode(JSON.stringify({ schema: "wikilean.contribute.package-transport/1", run_id: run,
    files: { "package.json": "e30=" } }));
}
function plan(run: string, bytes: Uint8Array, overrides: Record<string, unknown> = {}) {
  return { run_id: run, envelope_sha256: sha256(bytes), bytes: bytes.length, bundle_sha256: BUNDLE,
    universe_sha256: UNIVERSE, index_sha256: INDEX, campaign: `run:${run}+${run}-chl`, mode: "locate",
    tasks: 3, sources: 1, max_cost: 20, ...overrides };
}
function send(h: Harness, body: unknown, bearer = PIPELINE_TOKEN) {
  return post(h.env, "/api/packages", body, { bearer, origin: null });
}
// Upload a package's bytes, then publish it.
function publish(h: Harness, bucket: Bucket, run: string, overrides: Record<string, unknown> = {}, bearer = PIPELINE_TOKEN) {
  const bytes = envelope(run);
  bucket.objects.set(`packages/v1/${sha256(bytes)}.json`, bytes);
  return send(h, plan(run, bytes, overrides), bearer);
}
async function agent(h: Harness, owner = "u-human", agreed = true) {
  if (agreed) h.db.prepare("INSERT OR IGNORE INTO user_agreements (user_id,cc0_version,accepted_at) VALUES (?,?,?)")
    .run(owner, CC0_AGREEMENT_VERSION, Date.now());
  const res = await post(h.env, "/api/agents", { name: "Laptop" }, { user: owner });
  expect(res.status).toBe(201);
  return await res.json() as { token: string; agent: { id: string } };
}
let counter = 0;
const requestKey = () => (counter += 1).toString(16).padStart(32, "0");
interface Claim {
  id: string; run_id: string; state: string; claimed_at: number; lease_until: number; expires_at: number;
  end_reason: string | null; package_url: string; bundle_url: string; package: Record<string, unknown>;
}
async function claim(h: Harness, token: string, body: Record<string, unknown> = {}) {
  const res = await post(h.env, "/api/claims", { request_key: requestKey(), ...body }, { bearer: token });
  return { res, body: await res.json() as { code?: string; claim?: Claim | null } };
}
function act(h: Harness, token: string, id: string, action: string, body: unknown = {}) {
  return post(h.env, `/api/claims/${id}/${action}`, body, { bearer: token });
}
function row(h: Harness, id: string) {
  return h.db.prepare("SELECT state, end_reason, note, claimed_at, lease_until FROM contrib_claims WHERE id=?").get(id) as
    { state: string; end_reason: string | null; note: string | null; claimed_at: number; lease_until: number };
}
// An intake receipt of this agent's, as intake stores one.
function submission(h: Harness, agentId: string, owner = "u-human") {
  const id = "sub-" + sha256(`${agentId}/${(counter += 1)}`);
  const now = Date.now();
  h.db.prepare(
    "INSERT INTO pipeline_submissions (id,agent_id,owner_id,seal_sha256,body_sha256,body_bytes,received_at,updated_at,state,verdict) " +
    "VALUES (?,?,?,?,?,?,?,?,'accepted',?)",
  ).run(id, agentId, owner, sha256(`${id}/seal`), sha256(`${id}/body`), 10, now, now, JSON.stringify({ verifier: "test", code: "ok" }));
  return id;
}
async function code(res: Response) {
  return (await res.json() as { code?: string }).code;
}
async function listed(h: Harness, token: string) {
  return await (await get(h.env, "/api/packages", { bearer: token })).json() as
    { claims_open: boolean; packages: Array<{ run_id: string; available: boolean }> };
}

describe("migration 0023", () => {
  it("seeds the lever shut, and re-applying it changes nothing", () => {
    const h = setup();
    expect(h.db.prepare("SELECT value FROM site_settings WHERE key='claims'").get()).toEqual({ value: "closed" });
    lever(h, "open");
    h.db.exec(readFileSync(new URL("../migrations/0023_contrib_claims.sql", import.meta.url), "utf8"));
    expect(h.db.prepare("SELECT value FROM site_settings WHERE key='claims'").get()).toEqual({ value: "open" });
  });

  it("refuses a second live claim on one package, whatever writes it", async () => {
    const { h, bucket } = world();
    expect((await publish(h, bucket, "loc-1")).status).toBe(201);
    const a = await agent(h), b = await agent(h, "u-admin");
    const insert = h.db.prepare("INSERT INTO contrib_claims (id,run_id,agent_id,owner_id,request_key,state,claimed_at," +
      "renewed_at,lease_until) VALUES (?,'loc-1',?,?,?,?,1,1,2)");
    insert.run("clm-" + "1".repeat(32), a.agent.id, "u-human", requestKey(), "live");
    expect(() => insert.run("clm-" + "2".repeat(32), b.agent.id, "u-admin", requestKey(), "live")).toThrow();
    insert.run("clm-" + "3".repeat(32), b.agent.id, "u-admin", requestKey(), "expired");
  });
});

describe("publishing a package", () => {
  it("takes the pipeline bearer only, and publishes a run once", async () => {
    const { h, bucket } = world();
    const a = await agent(h);
    expect((await publish(h, bucket, "loc-1", {}, a.token)).status).toBe(403);
    const res = await publish(h, bucket, "loc-1");
    expect(res.status).toBe(201);
    const bytes = envelope("loc-1");
    expect((await res.json() as { package: unknown }).package).toMatchObject({ run_id: "loc-1",
      envelope_sha256: sha256(bytes), bytes: bytes.length, bundle_sha256: BUNDLE, campaign: "run:loc-1+loc-1-chl",
      tasks: 3, max_cost: 20, state: "open" });
    // The same publication again is the same row; other values are refused, never replaced.
    expect((await publish(h, bucket, "loc-1")).status).toBe(200);
    const changed = await publish(h, bucket, "loc-1", { tasks: 4 });
    expect(changed.status).toBe(409);
    expect(await code(changed)).toBe("exists");
    expect(await code(await send(h, plan("loc-2", bytes, { campaign: "run:loc-2" })))).toBe("exists");
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM contrib_packages").get()).toEqual({ n: 1 });
  });

  it("refuses a package the bucket does not hold, or one no live bundle matches", async () => {
    const { h, bucket } = world();
    const bytes = envelope("loc-1");
    const key = `packages/v1/${sha256(bytes)}.json`;
    expect(await code(await send(h, plan("loc-1", bytes)))).toBe("package_missing");
    bucket.objects.set(key, bytes.slice(1));
    expect(await code(await send(h, plan("loc-1", bytes)))).toBe("package_missing");
    bucket.objects.set(key, bytes);
    for (const overrides of [{ universe_sha256: "d".repeat(64) }, { index_sha256: "d".repeat(64) }, { bundle_sha256: "d".repeat(64) }]) {
      expect(await code(await send(h, plan("loc-1", bytes, overrides))), JSON.stringify(overrides)).toBe("bundle_mismatch");
    }
    h.db.prepare("UPDATE index_bundles SET state='retired'").run();
    expect(await code(await send(h, plan("loc-1", bytes)))).toBe("bundle_mismatch");
    h.db.prepare("UPDATE index_bundles SET state='live'").run();
    for (const overrides of [{ campaign: "run:loc-9+loc-1-chl" }, { campaign: "run:loc-10" }, { campaign: "loc-1" },
      { run_id: "-x" }, { tasks: 0 }, { max_cost: -1 }, { mode: "Locate" }, { bytes: 0 }, { extra: 1 }]) {
      const res = await send(h, plan("loc-1", bytes, overrides));
      expect(res.status, JSON.stringify(overrides)).toBe(422);
      expect(await code(res)).toBe("invalid");
    }
    // A package may pin no index and no cost, and name no challenge run.
    expect((await send(h, plan("loc-1", bytes, { index_sha256: null, max_cost: null, campaign: "run:loc-1" }))).status).toBe(201);
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM contrib_packages").get()).toEqual({ n: 1 });
  });

  it("stops under the brake, and without the bucket", async () => {
    const locked = world({ siteWriteMode: "locked" });
    expect((await publish(locked.h, locked.bucket, "loc-1")).status).toBe(503);
    const unbound = world();
    (unbound.h.env as unknown as { CONTRIB?: Bucket }).CONTRIB = undefined;
    expect(await code(await publish(unbound.h, unbound.bucket, "loc-1"))).toBe("not_configured");
  });
});

describe("claiming", () => {
  it("claims nothing until the lever opens, then the oldest free package, one live claim each", async () => {
    const { h, bucket } = world();
    for (const run of ["loc-1", "loc-2"]) expect((await publish(h, bucket, run)).status).toBe(201);
    const a = await agent(h), b = await agent(h, "u-admin"), p = await agent(h, "u-patroller");
    lever(h, "closed");
    const shut = await claim(h, a.token);
    expect(shut.res.status).toBe(503);
    expect(shut.body.code).toBe("claims_closed");
    lever(h, "open");
    const taken = await claim(h, a.token);
    expect(taken.res.status).toBe(201);
    const held = taken.body.claim!;
    expect(held).toMatchObject({ run_id: "loc-1", state: "live", package_url: `/api/claims/${held.id}/package`,
      bundle_url: `/api/index/bundles/${BUNDLE}` });
    expect(held.id).toMatch(/^clm-[0-9a-f]{32}$/);
    expect(held.lease_until).toBe(held.claimed_at + CLAIM_LEASE_MS);
    expect(held.expires_at).toBe(held.claimed_at + CLAIM_MAX_LIFE_MS);
    expect(held.package).toMatchObject({ envelope_sha256: sha256(envelope("loc-1")), universe_sha256: UNIVERSE });
    expect((await claim(h, b.token)).body.claim?.run_id).toBe("loc-2");
    const none = await claim(h, p.token);
    expect(none.res.status).toBe(200);
    expect(none.body.claim).toBeNull();
    const list = await get(h.env, "/api/packages", { bearer: p.token });
    expect(list.headers.get("Cache-Control")).toBe("no-store");
    const body = await list.json() as Awaited<ReturnType<typeof listed>>;
    expect(body.claims_open).toBe(true);
    expect(body.packages.map(x => [x.run_id, x.available])).toEqual([["loc-1", false], ["loc-2", false]]);
  });

  it("gives a package two agents race for to exactly one of them", async () => {
    const { h, bucket } = world();
    await publish(h, bucket, "loc-1");
    const a = await agent(h), b = await agent(h, "u-admin");
    const raced = await Promise.all([claim(h, a.token), claim(h, b.token)]);
    expect(raced.map(r => r.res.status).sort()).toEqual([200, 201]);
    expect(raced.filter(r => r.body.claim).length).toBe(1);
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM contrib_claims WHERE state='live'").get()).toEqual({ n: 1 });
  });

  it("answers a repeated request with its claim, and takes a named run only when it is open and free", async () => {
    const { h, bucket } = world();
    for (const run of ["loc-1", "loc-2"]) await publish(h, bucket, run);
    const a = await agent(h), b = await agent(h, "u-admin");
    const key = requestKey();
    const first = await claim(h, a.token, { request_key: key });
    expect(first.res.status).toBe(201);
    const again = await claim(h, a.token, { request_key: key });
    expect(again.res.status).toBe(200);
    expect(again.body.claim?.id).toBe(first.body.claim?.id);
    expect((await claim(h, a.token, { request_key: key, run_id: "loc-2" })).body.code).toBe("idempotency_conflict");
    const held = await claim(h, b.token, { run_id: "loc-1" });
    expect(held.res.status).toBe(409);
    expect(held.body.code).toBe("claimed");
    expect((await claim(h, b.token, { run_id: "loc-9" })).res.status).toBe(404);
    expect((await claim(h, b.token, { run_id: "loc-2" })).body.claim?.run_id).toBe("loc-2");
    for (const body of [{ request_key: "x" }, { request_key: "A".repeat(32) }, { run_id: "../x" }, { run_id: 7 }, { extra: 1 }]) {
      expect((await claim(h, b.token, body)).res.status, JSON.stringify(body)).toBe(422);
    }
  });

  it("refuses anyone but an owned agent whose owner accepted CC0", async () => {
    const { h, bucket } = world();
    await publish(h, bucket, "loc-1");
    expect((await post(h.env, "/api/claims", { request_key: requestKey() })).status).toBe(401);
    expect((await post(h.env, "/api/claims", { request_key: requestKey() }, { user: "u-human" })).status).toBe(403);
    expect((await post(h.env, "/api/claims", { request_key: requestKey() }, { bearer: PIPELINE_TOKEN })).status).toBe(403);
    const unagreed = await agent(h, "u-patroller", false);
    expect((await claim(h, unagreed.token)).body.code).toBe("agreement_required");
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM contrib_claims").get()).toEqual({ n: 0 });
  });

  it("caps each owner's live and daily claims, all their agents together", async () => {
    const { h, bucket } = world();
    for (const run of ["loc-1", "loc-2", "loc-3"]) await publish(h, bucket, run);
    const a = await agent(h), second = await agent(h);
    const one = (await claim(h, a.token)).body.claim!;
    expect((await claim(h, second.token)).body.claim?.run_id).toBe("loc-2");
    const over = await claim(h, a.token);
    expect(over.res.status).toBe(409);
    expect(over.body.code).toBe("claim_limit");
    // Another owner's agents have their own.
    const other = await agent(h, "u-admin");
    expect((await claim(h, other.token)).body.claim?.run_id).toBe("loc-3");
    // The day's count includes the claims that ended.
    expect((await act(h, a.token, one.id, "release", { reason: "gave_up" })).status).toBe(200);
    const now = Date.now();
    const insert = h.db.prepare("INSERT INTO contrib_claims (id,run_id,agent_id,owner_id,request_key,state,claimed_at," +
      "renewed_at,lease_until,ended_at,end_reason) VALUES (?,'loc-1',?,'u-human',?,'released',?,?,?,?,'gave_up')");
    for (let i = 0; i < CLAIMS_DAILY_PER_OWNER - 2; i += 1) {
      insert.run("clm-" + String(i).padStart(32, "0"), a.agent.id, requestKey(), now, now, now, now);
    }
    const quota = await claim(h, a.token);
    expect(quota.res.status).toBe(429);
    expect(quota.body.code).toBe("claim_quota");
    expect(Number(quota.res.headers.get("Retry-After"))).toBeGreaterThanOrEqual(60);
  });

  it("first ends lapsed claims, and claims whose agent lost access", async () => {
    const { h, bucket } = world();
    await publish(h, bucket, "loc-1");
    const a = await agent(h), b = await agent(h, "u-admin"), p = await agent(h, "u-patroller");
    const first = (await claim(h, a.token)).body.claim!;
    h.db.prepare("UPDATE contrib_claims SET lease_until=? WHERE id=?").run(Date.now() - 1, first.id);
    expect((await listed(h, b.token)).packages).toEqual([expect.objectContaining({ run_id: "loc-1", available: true })]);
    const second = (await claim(h, b.token)).body.claim!;
    expect(second.run_id).toBe("loc-1");
    expect(row(h, first.id)).toMatchObject({ state: "expired", end_reason: "expired" });
    // A blocked owner's agents stop at once, and the next claim ends their claims.
    h.db.prepare("UPDATE users SET role='blocked' WHERE id='u-admin'").run();
    expect((await act(h, b.token, second.id, "renew")).status).toBe(401);
    expect((await claim(h, p.token)).body.claim?.run_id).toBe("loc-1");
    expect(row(h, second.id)).toMatchObject({ state: "revoked", end_reason: "access_lost" });
  });
});

describe("working on a claim", () => {
  it("downloads the package's exact bytes, to the claiming agent only", async () => {
    const { h, bucket } = world();
    await publish(h, bucket, "loc-1");
    const a = await agent(h), b = await agent(h, "u-admin");
    const held = (await claim(h, a.token)).body.claim!;
    const fetchPackage = (token: string, method = "GET") =>
      app.request(held.package_url, { method, headers: { Authorization: `Bearer ${token}` } }, h.env);
    const bytes = envelope("loc-1");
    const res = await fetchPackage(a.token);
    expect(res.status).toBe(200);
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(bytes);
    expect(res.headers.get("Content-Digest")).toBe(sha256(bytes));
    expect(res.headers.get("Content-Type")).toBe("application/json");
    expect(res.headers.get("Cache-Control")).toBe("private, no-store");
    expect((await fetchPackage(b.token)).status).toBe(404);
    // HEAD is answered from D1 alone.
    const reads = bucket.gets.length;
    const head = await fetchPackage(a.token, "HEAD");
    expect(head.status).toBe(200);
    expect(head.headers.get("Content-Length")).toBe(String(bytes.length));
    expect(bucket.gets.length).toBe(reads);
    // A claim that ended downloads nothing.
    expect((await act(h, a.token, held.id, "release", { reason: "gave_up" })).status).toBe(200);
    const ended = await fetchPackage(a.token);
    expect(ended.status).toBe(409);
    expect(await code(ended)).toBe("lease");
    (h.env as unknown as { CONTRIB?: Bucket }).CONTRIB = undefined;
    expect(await code(await fetchPackage(a.token))).toBe("not_configured");
  });

  it("renews up to the claim's hard lifetime, and not once the lease has lapsed", async () => {
    const { h, bucket } = world();
    await publish(h, bucket, "loc-1");
    const a = await agent(h), b = await agent(h, "u-admin");
    const held = (await claim(h, a.token)).body.claim!;
    const renewed = await act(h, a.token, held.id, "renew");
    expect(renewed.status).toBe(200);
    expect(row(h, held.id).lease_until).toBeGreaterThanOrEqual(held.lease_until);
    // Six days and 23 hours in, a renewal reaches only to the seventh day.
    const now = Date.now();
    h.db.prepare("UPDATE contrib_claims SET claimed_at=?, lease_until=? WHERE id=?")
      .run(now - CLAIM_MAX_LIFE_MS + 3600000, now + 60000, held.id);
    expect((await act(h, a.token, held.id, "renew")).status).toBe(200);
    expect(row(h, held.id).lease_until).toBe(now + 3600000);
    expect((await act(h, b.token, held.id, "renew")).status).toBe(404);
    h.db.prepare("UPDATE contrib_claims SET lease_until=? WHERE id=?").run(Date.now() - 1, held.id);
    const lapsed = await act(h, a.token, held.id, "renew");
    expect(lapsed.status).toBe(409);
    expect(await code(lapsed)).toBe("claim_expired");
  });

  it("links its agent's own receipts, and only a claim with one is done", async () => {
    const { h, bucket } = world();
    for (const run of ["loc-1", "loc-2", "loc-3"]) await publish(h, bucket, run);
    const a = await agent(h), b = await agent(h, "u-admin");
    const one = (await claim(h, a.token)).body.claim!;
    const two = (await claim(h, a.token)).body.claim!;
    const theirs = (await claim(h, b.token)).body.claim!;
    const early = await act(h, a.token, one.id, "release", { reason: "done" });
    expect(early.status).toBe(409);
    expect(await code(early)).toBe("no_submission");
    const mine = submission(h, a.agent.id), others = submission(h, b.agent.id, "u-admin");
    const linked = await act(h, a.token, one.id, "submissions", { submission_id: mine });
    expect(linked.status).toBe(200);
    expect((await linked.json() as { link: unknown }).link).toMatchObject({ submission_id: mine, claim_id: one.id });
    expect((await act(h, a.token, one.id, "submissions", { submission_id: mine })).status).toBe(200);
    expect(await code(await act(h, a.token, two.id, "submissions", { submission_id: mine }))).toBe("linked_elsewhere");
    expect((await act(h, a.token, one.id, "submissions", { submission_id: others })).status).toBe(404);
    expect((await act(h, b.token, one.id, "submissions", { submission_id: others })).status).toBe(404);
    expect((await act(h, a.token, one.id, "submissions", { submission_id: "sub-x" })).status).toBe(422);
    const done = await act(h, a.token, one.id, "release", { reason: "done" });
    expect(done.status).toBe(200);
    expect((await done.json() as { claim: Claim }).claim).toMatchObject({ state: "completed", end_reason: "done" });
    // Repeated, a release answers as it did; a claim that ended takes nothing more.
    expect((await act(h, a.token, one.id, "release", { reason: "done" })).status).toBe(200);
    expect(await code(await act(h, a.token, one.id, "release", { reason: "gave_up" }))).toBe("lease");
    expect(await code(await act(h, a.token, one.id, "submissions", { submission_id: submission(h, a.agent.id) }))).toBe("lease");
    const given = await act(h, a.token, two.id, "release", { reason: "sources_mismatch", note: "revision 42 is gone" });
    expect((await given.json() as { claim: Claim }).claim.state).toBe("released");
    expect(row(h, two.id)).toMatchObject({ state: "released", end_reason: "sources_mismatch", note: "revision 42 is gone" });
    expect((await act(h, a.token, theirs.id, "release", { reason: "gave_up" })).status).toBe(404);
    expect((await act(h, b.token, theirs.id, "release", { reason: "finished" })).status).toBe(422);
    expect((await act(h, b.token, theirs.id, "release", { reason: "other", note: "x".repeat(501) })).status).toBe(422);
  });

  it("caps a claim's receipts", async () => {
    const { h, bucket } = world();
    await publish(h, bucket, "loc-1");
    const a = await agent(h);
    const held = (await claim(h, a.token)).body.claim!;
    const insert = h.db.prepare("INSERT INTO contrib_claim_submissions (submission_id,claim_id,linked_at) VALUES (?,?,?)");
    for (let i = 0; i < CLAIM_LINKS_MAX; i += 1) insert.run("sub-" + sha256(String(i)), held.id, Date.now());
    const over = await act(h, a.token, held.id, "submissions", { submission_id: submission(h, a.agent.id) });
    expect(over.status).toBe(409);
    expect(await code(over)).toBe("link_limit");
  });

  it("stops renewal, links and release under the brake", async () => {
    const { h, bucket } = world();
    await publish(h, bucket, "loc-1");
    const a = await agent(h);
    const held = (await claim(h, a.token)).body.claim!;
    const sub = submission(h, a.agent.id);
    h.db.prepare("UPDATE site_settings SET value='locked' WHERE key='site_write_mode'").run();
    for (const [action, body] of [["renew", {}], ["submissions", { submission_id: sub }], ["release", { reason: "gave_up" }]] as const) {
      expect((await act(h, a.token, held.id, action, body)).status, action).toBe(503);
    }
    expect(row(h, held.id).state).toBe("live");
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM contrib_claim_submissions").get()).toEqual({ n: 0 });
  });
});

describe("the coordinator", () => {
  it("re-opening a package ends its live claim, and the view shows every claim with its receipts", async () => {
    const { h, bucket } = world();
    await publish(h, bucket, "loc-1");
    const a = await agent(h), b = await agent(h, "u-admin");
    const first = (await claim(h, a.token)).body.claim!;
    const sub = submission(h, a.agent.id);
    expect((await act(h, a.token, first.id, "submissions", { submission_id: sub })).status).toBe(200);
    const setState = (state: string, bearer = PIPELINE_TOKEN) =>
      post(h.env, "/api/packages/loc-1/state", { state }, { bearer, origin: null });
    expect((await setState("open", a.token)).status).toBe(403);
    const reopened = await setState("open");
    expect(reopened.status).toBe(200);
    expect((await reopened.json() as { ended_claims: number }).ended_claims).toBe(1);
    expect(row(h, first.id)).toMatchObject({ state: "revoked", end_reason: "coordinator" });
    expect(await code(await act(h, a.token, first.id, "renew"))).toBe("lease");
    const second = (await claim(h, b.token)).body.claim!;
    // Lapsed and not yet swept, it reads as expired; it was claimed after the first.
    h.db.prepare("UPDATE contrib_claims SET claimed_at=claimed_at+1, lease_until=? WHERE id=?").run(Date.now() - 1, second.id);
    expect((await get(h.env, "/api/packages/loc-1/claims", { bearer: a.token })).status).toBe(403);
    const view = await get(h.env, "/api/packages/loc-1/claims", { bearer: PIPELINE_TOKEN });
    expect(view.status).toBe(200);
    const body = await view.json() as { package: { run_id: string };
      claims: Array<{ id: string; state: string; end_reason: string | null; submissions: unknown[] }> };
    expect(body.package.run_id).toBe("loc-1");
    expect(body.claims.map(k => [k.id, k.state, k.end_reason]))
      .toEqual([[first.id, "revoked", "coordinator"], [second.id, "expired", null]]);
    expect(body.claims[0].submissions).toEqual([expect.objectContaining({ id: sub, state: "accepted",
      verdict: { verifier: "test", code: "ok" } })]);
    expect(body.claims[1].submissions).toEqual([]);
    // Retired, it is listed and claimed no more.
    expect((await setState("retired")).status).toBe(200);
    expect((await listed(h, a.token)).packages).toEqual([]);
    expect((await claim(h, a.token)).body.claim).toBeNull();
    expect((await setState("gone")).status).toBe(422);
    expect((await post(h.env, "/api/packages/loc-9/state", { state: "open" }, { bearer: PIPELINE_TOKEN, origin: null })).status).toBe(404);
  });

  it("revoking an agent at /agents ends its live claims", async () => {
    const { h, bucket } = world();
    await publish(h, bucket, "loc-1");
    const a = await agent(h);
    const held = (await claim(h, a.token)).body.claim!;
    expect((await post(h.env, `/api/agents/${a.agent.id}/revoke`, {}, { user: "u-human" })).status).toBe(200);
    expect(row(h, held.id)).toMatchObject({ state: "revoked", end_reason: "revoked" });
  });
});
