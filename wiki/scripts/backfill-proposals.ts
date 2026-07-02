// One-shot blob→table backfill for the proposals lifecycle table (0009).
// Proposals stored in moderation_state.proposal BEFORE the table existed have
// no lifecycle rows, so /proposals and /stats can't see them and their
// decisions would be silent 0-row UPDATE no-ops. INSERT OR IGNORE is
// idempotent — safe to re-run any time.
//
//   node --experimental-strip-types scripts/backfill-proposals.ts [--remote]
import { execFileSync } from "node:child_process";

const remote = process.argv.includes("--remote") ? "--remote" : "--local";

// Deterministic JSON (recursively sorted keys) — keep in lockstep with
// src/proposals.ts stable()/fieldsSig().
function stable(v: unknown): string {
  if (v === null || typeof v !== "object") return JSON.stringify(v) ?? "null";
  if (Array.isArray(v)) return "[" + v.map(stable).join(",") + "]";
  const o = v as Record<string, unknown>;
  return "{" + Object.keys(o).sort().map((k) => JSON.stringify(k) + ":" + stable(o[k])).join(",") + "}";
}

function d1(sqlText: string): unknown[] {
  const out = execFileSync(
    "npx",
    ["wrangler", "d1", "execute", "wikilean", remote, "--json", "--command", sqlText],
    { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 },
  );
  return (JSON.parse(out)[0]?.results ?? []) as unknown[];
}

const esc = (s: string) => s.replaceAll("'", "''");

const rows = d1("SELECT slug, proposal FROM moderation_state WHERE proposal IS NOT NULL") as Array<{
  slug: string;
  proposal: string;
}>;
let inserted = 0;
for (const row of rows) {
  let pending: Array<Record<string, unknown>> = [];
  try {
    const v = JSON.parse(row.proposal);
    if (Array.isArray(v)) pending = v;
  } catch {
    continue;
  }
  for (const p of pending) {
    if (typeof p.proposalId !== "string" || typeof p.annotationId !== "string") continue;
    const fields = p.fields && typeof p.fields === "object" ? p.fields : {};
    const cols =
      `'${esc(p.proposalId)}','${esc(row.slug)}','${esc(p.annotationId)}',` +
      `'${esc(JSON.stringify(fields))}','${esc(stable(fields))}',` +
      `${typeof p.reason === "string" ? `'${esc(p.reason)}'` : "NULL"},` +
      `${typeof p.runId === "string" ? `'${esc(p.runId)}'` : "NULL"},` +
      `${typeof p.model === "string" ? `'${esc(p.model)}'` : "NULL"},` +
      `'pending',${typeof p.createdAt === "number" ? p.createdAt : Date.now()}`;
    d1(
      "INSERT OR IGNORE INTO proposals (id, slug, annotation_id, fields, fields_sig, reason, run_id, model, status, created_at) " +
        `VALUES (${cols})`,
    );
    inserted++;
  }
}
console.log(`backfill (${remote}): ${rows.length} blob(s) scanned, ${inserted} proposal row(s) upserted`);
