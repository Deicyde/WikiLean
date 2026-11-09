import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { describe, it, expect, vi } from "vitest";
import { app } from "../src/index.js";
import { CC0_AGREEMENT_VERSION } from "../src/articles-overlay.js";
import { SUBMISSION_DAILY_COUNT, SUBMISSION_PENDING_PER_OWNER, SUBMISSION_MAX_BYTES, SUBMISSION_DAILY_BYTES,
  SUBMISSION_RETAINED_OWNER_BYTES, SUBMISSION_RETAINED_GLOBAL_BYTES } from "../src/submissions.js";
import { setup, post, get, blockNetwork, PIPELINE_TOKEN, type Harness } from "./helpers/harness.js";

blockNetwork();
const digest = (bytes: string | Uint8Array) => createHash("sha256").update(bytes).digest("hex");
const fixture = resolve(import.meta.dirname, "../../scripts/pipeline/fixtures/recordset");
function upload(n = 0) {
  const files: Record<string, string> = {};
  for (const item of readdirSync(fixture, { recursive: true, withFileTypes: true })) {
    if (item.isFile()) {
      const path = resolve(item.parentPath, item.name).slice(fixture.length + 1);
      files[path] = readFileSync(resolve(fixture, path), "utf8");
    }
  }
  if (n) files["SEAL.json"] += " ".repeat(n); // distinct seal bytes, same valid JSON
  return { files, body: JSON.stringify({ schema: "wikilean.submission-upload/1", files }), seal: digest(files["SEAL.json"]) };
}
async function agent(h: Harness, owner = "u-human", agreed = true) {
  if (agreed) h.db.prepare("INSERT OR IGNORE INTO user_agreements (user_id,cc0_version,accepted_at) VALUES (?,?,?)")
    .run(owner, CC0_AGREEMENT_VERSION, Date.now());
  const res = await post(h.env, "/api/agents", { name: "Laptop" }, { user: owner });
  expect(res.status).toBe(201);
  return await res.json() as { token: string; agent: { id: string } };
}
async function send(h: Harness, token: string, data = upload(), extra: Record<string, string> = {}) {
  return app.request("/api/submissions", { method: "POST", body: data.body, headers: {
    "Authorization": "Bearer " + token, "Content-Type": "application/json",
    "Content-Digest": digest(data.body), "Idempotency-Key": data.seal, ...extra,
  } }, h.env);
}
async function claim(h: Harness) {
  const res = await post(h.env, "/api/submissions/claim", {}, { bearer: PIPELINE_TOKEN, origin: null });
  expect(res.status).toBe(200);
  return (await res.json() as { job: Record<string, string | number> | null }).job;
}
async function verdict(h: Harness, job: NonNullable<Awaited<ReturnType<typeof claim>>>, overrides: Record<string, unknown> = {}, lease = String(job.lease_id)) {
  return app.request(`/api/submissions/${job.id}/verdict`, { method: "POST", headers: {
    "Authorization": "Bearer " + PIPELINE_TOKEN, "Content-Type": "application/json", "X-Intake-Lease": lease,
  }, body: JSON.stringify({ state: "accepted", body_sha256: job.body_sha256, seal_sha256: job.seal_sha256,
    verdict: { verifier: "scripts/articles/verify_submission.py/1", code: "verified", records: 7 }, ...overrides }) }, h.env);
}
function count(h: Harness, table = "pipeline_submissions") {
  return (h.db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get() as { n: number }).n;
}

describe("self-service submission transport", () => {
  it("lets only the owner explicitly accept terms, without creating a throwaway edit", async () => {
    const h = setup(), a = await agent(h, "u-human", false);
    expect((await post(h.env, "/api/agents/agreement", { cc0: true }, { bearer: a.token })).status).toBe(403);
    expect((await post(h.env, "/api/agents/agreement", { cc0: true }, { bearer: PIPELINE_TOKEN })).status).toBe(403);
    expect((await post(h.env, "/api/agents/agreement", { cc0: false }, { user: "u-human" })).status).toBe(403);
    expect((await post(h.env, "/api/agents/agreement", { cc0: true }, { user: "u-human", origin: "https://elsewhere.invalid" })).status).toBe(403);
    expect(count(h, "user_agreements")).toBe(0);
    expect((await post(h.env, "/api/agents/agreement", { cc0: true }, { user: "u-human" })).status).toBe(200);
    expect(count(h, "article_tags")).toBe(0);
    expect((await send(h, a.token)).status).toBe(202);
    const page = await get(h.env, "/agents", { user: "u-human" });
    expect(await page.text()).toContain("You have accepted the CC0 contribution agreement");
  });

  it("keeps the first contribution's Turnstile requirement on the owner agreement", async () => {
    const h = setup({ turnstileSecret: "fixture-secret", turnstileSiteKey: "fixture-site-key" });
    const res = await post(h.env, "/api/agents/agreement", { cc0: true }, { user: "u-human" });
    expect(res.status).toBe(403);
    expect(count(h, "user_agreements")).toBe(0);
    const page = await get(h.env, "/agents", { user: "u-human" });
    expect(page.headers.get("Content-Security-Policy")).toContain("https://challenges.cloudflare.com");
    expect(await page.text()).toContain('data-sitekey="fixture-site-key"');
  });

  it("queues exact bytes once, returns a private receipt, and never changes live tables", async () => {
    const h = setup(), a = await agent(h), data = upload();
    const before = h.db.prepare("SELECT * FROM articles").all();
    const res = await send(h, a.token, data);
    expect(res.status).toBe(202);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    const { submission } = await res.json() as { submission: { status_url: string } };
    expect(submission).toMatchObject({ state: "queued", published: false, agent_id: a.agent.id, seal_sha256: data.seal });
    expect(JSON.stringify(submission)).not.toContain(a.token);
    expect((await send(h, a.token, data)).status).toBe(200);
    expect(count(h)).toBe(1);
    const chunks = h.db.prepare("SELECT bytes FROM pipeline_submission_chunks ORDER BY ordinal").all() as { bytes: Uint8Array }[];
    expect(Buffer.concat(chunks.map(x => Buffer.from(x.bytes))).toString()).toBe(data.body);
    expect(h.db.prepare("SELECT * FROM articles").all()).toEqual(before);
    expect(count(h, "article_tags")).toBe(0);
    expect((await get(h.env, submission.status_url, { bearer: a.token })).status).toBe(200);
    expect((await get(h.env, submission.status_url, { user: "u-human" })).status).toBe(200);
    expect((await get(h.env, submission.status_url, { user: "u-patroller" })).status).toBe(404);
    expect((await get(h.env, submission.status_url)).status).toBe(401);
  });

  it("requires an owned agent and the owner's agreement", async () => {
    const h = setup(), a = await agent(h, "u-human", false);
    expect((await send(h, "invalid")).status).toBe(401);
    expect((await send(h, PIPELINE_TOKEN)).status).toBe(403);
    expect((await post(h.env, "/api/submissions", {}, { user: "u-human" })).status).toBe(403);
    expect((await send(h, a.token)).status).toBe(403);
    expect(count(h)).toBe(0);
  });

  it.each(["agent", "owner", "revoked"])("refuses a %s whose access was removed", async kind => {
    const h = setup(), a = await agent(h);
    if (kind === "revoked") h.db.prepare("UPDATE agent_tokens SET revoked_at=1 WHERE agent_id=?").run(a.agent.id);
    else h.db.prepare("UPDATE users SET role='blocked' WHERE id=?").run(kind === "owner" ? "u-human" : a.agent.id);
    expect((await send(h, a.token)).status).toBe(401);
    expect(count(h)).toBe(0);
  });

  it("honors the write brake, shared burst budget and same-origin guard", async () => {
    const h = setup(), a = await agent(h);
    h.db.prepare("UPDATE site_settings SET value='locked' WHERE key='site_write_mode'").run();
    expect((await send(h, a.token)).status).toBe(503);
    h.db.prepare("UPDATE site_settings SET value='open' WHERE key='site_write_mode'").run();
    expect((await send(h, a.token, upload(), { Origin: "https://elsewhere.invalid" })).status).toBe(403);
    const limit = vi.fn(async () => ({ success: false })); h.env.EDIT_LIMITER.limit = limit;
    expect((await send(h, a.token)).status).toBe(429);
    expect(limit).toHaveBeenCalledWith({ key: "agents-of:u-human" });
  });

  it("refuses changed bytes, bad seal identity, unexpected paths and media types", async () => {
    const h = setup(), a = await agent(h), data = upload();
    expect((await send(h, a.token, data, { "Content-Digest": "0".repeat(64) })).status).toBe(422);
    expect((await send(h, a.token, data, { "Idempotency-Key": "0".repeat(64) })).status).toBe(422);
    expect((await send(h, a.token, data, { "Content-Type": "application/zip" })).status).toBe(415);
    data.files["../escape"] = "no";
    data.body = JSON.stringify({ schema: "wikilean.submission-upload/1", files: data.files });
    expect((await send(h, a.token, data)).status).toBe(422);
    expect(count(h)).toBe(0);
  });

  it("caps bodies with no length header and verifies original UTF-8 bytes", async () => {
    const h = setup(), a = await agent(h);
    const large = { ...upload(), body: "x".repeat(SUBMISSION_MAX_BYTES + 1) };
    expect((await send(h, a.token, large)).status).toBe(413);
    const bytes = new Uint8Array([0xff]);
    const res = await app.request("/api/submissions", { method: "POST", body: bytes, headers: {
      Authorization: "Bearer " + a.token, "Content-Type": "application/json",
      "Content-Digest": digest(bytes), "Idempotency-Key": "0".repeat(64),
    } }, h.env);
    expect(res.status).toBe(422);
    expect(count(h)).toBe(0);
  });

  it("does not overwrite a seal's original upload", async () => {
    const h = setup(), a = await agent(h), data = upload();
    expect((await send(h, a.token, data)).status).toBe(202);
    data.body += " ";
    expect((await send(h, a.token, data)).status).toBe(409);
    expect(count(h)).toBe(1);
  });

  it("preserves Unicode across multiple binary chunks all the way to the validator", async () => {
    const h = setup(), a = await agent(h), data = upload();
    data.files["SEAL.json"] = JSON.stringify({ ...JSON.parse(data.files["SEAL.json"]), extra: "∑𝕏".repeat(30000) });
    data.seal = digest(data.files["SEAL.json"]);
    data.body = JSON.stringify({ schema: "wikilean.submission-upload/1", files: data.files });
    expect((await send(h, a.token, data)).status).toBe(202); // transport only; the checker rejects this extra seal key
    expect(count(h, "pipeline_submission_chunks")).toBeGreaterThan(1);
    const job = (await claim(h))!;
    const res = await app.request(`/api/submissions/${job.id}/body`, { headers: {
      Authorization: "Bearer " + PIPELINE_TOKEN, "X-Intake-Lease": String(job.lease_id),
    } }, h.env);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe(data.body);
    expect(res.headers.get("Content-Digest")).toBe(digest(data.body));
  });

  it.each(["daily", "owner", "global"])("enforces the %s byte cap without deleting prior evidence", async kind => {
    const h = setup(), a = await agent(h), b = await agent(h, "u-patroller");
    const limit = kind === "daily" ? SUBMISSION_DAILY_BYTES : kind === "owner" ? SUBMISSION_RETAINED_OWNER_BYTES : SUBMISSION_RETAINED_GLOBAL_BYTES;
    const existing = kind === "global" ? b.agent.id : a.agent.id;
    const owner = kind === "global" ? "u-patroller" : "u-human";
    const time = kind === "daily" ? Date.now() : 1;
    const seed = h.db.prepare("INSERT INTO pipeline_submissions (id,agent_id,owner_id,seal_sha256,body_sha256,body_bytes,received_at,updated_at,state) VALUES (?,?,?,?,?,?,?,?,?)");
    for (let n = 0; n < limit / SUBMISSION_MAX_BYTES; n++) {
      const hash = digest(String(n));
      seed.run("sub-" + hash, existing, owner, hash, hash, SUBMISSION_MAX_BYTES, time, time, "accepted");
    }
    const before = count(h);
    const res = await send(h, a.token);
    expect(res.status).toBe(kind === "daily" ? 429 : 503);
    expect(await res.json()).toMatchObject({ code: kind === "daily" ? "quota_bytes" : "storage_quota" });
    expect(count(h)).toBe(before);
    expect(count(h, "pipeline_submission_chunks")).toBe(0);
  });

  it("shares durable pending and daily quotas between an owner's agents, including concurrent posts", async () => {
    const h = setup(), a = await agent(h), b = await agent(h);
    const sent = await Promise.all(Array.from({ length: SUBMISSION_PENDING_PER_OWNER + 1 }, (_, i) => send(h, i % 2 ? a.token : b.token, upload(i + 1))));
    expect(sent.filter(r => r.status === 202)).toHaveLength(SUBMISSION_PENDING_PER_OWNER);
    expect(sent.filter(r => r.status === 429)).toHaveLength(1);
    expect(sent.find(r => r.status === 429)!.headers.get("Retry-After")).toBe("60");
    h.db.prepare("UPDATE pipeline_submissions SET state='accepted'").run();
    for (let i = SUBMISSION_PENDING_PER_OWNER; i < SUBMISSION_DAILY_COUNT; i++) {
      expect((await send(h, a.token, upload(i + 20))).status).toBe(202);
      h.db.prepare("UPDATE pipeline_submissions SET state='accepted'").run();
    }
    expect((await send(h, b.token, upload(100))).status).toBe(429);
    expect(count(h)).toBe(SUBMISSION_DAILY_COUNT);
    expect((await send(h, b.token, upload(1))).status).toBe(200);
  });

  it("rolls back metadata if a chunk fails", async () => {
    const h = setup(), a = await agent(h);
    h.db.exec("CREATE TRIGGER break_chunk BEFORE INSERT ON pipeline_submission_chunks BEGIN SELECT RAISE(ABORT,'fixture failure'); END");
    expect((await send(h, a.token)).status).toBe(500);
    expect(count(h)).toBe(0);
    expect(count(h, "pipeline_submission_chunks")).toBe(0);
  });
});

describe("leased authoritative validation", () => {
  it("lets only the pipeline validator claim, read exact bytes and commit a verdict", async () => {
    const h = setup(), a = await agent(h), data = upload();
    await send(h, a.token, data);
    expect((await post(h.env, "/api/submissions/claim", {}, { bearer: a.token })).status).toBe(403);
    expect((await post(h.env, "/api/submissions/claim", {}, { user: "u-admin" })).status).toBe(403);
    const job = (await claim(h))!;
    expect(await claim(h)).toBeNull();
    const body = await app.request(`/api/submissions/${job.id}/body`, { headers: {
      Authorization: "Bearer " + PIPELINE_TOKEN, "X-Intake-Lease": String(job.lease_id),
    } }, h.env);
    expect(body.status).toBe(200);
    expect(await body.text()).toBe(data.body);
    expect((await verdict(h, job, { body_sha256: "0".repeat(64) })).status).toBe(422);
    expect((await verdict(h, job, {}, "wrong-lease")).status).toBe(409);
    const done = await verdict(h, job);
    expect(done.status).toBe(200);
    expect(await done.json()).toMatchObject({ submission: { state: "accepted", published: false } });
    expect((await verdict(h, job)).status).toBe(409);
    expect(count(h, "article_tags")).toBe(0);
  });

  it("recovers an expired lease and fences off the old worker", async () => {
    const h = setup(), a = await agent(h); await send(h, a.token);
    const old = (await claim(h))!;
    h.db.prepare("UPDATE pipeline_submissions SET lease_until=0").run();
    const current = (await claim(h))!;
    expect(current.id).toBe(old.id);
    expect(current.lease_id).not.toBe(old.lease_id);
    expect((await verdict(h, old)).status).toBe(409);
    expect((await verdict(h, current, { state: "rejected", verdict: { verifier: "scripts/articles/verify_submission.py/1", code: "record_set", error: "bad source pin" } })).status).toBe(200);
    expect(await claim(h)).toBeNull();
  });

  it("shows a failed receipt after three abandoned leases, without retrying forever", async () => {
    const h = setup(), a = await agent(h); const response = await send(h, a.token);
    const { submission } = await response.json() as { submission: { status_url: string } };
    for (let i = 0; i < 3; i++) {
      expect(await claim(h)).not.toBeNull();
      h.db.prepare("UPDATE pipeline_submissions SET lease_until=0").run();
    }
    expect(await claim(h)).toBeNull();
    const got = await get(h.env, submission.status_url, { bearer: a.token });
    expect(await got.json()).toMatchObject({ submission: { state: "failed", verdict: { code: "validator_unavailable" } } });
  });
});
