// Durable transport for sealed pipeline results. A 202 means queued, never
// validated or published. scripts/articles/intake_worker.py runs the existing
// normative Python checker and returns a leased verdict. No live table writes.
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import { bearerMatches, getUser, type AuthUser } from "./auth.js";
import { editLimitKey } from "./agents.js";
import { CC0_AGREEMENT_VERSION, loadAgreement } from "./articles-overlay.js";
import { checkOrigin, readBytesCapped, readJsonCapped, MAX_JSON_BODY_BYTES } from "./security.js";
import { writeBrake } from "./site-settings.js";

type Ctx = Context<{ Bindings: Env }>;
export const SUBMISSION_MAX_BYTES = MAX_JSON_BODY_BYTES.submission;
export const SUBMISSION_DAILY_COUNT = 20;
export const SUBMISSION_DAILY_BYTES = 32 * 1024 * 1024;
export const SUBMISSION_PENDING_PER_OWNER = 10;
export const SUBMISSION_PENDING_GLOBAL = 100;
export const SUBMISSION_RETAINED_OWNER_BYTES = 128 * 1024 * 1024;
export const SUBMISSION_RETAINED_GLOBAL_BYTES = 1024 * 1024 * 1024;
export const SUBMISSION_LEASE_MS = 5 * 60 * 1000;
const HEX = /^[0-9a-f]{64}$/;
const ID = /^sub-[0-9a-f]{64}$/;
const CHUNK = 65536;
const encoder = new TextEncoder();
interface Row {
  id: string; agent_id: string; owner_id: string; seal_sha256: string;
  body_sha256: string; body_bytes: number; received_at: number; updated_at: number;
  state: string; attempts: number; lease_id: string | null; lease_until: number | null;
  verdict: string | null;
}
function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
async function hash(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("");
}
function refuse(c: Ctx, code: string, error: string, status: 400 | 401 | 403 | 404 | 409 | 413 | 415 | 422 | 429 | 503) {
  return c.json({ ok: false, code, error, retryable: status === 429 || status === 503 }, status);
}
function receipt(c: Ctx, row: Row, status: 200 | 202 = 200) {
  return c.json({ ok: true, submission: {
    id: row.id, state: row.state, seal_sha256: row.seal_sha256,
    body_sha256: row.body_sha256, bytes: row.body_bytes,
    agent_id: row.agent_id, received_at: row.received_at, updated_at: row.updated_at,
    published: false, verdict: row.verdict ? JSON.parse(row.verdict) : null,
    status_url: `/api/submissions/${row.id}`,
  } }, status);
}
async function rowById(c: Ctx, id: string): Promise<Row | null> {
  if (!ID.test(id)) return null;
  const { results } = await c.env.DB.prepare("SELECT * FROM pipeline_submissions WHERE id = ?").bind(id).all<Row>();
  return results[0] ?? null;
}
async function pipeline(c: Ctx): Promise<AuthUser | Response> {
  const authz = c.req.header("Authorization");
  const token = authz?.startsWith("Bearer ") ? authz.slice(7) : null;
  if (!token || !c.env.PIPELINE_TOKEN || !await bearerMatches(token, c.env.PIPELINE_TOKEN)) {
    return refuse(c, "validator_required", "the intake validator credential is required", 403);
  }
  const user = await getUser(c);
  if (!user || user.id !== "pipeline" || user.role !== "bot") {
    return refuse(c, "validator_required", "the intake validator account is unavailable", 403);
  }
  return user;
}

export function registerSubmissionRoutes(app: Hono<{ Bindings: Env }>): void {
  app.use("/api/submissions*", async (c, next) => {
    c.header("Cache-Control", "no-store");
    await next();
  });
  app.post("/api/submissions", async c => {
    const origin = checkOrigin(c); if (origin) return origin;
    const user = await getUser(c);
    if (!user) return refuse(c, "authentication", "an agent token is required", 401);
    if (user.role !== "agent" || !user.ownerId) return refuse(c, "agent_required", "submit using an owned agent account", 403);
    const brake = await writeBrake(c); if (brake) return brake;
    const agreement = await loadAgreement(c.env, user.ownerId);
    if (agreement?.cc0_version !== CC0_AGREEMENT_VERSION) {
      return refuse(c, "agreement_required", "the agent owner must accept the CC0 contribution agreement on WikiLean first", 403);
    }
    if (!(await c.env.EDIT_LIMITER.limit({ key: editLimitKey(user) })).success) {
      c.header("Retry-After", "60");
      return refuse(c, "rate_limit", "submission rate limit reached", 429);
    }
    if (c.req.header("Content-Type")?.split(";", 1)[0].trim() !== "application/json") {
      return refuse(c, "content_type", "send a wikilean.submission-upload/1 JSON envelope", 415);
    }
    const key = c.req.header("Idempotency-Key") ?? "";
    const digest = c.req.header("Content-Digest") ?? "";
    if (!HEX.test(key) || !HEX.test(digest)) return refuse(c, "envelope", "digest headers must be lowercase SHA-256 hex", 422);
    const bytes = await readBytesCapped(c.req.raw, MAX_JSON_BODY_BYTES.submission);
    if (!bytes) return refuse(c, "body_too_large", "upload exceeds 4 MiB; export a smaller sealed shard", 413);
    if (await hash(bytes) !== digest) return refuse(c, "body_digest", "Content-Digest does not match the uploaded bytes", 422);
    let envelope: unknown;
    try { envelope = JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes)); }
    catch { return refuse(c, "envelope", "upload must be UTF-8 JSON", 422); }
    if (!object(envelope) || envelope.schema !== "wikilean.submission-upload/1" ||
        Object.keys(envelope).sort().join(",") !== "files,schema" || !object(envelope.files) ||
        Object.keys(envelope.files).length > 512 || typeof envelope.files["SEAL.json"] !== "string" ||
        !Object.entries(envelope.files).every(([path, value]) => typeof value === "string" &&
          /^(SEAL\.json|universe\.json|sources\.jsonl|records\/enwiki-[1-9][0-9]*-[1-9][0-9]*\.jsonl|receipts\/[0-9a-f]{64}\.json)$/.test(path))) {
      return refuse(c, "envelope", "expected only sealed record-set files (at most 512)", 422);
    }
    if (await hash(encoder.encode(envelope.files["SEAL.json"])) !== key) return refuse(c, "idempotency_key", "Idempotency-Key does not match SEAL.json", 422);
    const id = "sub-" + await hash(encoder.encode(`${user.id}\n${key}`));
    const existing = await rowById(c, id);
    if (existing) return existing.body_sha256 === digest ? receipt(c, existing) :
      refuse(c, "idempotency_conflict", "this seal already has a different upload; retain the original receipt", 409);
    const now = Date.now();
    const day = Math.floor(now / 86400000) * 86400000;
    const insert = c.env.DB.prepare(
      "INSERT INTO pipeline_submissions (id,agent_id,owner_id,seal_sha256,body_sha256,body_bytes,received_at,updated_at,state) " +
      "SELECT ?,?,?,?,?,?,?,?,'queued' WHERE " +
      "EXISTS (SELECT 1 FROM agent_tokens t JOIN users a ON a.id=t.agent_id JOIN users o ON o.id=t.owner_id " +
      "JOIN user_agreements g ON g.user_id=o.id WHERE t.agent_id=? AND t.owner_id=? AND t.revoked_at IS NULL " +
      "AND a.role='agent' AND o.role!='blocked' AND g.cc0_version=?) " +
      "AND (SELECT value FROM site_settings WHERE key='site_write_mode')='open' " +
      "AND (SELECT COUNT(*) FROM pipeline_submissions WHERE owner_id=? AND received_at>=?) < ? " +
      "AND (SELECT COALESCE(SUM(body_bytes),0) FROM pipeline_submissions WHERE owner_id=? AND received_at>=?) + ? <= ? " +
      "AND (SELECT COUNT(*) FROM pipeline_submissions WHERE owner_id=? AND state IN ('queued','validating')) < ? " +
      "AND (SELECT COUNT(*) FROM pipeline_submissions WHERE state IN ('queued','validating')) < ? " +
      "AND (SELECT COALESCE(SUM(body_bytes),0) FROM pipeline_submissions WHERE owner_id=?) + ? <= ? " +
      "AND (SELECT COALESCE(SUM(body_bytes),0) FROM pipeline_submissions) + ? <= ? " +
      "ON CONFLICT(agent_id,seal_sha256) DO NOTHING",
    ).bind(id, user.id, user.ownerId, key, digest, bytes.length, now, now,
      user.id, user.ownerId, CC0_AGREEMENT_VERSION,
      user.ownerId, day, SUBMISSION_DAILY_COUNT, user.ownerId, day, bytes.length, SUBMISSION_DAILY_BYTES,
      user.ownerId, SUBMISSION_PENDING_PER_OWNER, SUBMISSION_PENDING_GLOBAL,
      user.ownerId, bytes.length, SUBMISSION_RETAINED_OWNER_BYTES, bytes.length, SUBMISSION_RETAINED_GLOBAL_BYTES);
    const writes = [insert];
    for (let offset = 0; offset < bytes.length; offset += CHUNK) {
      writes.push(c.env.DB.prepare(
        "INSERT OR IGNORE INTO pipeline_submission_chunks (submission_id,ordinal,bytes) " +
        "SELECT ?,?,? WHERE EXISTS (SELECT 1 FROM pipeline_submissions WHERE id=? AND body_sha256=?)",
      ).bind(id, offset / CHUNK, bytes.slice(offset, offset + CHUNK), id, digest));
    }
    // Quotas and the whole upload commit together; concurrent agents cannot
    // spend the same owner's remaining quota or expose a partially stored set.
    const results = await c.env.DB.batch(writes);
    const row = await rowById(c, id);
    if (!row) {
      const brake = await writeBrake(c); if (brake) return brake;
      if (!(await getUser(c)) || (await loadAgreement(c.env, user.ownerId))?.cc0_version !== CC0_AGREEMENT_VERSION) {
        return refuse(c, "access_changed", "contributor access changed during the upload", 403);
      }
      const { results: usage } = await c.env.DB.prepare(
        "SELECT COUNT(CASE WHEN received_at>=? THEN 1 END) AS today, " +
        "COALESCE(SUM(CASE WHEN received_at>=? THEN body_bytes ELSE 0 END),0) AS day_bytes," +
        "COALESCE(SUM(body_bytes),0) AS retained FROM pipeline_submissions WHERE owner_id=?",
      ).bind(day, day, user.ownerId).all<{ today: number; day_bytes: number; retained: number }>();
      if (usage[0].retained + bytes.length > SUBMISSION_RETAINED_OWNER_BYTES) {
        return refuse(c, "storage_quota", "retained submissions reached 128 MiB; a maintainer must archive them before more can be uploaded", 503);
      }
      const { results: total } = await c.env.DB.prepare("SELECT COALESCE(SUM(body_bytes),0) AS retained FROM pipeline_submissions")
        .all<{ retained: number }>();
      if (total[0].retained + bytes.length > SUBMISSION_RETAINED_GLOBAL_BYTES) {
        return refuse(c, "storage_quota", "submission storage is full; a maintainer must archive retained evidence before more can be uploaded", 503);
      }
      const code = usage[0].today >= SUBMISSION_DAILY_COUNT ? "quota_submissions" :
        usage[0].day_bytes + bytes.length > SUBMISSION_DAILY_BYTES ? "quota_bytes" : "queue_capacity";
      c.header("Retry-After", String(Math.max(60, Math.ceil((day + 86400000 - now) / 1000))));
      return refuse(c, code, "submission quota or server capacity reached; retain this sealed set and try again after the indicated delay", 429);
    }
    if (row.body_sha256 !== digest) return refuse(c, "idempotency_conflict", "this seal already has a different upload", 409);
    c.header("Location", `/api/submissions/${id}`);
    return receipt(c, row, results[0].meta.changes ? 202 : 200);
  });

  app.get("/api/submissions/:id", async c => {
    const user = await getUser(c);
    if (!user) return refuse(c, "authentication", "sign in or use your agent token", 401);
    if (!(await (c.env.MCP_LIMITER ?? c.env.EDIT_LIMITER).limit({ key: `submission-read:${user.ownerId ?? user.id}` })).success) {
      c.header("Retry-After", "60");
      return refuse(c, "rate_limit", "receipt polling limit reached", 429);
    }
    const row = await rowById(c, c.req.param("id"));
    if (!row || !(row.agent_id === user.id || row.owner_id === user.id)) return refuse(c, "not_found", "submission not found", 404);
    return receipt(c, row);
  });

  app.post("/api/submissions/claim", async c => {
    const origin = checkOrigin(c); if (origin) return origin;
    const user = await pipeline(c); if (user instanceof Response) return user;
    const brake = await writeBrake(c); if (brake) return brake;
    const now = Date.now();
    // Three abandoned leases are an operational failure, not a rejected set.
    await c.env.DB.prepare("UPDATE pipeline_submissions SET state='failed',updated_at=?,lease_id=NULL,lease_until=NULL," +
      "verdict=? WHERE state='validating' AND lease_until<=? AND attempts>=3")
      .bind(now, JSON.stringify({ code: "validator_unavailable", error: "validation repeatedly stopped; a maintainer must investigate" }), now).run();
    const lease = crypto.randomUUID();
    const { results } = await c.env.DB.prepare(
      "UPDATE pipeline_submissions SET state='validating',attempts=attempts+1,lease_id=?,lease_until=?,updated_at=? " +
      "WHERE id=(SELECT id FROM pipeline_submissions WHERE state='queued' OR (state='validating' AND lease_until<=? AND attempts<3) " +
      "ORDER BY received_at,id LIMIT 1) RETURNING *",
    ).bind(lease, now + SUBMISSION_LEASE_MS, now, now).all<Row>();
    const row = results[0];
    if (!row) return c.json({ ok: true, job: null });
    return c.json({ ok: true, job: { id: row.id, agent_id: row.agent_id, owner_id: row.owner_id,
      received_at: row.received_at, seal_sha256: row.seal_sha256, body_sha256: row.body_sha256,
      bytes: row.body_bytes, lease_id: lease, lease_until: row.lease_until } });
  });

  app.get("/api/submissions/:id/body", async c => {
    const user = await pipeline(c); if (user instanceof Response) return user;
    const row = await rowById(c, c.req.param("id"));
    const leased = row?.state === "validating" && row.lease_id === c.req.header("X-Intake-Lease") && (row.lease_until ?? 0) > Date.now();
    if (!row || !(row.state === "accepted" || leased)) {
      return refuse(c, "lease", "validation lease is missing or expired", 409);
    }
    const { results } = await c.env.DB.prepare("SELECT bytes FROM pipeline_submission_chunks WHERE submission_id=? ORDER BY ordinal")
      .bind(row.id).all<{ bytes: number[] | ArrayBuffer }>();
    const body = new Uint8Array(row.body_bytes);
    let offset = 0;
    for (const result of results) {
      const chunk = new Uint8Array(result.bytes); body.set(chunk, offset); offset += chunk.length;
    }
    if (offset !== row.body_bytes || await hash(body) !== row.body_sha256) return refuse(c, "stored_digest", "stored upload failed its integrity check", 503);
    return new Response(body, { headers: { "Content-Type": "application/json", "Content-Digest": row.body_sha256, "Cache-Control": "no-store" } });
  });

  app.post("/api/submissions/:id/verdict", async c => {
    const origin = checkOrigin(c); if (origin) return origin;
    const user = await pipeline(c); if (user instanceof Response) return user;
    const brake = await writeBrake(c); if (brake) return brake;
    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.submissionVerdict, "invalid validator result");
    if (!read.ok) return read.response;
    const value = read.value;
    const row = await rowById(c, c.req.param("id"));
    if (!row || !object(value) || value.body_sha256 !== row.body_sha256 || value.seal_sha256 !== row.seal_sha256 ||
        !["accepted", "rejected", "failed"].includes(String(value.state)) || !object(value.verdict) ||
        typeof value.verdict.verifier !== "string" || typeof value.verdict.code !== "string") {
      return refuse(c, "verdict", "validator result must bind the stored upload and its verifier", 422);
    }
    const result = await c.env.DB.prepare(
      "UPDATE pipeline_submissions SET state=?,verdict=?,updated_at=?,lease_id=NULL,lease_until=NULL " +
      "WHERE id=? AND state='validating' AND lease_id=? AND lease_until>?",
    ).bind(String(value.state), JSON.stringify(value.verdict), Date.now(), row.id, c.req.header("X-Intake-Lease") ?? "", Date.now()).run();
    if (!result.meta.changes) return refuse(c, "lease", "validation lease is missing or expired", 409);
    return receipt(c, (await rowById(c, row.id))!);
  });
}
