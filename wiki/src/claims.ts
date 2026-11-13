// The claims API (docs/ROADMAP.md, outside contributors: Jack, 2026-11-11, "Build a claims
// API"). The coordinator publishes work packages, each one planned locate run whose bytes
// the operator uploaded to the private bucket CONTRIB (packages/v1/<envelope sha256>.json).
// An outside contributor's agent claims one, downloads it, runs it on its own machine,
// submits the sealed results through intake (submissions.ts) and links each receipt to its
// claim, so the coordinator collects a run's results from one view.
//
//   GET  /api/packages                    the open packages (agent)
//   POST /api/claims                      claim the oldest free package, or a named one (agent)
//   GET  /api/claims/<id>/package         the claimed package's bytes (its agent)
//   POST /api/claims/<id>/renew           extend the lease (its agent)
//   POST /api/claims/<id>/submissions     link an intake receipt to the claim (its agent)
//   POST /api/claims/<id>/release         end the claim, done or not (its agent)
//   POST /api/packages                    publish an uploaded package (pipeline)
//   POST /api/packages/<run>/state        open, done or retired; ends a live claim (pipeline)
//   GET  /api/packages/<run>/claims       a package's claims and their receipts (pipeline)
//
// A claim is a lease of CLAIM_LEASE_MS that the client renews as it works, for at most
// CLAIM_MAX_LIFE_MS from the claim. One live claim per package (a partial unique index), and
// per owner, all of their agents together, CLAIMS_LIVE_PER_OWNER live and
// CLAIMS_DAILY_PER_OWNER new a UTC day. Expiry is lazy: every claim first ends the claims
// whose lease lapsed or whose agent lost access. Controls that need no deploy: the claims
// lever (migration 0023 seeds it 'closed'), the brake, a package's state, revoking the token
// at /agents (which ends its claims) or blocking its owner. The Worker never writes the
// bucket and never parses a package: the client checks the bytes against envelope_sha256.
import type { Context, Hono } from "hono";
import type { Env } from "./env.js";
import { getUser, type AuthUser } from "./auth.js";
import { editLimitKey } from "./agents.js";
import { agreementStands, CC0_ACCEPTED_VERSIONS, loadAgreement } from "./articles-overlay.js";
import { checkOrigin, readJsonCapped, MAX_JSON_BODY_BYTES } from "./security.js";
import { leverOpen, writeBrake } from "./site-settings.js";
import { refuse, requirePipeline, SUBMISSION_DAILY_COUNT } from "./submissions.js";

type Ctx = Context<{ Bindings: Env }>;
type Agent = AuthUser & { ownerId: string };
export const CLAIMS_LEVER = "claims";
export const PACKAGE_PREFIX = "packages/v1/";
export const PACKAGE_MAX_BYTES = 64 * 1024 * 1024;
export const CLAIM_LEASE_MS = 24 * 3600 * 1000;
export const CLAIM_MAX_LIFE_MS = 7 * 24 * 3600 * 1000;
export const CLAIMS_LIVE_PER_OWNER = 2;
export const CLAIMS_DAILY_PER_OWNER = 10;
// A claim's receipts: as many as intake takes from one owner in a day.
export const CLAIM_LINKS_MAX = SUBMISSION_DAILY_COUNT;
export const RELEASE_REASONS: readonly string[] = ["done", "sources_mismatch", "gave_up", "other"];
const PACKAGE_STATES: readonly string[] = ["open", "done", "retired"];
const PACKAGES_LISTED = 500;
const NOTE_MAX = 500;
const DAY_MS = 86400000;
const SHA = /^[0-9a-f]{64}$/;
const CLAIM_ID = /^clm-[0-9a-f]{32}$/;
const REQUEST_KEY = /^[0-9a-f]{32}$/;
const SUBMISSION_ID = /^sub-[0-9a-f]{64}$/;
// scripts/pipeline/vocab.py RUN_ID. A package's campaign is vocab.CAMPAIGN_RE's run: form
// naming the package's own run: run:<run>, or run:<run>+<challenge run>.
const RUN = "[A-Za-z0-9][A-Za-z0-9._-]{0,63}";
const RUN_ID = new RegExp(`^${RUN}$`);
const CHALLENGE_SUFFIX = new RegExp(`^\\+${RUN}$`);
const MODE = /^[a-z][a-z0-9_-]{0,31}$/;
// Re-checked inside the statements: the brake, read as writeBrake reads it, and the access
// of a claim's agent (a live token, neither the agent nor its owner blocked).
const BRAKE_OPEN = "COALESCE((SELECT value FROM site_settings WHERE key='site_write_mode'),'open')='open'";
const ACCESS = "SELECT 1 FROM agent_tokens t JOIN users a ON a.id=t.agent_id JOIN users o ON o.id=t.owner_id " +
  "WHERE t.agent_id=contrib_claims.agent_id AND t.revoked_at IS NULL AND a.role='agent' AND o.role!='blocked'";
const CLAIM_SHAPE = 'send {"request_key": <32 lowercase hex>, "run_id"?: <run id>}';
const RELEASE_SHAPE = `send {"reason": ${RELEASE_REASONS.join("|")}, "note"?: <at most ${NOTE_MAX} characters>}`;
const LINK_SHAPE = 'send {"submission_id": "sub-<64 hex>"}';
const STATE_SHAPE = `send {"state": ${PACKAGE_STATES.join("|")}}`;
const PLAN_KEYS = ["run_id", "envelope_sha256", "bytes", "bundle_sha256", "universe_sha256", "index_sha256",
  "campaign", "mode", "tasks", "sources", "max_cost"];

interface PackageRow {
  run_id: string; envelope_sha256: string; bytes: number; bundle_sha256: string; universe_sha256: string;
  index_sha256: string | null; campaign: string; mode: string; tasks: number; sources: number;
  max_cost: number | null; state: string; published_at: number; updated_at: number;
}
type Plan = Omit<PackageRow, "state" | "published_at" | "updated_at">;
interface ClaimRow {
  id: string; run_id: string; agent_id: string; owner_id: string; request_key: string; state: string;
  claimed_at: number; renewed_at: number; lease_until: number; ended_at: number | null;
  end_reason: string | null; note: string | null;
}
interface Own { claim: ClaimRow; pkg: PackageRow }

function object(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function onlyKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).every(key => keys.includes(key));
}
async function first<T>(c: Ctx, sql: string, ...values: unknown[]): Promise<T | null> {
  const { results } = await c.env.DB.prepare(sql).bind(...values).all<T>();
  return results[0] ?? null;
}

// The fields of a POST /api/packages body, or why they are refused.
export function parsePackage(value: unknown): Plan | string {
  if (!object(value) || Object.keys(value).length !== PLAN_KEYS.length || !onlyKeys(value, PLAN_KEYS)) {
    return `send exactly ${PLAN_KEYS.join(", ")}`;
  }
  const { run_id, envelope_sha256, bytes, bundle_sha256, universe_sha256, index_sha256, campaign, mode,
    tasks, sources, max_cost } = value;
  if (typeof run_id !== "string" || !RUN_ID.test(run_id)) return "run_id is not a pipeline run id";
  for (const [key, sha] of Object.entries({ envelope_sha256, bundle_sha256, universe_sha256 })) {
    if (typeof sha !== "string" || !SHA.test(sha)) return `${key} is not 64 lowercase hex`;
  }
  const index = typeof index_sha256 === "string" && SHA.test(index_sha256) ? index_sha256 : null;
  if (index_sha256 !== index) return "index_sha256 is neither null nor 64 lowercase hex";
  if (typeof bytes !== "number" || !Number.isSafeInteger(bytes) || bytes < 1 || bytes > PACKAGE_MAX_BYTES) {
    return `bytes must be between 1 and ${PACKAGE_MAX_BYTES}`;
  }
  const own = `run:${run_id}`;
  if (typeof campaign !== "string" || !campaign.startsWith(own) ||
      !(campaign === own || CHALLENGE_SUFFIX.test(campaign.slice(own.length)))) {
    return "campaign must be run:<run id> or run:<run id>+<challenge run id>";
  }
  if (typeof mode !== "string" || !MODE.test(mode)) return "mode is not a package mode";
  for (const [key, count] of Object.entries({ tasks, sources })) {
    if (typeof count !== "number" || !Number.isSafeInteger(count) || count < 1) return `${key} must be a positive count`;
  }
  const cost = typeof max_cost === "number" && Number.isFinite(max_cost) && max_cost >= 0 ? max_cost : null;
  if (max_cost !== cost) return "max_cost is neither null nor a non-negative number";
  return { run_id, envelope_sha256: envelope_sha256 as string, bytes, bundle_sha256: bundle_sha256 as string,
    universe_sha256: universe_sha256 as string, index_sha256: index, campaign, mode, tasks: tasks as number,
    sources: sources as number, max_cost: cost };
}

function describePackage(row: PackageRow) {
  return {
    run_id: row.run_id, envelope_sha256: row.envelope_sha256, bytes: row.bytes, bundle_sha256: row.bundle_sha256,
    universe_sha256: row.universe_sha256, index_sha256: row.index_sha256, campaign: row.campaign, mode: row.mode,
    tasks: row.tasks, sources: row.sources, max_cost: row.max_cost, state: row.state, published_at: row.published_at,
  };
}

// A live claim whose lease lapsed reads as expired before a sweep has written it so.
function stateNow(claim: ClaimRow, now: number): string {
  return claim.state === "live" && claim.lease_until <= now ? "expired" : claim.state;
}

function describeClaim({ claim, pkg }: Own, now: number) {
  return {
    id: claim.id, run_id: claim.run_id, agent_id: claim.agent_id, state: stateNow(claim, now),
    claimed_at: claim.claimed_at, renewed_at: claim.renewed_at, lease_until: claim.lease_until,
    expires_at: claim.claimed_at + CLAIM_MAX_LIFE_MS, ended_at: claim.ended_at, end_reason: claim.end_reason,
    package: describePackage(pkg),
    package_url: `/api/claims/${claim.id}/package`,
    bundle_url: `/api/index/bundles/${pkg.bundle_sha256}`,
  };
}

function packageRow(c: Ctx, run: string): Promise<PackageRow | null> {
  return first<PackageRow>(c, "SELECT * FROM contrib_packages WHERE run_id = ?", run);
}

async function withPackage(c: Ctx, claim: ClaimRow | null): Promise<Own | null> {
  return claim ? { claim, pkg: (await packageRow(c, claim.run_id))! } : null;
}

// The caller's own claim, or null: another agent's claim is not found.
async function ownClaim(c: Ctx, agent: Agent, id: string): Promise<Own | null> {
  if (!CLAIM_ID.test(id)) return null;
  return withPackage(c, await first<ClaimRow>(c, "SELECT * FROM contrib_claims WHERE id = ? AND agent_id = ?", id, agent.id));
}

// Why a claim can no longer be worked on, or null while it can.
function leaseProblem(c: Ctx, { claim, pkg }: Own, now: number): Response | null {
  if (claim.state !== "live") return refuse(c, "lease", `the claim has ended (${claim.state})`, 409);
  if (claim.lease_until <= now) {
    return refuse(c, "claim_expired", "the claim's lease ran out; claim the package again if it is still open", 409);
  }
  if (pkg.state !== "open") return refuse(c, "lease", `the package is ${pkg.state}`, 409);
  return null;
}

function notFound(c: Ctx) {
  return refuse(c, "not_found", "this agent holds no claim with that id", 404);
}
function unconfigured(c: Ctx) {
  return refuse(c, "not_configured", "work packages are not configured on this deployment", 503);
}
function closed(c: Ctx) {
  return refuse(c, "claims_closed", "claims are closed; a maintainer opens them without a deploy", 503);
}
function coordinator(c: Ctx): Promise<AuthUser | Response> {
  return requirePipeline(c, "coordinator_required", "coordinator");
}

async function ownedAgent(c: Ctx): Promise<Agent | Response> {
  const user = await getUser(c);
  if (!user) return refuse(c, "authentication", "an agent token is required", 401);
  if (user.role !== "agent" || !user.ownerId) return refuse(c, "agent_required", "claim with an owned agent token", 403);
  return { ...user, ownerId: user.ownerId };
}

// A read by an owned agent, under its owner's read budget.
async function agentRead(c: Ctx): Promise<Agent | Response> {
  const user = await ownedAgent(c); if (user instanceof Response) return user;
  if (!(await (c.env.MCP_LIMITER ?? c.env.EDIT_LIMITER).limit({ key: `claims-read:${user.ownerId}` })).success) {
    c.header("Retry-After", "60");
    return refuse(c, "rate_limit", "claims read limit reached", 429);
  }
  return user;
}

// A write by an owned agent: same origin, the brake, then its owner's write budget.
async function agentWrite(c: Ctx): Promise<Agent | Response> {
  const origin = checkOrigin(c); if (origin) return origin;
  const user = await ownedAgent(c); if (user instanceof Response) return user;
  const brake = await writeBrake(c); if (brake) return brake;
  if (!(await c.env.EDIT_LIMITER.limit({ key: editLimitKey(user) })).success) {
    c.header("Retry-After", "60");
    return refuse(c, "rate_limit", "claims write limit reached", 429);
  }
  return user;
}

// A request_key already used answers with its claim, unless the request names another package.
function replay(c: Ctx, own: Own, run: string | null, now: number) {
  if (run !== null && own.claim.run_id !== run) {
    return refuse(c, "idempotency_conflict", "this request_key already claimed another package", 409);
  }
  return c.json({ ok: true, claim: describeClaim(own, now) }, 200);
}

export function registerClaimRoutes(app: Hono<{ Bindings: Env }>): void {
  // JSON answers are never cached; a package download sets its own `private, no-store`.
  for (const path of ["/api/packages*", "/api/claims*"]) {
    app.use(path, async (c, next) => {
      await next();
      if (!c.res.headers.has("Cache-Control")) c.res.headers.set("Cache-Control", "no-store");
    });
  }

  app.get("/api/packages", async c => {
    const user = await agentRead(c); if (user instanceof Response) return user;
    const { results } = await c.env.DB.prepare(
      "SELECT p.*, NOT EXISTS (SELECT 1 FROM contrib_claims k WHERE k.run_id=p.run_id AND k.state='live' " +
      "AND k.lease_until>?) AS available FROM contrib_packages p WHERE p.state='open' " +
      "ORDER BY p.published_at, p.run_id LIMIT ?",
    ).bind(Date.now(), PACKAGES_LISTED).all<PackageRow & { available: number }>();
    return c.json({ ok: true, configured: !!c.env.CONTRIB, claims_open: await leverOpen(c.env, CLAIMS_LEVER),
      packages: results.map(row => ({ ...describePackage(row), available: !!row.available })) });
  });

  app.post("/api/claims", async c => {
    const user = await agentWrite(c); if (user instanceof Response) return user;
    if (!await leverOpen(c.env, CLAIMS_LEVER)) return closed(c);
    if (!c.env.CONTRIB) return unconfigured(c);
    if (!agreementStands(await loadAgreement(c.env, user.ownerId))) {
      return refuse(c, "agreement_required", "the agent owner must accept the CC0 contribution agreement at /agents first", 403);
    }
    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.claims, CLAIM_SHAPE);
    if (!read.ok) return read.response;
    const body = read.value;
    if (!object(body) || !onlyKeys(body, ["request_key", "run_id"])) return refuse(c, "invalid", CLAIM_SHAPE, 422);
    const key = body.request_key;
    const run = typeof body.run_id === "string" && RUN_ID.test(body.run_id) ? body.run_id : null;
    if (typeof key !== "string" || !REQUEST_KEY.test(key) || (body.run_id ?? null) !== run) {
      return refuse(c, "invalid", CLAIM_SHAPE, 422);
    }
    const byKey = async () => withPackage(c,
      await first<ClaimRow>(c, "SELECT * FROM contrib_claims WHERE agent_id = ? AND request_key = ?", user.id, key));
    const now = Date.now();
    const earlier = await byKey();
    if (earlier) return replay(c, earlier, run, now);

    const day = Math.floor(now / DAY_MS) * DAY_MS;
    const id = "clm-" + Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0")).join("");
    // One transaction: end lapsed claims and those whose agent lost access, then take the
    // oldest open package nobody holds, re-checking the caller's access, the brake, the
    // lever and the owner's caps inside the insert.
    const [expired, lost] = await c.env.DB.batch([
      c.env.DB.prepare(
        `UPDATE contrib_claims SET state='expired', ended_at=?, end_reason='expired' WHERE state='live' AND lease_until<=? AND ${BRAKE_OPEN}`,
      ).bind(now, now),
      c.env.DB.prepare(
        `UPDATE contrib_claims SET state='revoked', ended_at=?, end_reason='access_lost' WHERE state='live' AND NOT EXISTS (${ACCESS}) AND ${BRAKE_OPEN}`,
      ).bind(now),
      c.env.DB.prepare(
        "INSERT INTO contrib_claims (id,run_id,agent_id,owner_id,request_key,state,claimed_at,renewed_at,lease_until) " +
        "SELECT ?,p.run_id,?,?,?,'live',?,?,? FROM contrib_packages p WHERE p.state='open' AND (? IS NULL OR p.run_id=?) " +
        "AND NOT EXISTS (SELECT 1 FROM contrib_claims k WHERE k.run_id=p.run_id AND k.state='live') " +
        "AND EXISTS (SELECT 1 FROM agent_tokens t JOIN users a ON a.id=t.agent_id JOIN users o ON o.id=t.owner_id " +
        "JOIN user_agreements g ON g.user_id=o.id WHERE t.agent_id=? AND t.owner_id=? AND t.revoked_at IS NULL " +
        `AND a.role='agent' AND o.role!='blocked' AND g.cc0_version IN (${CC0_ACCEPTED_VERSIONS.map(() => "?").join(",")})) ` +
        `AND ${BRAKE_OPEN} AND (SELECT value FROM site_settings WHERE key=?)='open' ` +
        "AND (SELECT COUNT(*) FROM contrib_claims WHERE owner_id=? AND state='live') < ? " +
        "AND (SELECT COUNT(*) FROM contrib_claims WHERE owner_id=? AND claimed_at>=?) < ? " +
        "ORDER BY p.published_at, p.run_id LIMIT 1 ON CONFLICT DO NOTHING",
      ).bind(id, user.id, user.ownerId, key, now, now, now + CLAIM_LEASE_MS, run, run,
        user.id, user.ownerId, ...CC0_ACCEPTED_VERSIONS, CLAIMS_LEVER,
        user.ownerId, CLAIMS_LIVE_PER_OWNER, user.ownerId, day, CLAIMS_DAILY_PER_OWNER),
    ]);
    if (expired.meta.changes || lost.meta.changes) {
      console.log(JSON.stringify({ event: "claims-swept", expired: expired.meta.changes, access_lost: lost.meta.changes }));
    }
    const mine = await byKey();
    if (mine) {
      // Not this request's row: a concurrent one with the same key wrote it.
      if (mine.claim.id !== id) return replay(c, mine, run, now);
      console.log(JSON.stringify({ event: "claim", claim_id: id, run_id: mine.claim.run_id, agent_id: user.id,
        owner_id: user.ownerId }));
      return c.json({ ok: true, claim: describeClaim(mine, now) }, 201);
    }
    const brake = await writeBrake(c); if (brake) return brake;
    if (!await leverOpen(c.env, CLAIMS_LEVER)) return closed(c);
    if (!(await getUser(c)) || !agreementStands(await loadAgreement(c.env, user.ownerId))) {
      return refuse(c, "access_changed", "contributor access changed during the request", 403);
    }
    if (run !== null) {
      if ((await packageRow(c, run))?.state !== "open") return refuse(c, "not_found", "no open package has that run id", 404);
      if (await first(c, "SELECT 1 FROM contrib_claims WHERE run_id = ? AND state = 'live'", run)) {
        return refuse(c, "claimed", "another agent holds that package; claim another, or wait for its lease to end", 409);
      }
    }
    const usage = (await first<{ live: number; today: number }>(c,
      "SELECT COUNT(CASE WHEN state='live' THEN 1 END) AS live, COUNT(CASE WHEN claimed_at>=? THEN 1 END) AS today " +
      "FROM contrib_claims WHERE owner_id=?", day, user.ownerId))!;
    if (usage.live >= CLAIMS_LIVE_PER_OWNER) {
      return refuse(c, "claim_limit", `this account holds ${CLAIMS_LIVE_PER_OWNER} live claims; finish or release one first`, 409);
    }
    if (usage.today >= CLAIMS_DAILY_PER_OWNER) {
      c.header("Retry-After", String(Math.max(60, Math.ceil((day + DAY_MS - now) / 1000))));
      return refuse(c, "claim_quota", `this account made ${CLAIMS_DAILY_PER_OWNER} claims today; claim again after the indicated delay`, 429);
    }
    return c.json({ ok: true, claim: null });
  });

  app.get("/api/claims/:id/package", async c => {
    const user = await agentRead(c); if (user instanceof Response) return user;
    const bucket = c.env.CONTRIB;
    if (!bucket) return unconfigured(c);
    const own = await ownClaim(c, user, c.req.param("id"));
    if (!own) return notFound(c);
    const lapse = leaseProblem(c, own, Date.now()); if (lapse) return lapse;
    if (!agreementStands(await loadAgreement(c.env, user.ownerId))) {
      return refuse(c, "agreement_required", "the agent owner must accept the CC0 contribution agreement at /agents first", 403);
    }
    const sha = own.pkg.envelope_sha256;
    const headers = new Headers({
      "Content-Type": "application/json", "Content-Length": String(own.pkg.bytes), "Content-Digest": sha,
      "ETag": `"${sha}"`, "Cache-Control": "private, no-store",
    });
    // A HEAD request reaches this handler too, and is answered from D1 alone.
    if (c.req.method === "HEAD") return new Response(null, { headers });
    const stored = await bucket.get(`${PACKAGE_PREFIX}${sha}.json`);
    if (!stored || !("body" in stored)) {
      console.log(JSON.stringify({ event: "package-missing", run_id: own.pkg.run_id, envelope_sha256: sha }));
      return refuse(c, "package_missing", "the bucket does not hold this package; a maintainer must re-upload it", 503);
    }
    return new Response(stored.body, { headers });
  });

  app.post("/api/claims/:id/renew", async c => {
    const user = await agentWrite(c); if (user instanceof Response) return user;
    const id = c.req.param("id");
    const now = Date.now();
    const result = await c.env.DB.prepare(
      "UPDATE contrib_claims SET renewed_at=?, lease_until=MIN(?, claimed_at+?) " +
      "WHERE id=? AND agent_id=? AND state='live' AND lease_until>? AND claimed_at+?>? " +
      `AND EXISTS (${ACCESS}) ` +
      "AND EXISTS (SELECT 1 FROM contrib_packages p WHERE p.run_id=contrib_claims.run_id AND p.state='open') " +
      `AND ${BRAKE_OPEN}`,
    ).bind(now, now + CLAIM_LEASE_MS, CLAIM_MAX_LIFE_MS, id, user.id, now, CLAIM_MAX_LIFE_MS, now).run();
    const own = await ownClaim(c, user, id);
    if (!own) return notFound(c);
    if (!result.meta.changes) {
      const brake = await writeBrake(c); if (brake) return brake;
      const lapse = leaseProblem(c, own, now); if (lapse) return lapse;
      return refuse(c, "access_changed", "contributor access changed during the request", 403);
    }
    return c.json({ ok: true, claim: describeClaim(own, now) });
  });

  app.post("/api/claims/:id/submissions", async c => {
    const user = await agentWrite(c); if (user instanceof Response) return user;
    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.claims, LINK_SHAPE);
    if (!read.ok) return read.response;
    const body = read.value;
    const sub = object(body) ? body.submission_id : undefined;
    if (!object(body) || !onlyKeys(body, ["submission_id"]) || typeof sub !== "string" || !SUBMISSION_ID.test(sub)) {
      return refuse(c, "invalid", LINK_SHAPE, 422);
    }
    const id = c.req.param("id");
    const now = Date.now();
    // The receipt must be the same agent's, and the claim live; a receipt joins one claim.
    const result = await c.env.DB.prepare(
      "INSERT INTO contrib_claim_submissions (submission_id,claim_id,linked_at) " +
      "SELECT s.id,k.id,? FROM contrib_claims k JOIN pipeline_submissions s ON s.id=? AND s.agent_id=k.agent_id " +
      "WHERE k.id=? AND k.agent_id=? AND k.state='live' AND k.lease_until>? " +
      "AND (SELECT COUNT(*) FROM contrib_claim_submissions WHERE claim_id=?) < ? " +
      `AND ${BRAKE_OPEN} ON CONFLICT(submission_id) DO NOTHING`,
    ).bind(now, sub, id, user.id, now, id, CLAIM_LINKS_MAX).run();
    const own = await ownClaim(c, user, id);
    if (!own) return notFound(c);
    const link = await first<{ submission_id: string; claim_id: string; linked_at: number }>(c,
      "SELECT submission_id, claim_id, linked_at FROM contrib_claim_submissions WHERE submission_id = ?", sub);
    if (link?.claim_id === own.claim.id) {
      if (result.meta.changes) {
        console.log(JSON.stringify({ event: "claim-link", claim_id: id, run_id: own.claim.run_id, submission_id: sub,
          agent_id: user.id, owner_id: user.ownerId }));
      }
      return c.json({ ok: true, link });
    }
    if (link) return refuse(c, "linked_elsewhere", "that submission is linked to another claim", 409);
    const brake = await writeBrake(c); if (brake) return brake;
    const lapse = leaseProblem(c, own, now); if (lapse) return lapse;
    if (!await first(c, "SELECT 1 FROM pipeline_submissions WHERE id = ? AND agent_id = ?", sub, user.id)) {
      return refuse(c, "not_found", "this agent made no submission with that id", 404);
    }
    return refuse(c, "link_limit", `a claim takes at most ${CLAIM_LINKS_MAX} submissions`, 409);
  });

  app.post("/api/claims/:id/release", async c => {
    const user = await agentWrite(c); if (user instanceof Response) return user;
    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.claims, RELEASE_SHAPE);
    if (!read.ok) return read.response;
    const body = read.value;
    const reason = object(body) ? body.reason : undefined;
    const note = object(body) ? body.note ?? null : null;
    if (!object(body) || !onlyKeys(body, ["reason", "note"]) || typeof reason !== "string" || !RELEASE_REASONS.includes(reason) ||
        (note !== null && (typeof note !== "string" || note.length > NOTE_MAX))) {
      return refuse(c, "invalid", RELEASE_SHAPE, 422);
    }
    const id = c.req.param("id");
    const state = reason === "done" ? "completed" : "released";
    const now = Date.now();
    // Done means at least one receipt is linked; any other reason just ends the claim.
    const result = await c.env.DB.prepare(
      "UPDATE contrib_claims SET state=?, ended_at=?, end_reason=?, note=? WHERE id=? AND agent_id=? AND state='live' " +
      "AND (?=0 OR EXISTS (SELECT 1 FROM contrib_claim_submissions WHERE claim_id=contrib_claims.id)) " +
      `AND ${BRAKE_OPEN}`,
    ).bind(state, now, reason, note, id, user.id, reason === "done" ? 1 : 0).run();
    const own = await ownClaim(c, user, id);
    if (!own) return notFound(c);
    if (!result.meta.changes) {
      const brake = await writeBrake(c); if (brake) return brake;
      // A repeated release is answered as the first was.
      if (own.claim.state === state && own.claim.end_reason === reason) return c.json({ ok: true, claim: describeClaim(own, now) });
      if (own.claim.state !== "live") return refuse(c, "lease", `the claim has ended (${own.claim.state})`, 409);
      return refuse(c, "no_submission", "a claim is done once an intake receipt is linked to it; link one, or release it with another reason", 409);
    }
    console.log(JSON.stringify({ event: "claim-release", claim_id: id, run_id: own.claim.run_id, reason,
      agent_id: user.id, owner_id: user.ownerId }));
    return c.json({ ok: true, claim: describeClaim(own, now) });
  });

  app.post("/api/packages", async c => {
    const origin = checkOrigin(c); if (origin) return origin;
    const user = await coordinator(c); if (user instanceof Response) return user;
    const brake = await writeBrake(c); if (brake) return brake;
    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.contribPackage, `send ${PLAN_KEYS.join(", ")}`);
    if (!read.ok) return read.response;
    const plan = parsePackage(read.value);
    if (typeof plan === "string") return refuse(c, "invalid", plan, 422);
    const bucket = c.env.CONTRIB;
    if (!bucket) return unconfigured(c);
    const stored = await bucket.head(`${PACKAGE_PREFIX}${plan.envelope_sha256}.json`);
    if (stored?.size !== plan.bytes) {
      return refuse(c, "package_missing", "the bucket holds no package of that size under that sha; upload it first", 422);
    }
    const bundle = await first<{ universe_sha256: string; index_sha256: string; state: string }>(c,
      "SELECT universe_sha256, index_sha256, state FROM index_bundles WHERE manifest_sha256 = ?", plan.bundle_sha256);
    if (bundle?.state !== "live" || bundle.universe_sha256 !== plan.universe_sha256 ||
        (plan.index_sha256 !== null && plan.index_sha256 !== bundle.index_sha256)) {
      return refuse(c, "bundle_mismatch", "the package names no live index bundle of its universe and index", 422);
    }
    const now = Date.now();
    // Published once, never replaced: a second publication of a run id only repeats the first.
    const result = await c.env.DB.prepare(
      "INSERT INTO contrib_packages (run_id,envelope_sha256,bytes,bundle_sha256,universe_sha256,index_sha256,campaign," +
      "mode,tasks,sources,max_cost,state,published_at,updated_at) SELECT ?,?,?,?,?,?,?,?,?,?,?,'open',?,? " +
      `WHERE EXISTS (SELECT 1 FROM index_bundles WHERE manifest_sha256=? AND state='live') AND ${BRAKE_OPEN} ` +
      "ON CONFLICT DO NOTHING",
    ).bind(plan.run_id, plan.envelope_sha256, plan.bytes, plan.bundle_sha256, plan.universe_sha256, plan.index_sha256,
      plan.campaign, plan.mode, plan.tasks, plan.sources, plan.max_cost, now, now, plan.bundle_sha256).run();
    const row = await packageRow(c, plan.run_id);
    if (row && result.meta.changes) {
      console.log(JSON.stringify({ event: "package-publish", run_id: row.run_id, envelope_sha256: row.envelope_sha256,
        bytes: row.bytes, bundle_sha256: row.bundle_sha256 }));
      return c.json({ ok: true, package: describePackage(row) }, 201);
    }
    if (row && (Object.keys(plan) as Array<keyof Plan>).every(key => row[key] === plan[key])) {
      return c.json({ ok: true, package: describePackage(row) }, 200);
    }
    const stopped = await writeBrake(c); if (stopped) return stopped;
    if (row || await first(c, "SELECT 1 FROM contrib_packages WHERE envelope_sha256 = ?", plan.envelope_sha256)) {
      return refuse(c, "exists", "a package with that run id or envelope is published with other values; packages are never replaced", 409);
    }
    return refuse(c, "bundle_mismatch", "the package's index bundle was retired during the request", 422);
  });

  app.post("/api/packages/:run_id/state", async c => {
    const origin = checkOrigin(c); if (origin) return origin;
    const user = await coordinator(c); if (user instanceof Response) return user;
    const brake = await writeBrake(c); if (brake) return brake;
    const read = await readJsonCapped(c, MAX_JSON_BODY_BYTES.claims, STATE_SHAPE);
    if (!read.ok) return read.response;
    const body = read.value;
    const state = object(body) ? body.state : undefined;
    if (!object(body) || !onlyKeys(body, ["state"]) || typeof state !== "string" || !PACKAGE_STATES.includes(state)) {
      return refuse(c, "invalid", STATE_SHAPE, 422);
    }
    const run = c.req.param("run_id");
    if (!RUN_ID.test(run) || !await packageRow(c, run)) return refuse(c, "not_found", "no package has that run id", 404);
    const now = Date.now();
    // Any change, re-opening an open package included, ends its live claim: that is how the
    // coordinator frees a package from a claimant who stopped.
    const [moved, ended] = await c.env.DB.batch([
      c.env.DB.prepare(`UPDATE contrib_packages SET state=?, updated_at=? WHERE run_id=? AND ${BRAKE_OPEN}`).bind(state, now, run),
      c.env.DB.prepare(
        `UPDATE contrib_claims SET state='revoked', ended_at=?, end_reason='coordinator' WHERE run_id=? AND state='live' AND ${BRAKE_OPEN}`,
      ).bind(now, run),
    ]);
    if (!moved.meta.changes) {
      const stopped = await writeBrake(c); if (stopped) return stopped;
    }
    const row = (await packageRow(c, run))!;
    console.log(JSON.stringify({ event: "package-state", run_id: run, state: row.state, ended_claims: ended.meta.changes }));
    return c.json({ ok: true, package: describePackage(row), ended_claims: ended.meta.changes });
  });

  app.get("/api/packages/:run_id/claims", async c => {
    const user = await coordinator(c); if (user instanceof Response) return user;
    const run = c.req.param("run_id");
    const pkg = RUN_ID.test(run) ? await packageRow(c, run) : null;
    if (!pkg) return refuse(c, "not_found", "no package has that run id", 404);
    const now = Date.now();
    const { results: claims } = await c.env.DB.prepare(
      "SELECT * FROM contrib_claims WHERE run_id = ? ORDER BY claimed_at, id",
    ).bind(run).all<ClaimRow>();
    // A receipt intake no longer holds (an archive removed it) is listed with nulls.
    const { results: links } = await c.env.DB.prepare(
      "SELECT l.claim_id, l.submission_id AS id, l.linked_at, s.state, s.seal_sha256, s.body_sha256, s.received_at, s.verdict " +
      "FROM contrib_claim_submissions l JOIN contrib_claims k ON k.id=l.claim_id " +
      "LEFT JOIN pipeline_submissions s ON s.id=l.submission_id WHERE k.run_id=? ORDER BY l.linked_at, l.submission_id",
    ).bind(run).all<{ claim_id: string; id: string; linked_at: number; state: string | null; seal_sha256: string | null;
      body_sha256: string | null; received_at: number | null; verdict: string | null }>();
    return c.json({ ok: true, package: describePackage(pkg), claims: claims.map(k => ({
      id: k.id, agent_id: k.agent_id, owner_id: k.owner_id, state: stateNow(k, now), claimed_at: k.claimed_at,
      renewed_at: k.renewed_at, lease_until: k.lease_until, ended_at: k.ended_at, end_reason: k.end_reason, note: k.note,
      submissions: links.filter(l => l.claim_id === k.id).map(l => ({
        id: l.id, linked_at: l.linked_at, state: l.state, seal_sha256: l.seal_sha256, body_sha256: l.body_sha256,
        received_at: l.received_at, verdict: l.verdict ? JSON.parse(l.verdict) : null,
      })),
    })) });
  });
}
