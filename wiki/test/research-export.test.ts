// GET /api/research/export.jsonl (P2a): the nightly pseudonymized research
// export. Pins the line shape (exact key set), pseudonym derivation/stability
// (sha256(user_id + PIPELINE_TOKEN) first 12 hex; null for pipeline), run_id
// extraction from revisions.meta, and the no-PII guarantee (no emails, names,
// raw user ids, or comments anywhere in the stream). Authz cells live in
// authz.test.ts.

import { createHash } from "node:crypto";
import { describe, it, expect } from "vitest";
import {
  setup,
  get,
  save,
  botSave,
  storedAnnotations,
  blockNetwork,
  echo,
  SLUG,
  PIPELINE_TOKEN,
  SEED_ANNOTATIONS,
  EXTRA_ANNOTATION,
  type Harness,
} from "./helpers/harness.js";

blockNetwork();

const EXPECTED_KEYS = [
  "annotation_id",
  "actor_type",
  "created_at",
  "event_id",
  "event_type",
  "field_changes",
  "pseudonym",
  "revision_id",
  "revision_kind",
  "run_id",
  "slug",
].sort();

const HUMAN_PSEUDONYM = createHash("sha256")
  .update("u-human" + PIPELINE_TOKEN)
  .digest("hex")
  .slice(0, 12);

interface ExportLine {
  event_id: number;
  slug: string;
  annotation_id: string;
  event_type: string;
  actor_type: string;
  pseudonym: string | null;
  field_changes: Record<string, unknown> | null;
  revision_id: number;
  revision_kind: string;
  run_id: string | null;
  created_at: number;
}

async function fetchExport(h: Harness): Promise<{ raw: string; lines: ExportLine[]; res: Response }> {
  const res = await get(h.env, "/api/research/export.jsonl", { bearer: PIPELINE_TOKEN, origin: null });
  const raw = await res.text();
  const lines = raw
    .split("\n")
    .filter((l) => l.length > 0)
    .map((l) => JSON.parse(l) as ExportLine);
  return { raw, lines, res };
}

// Three events across the real write paths: a human modify (v1→v2), a
// pipeline add with run_id meta (v2→v3), a human endorse (v3→v4). The human
// save carries a comment that must NOT surface in the export.
async function seedEvents(h: Harness): Promise<void> {
  const edited = echo(SEED_ANNOTATIONS);
  edited[0].label = "Abelian group (edited)";
  const r1 = await save(
    h.env,
    { annotations: edited, base_version: 1, comment: "private comment text" },
    { user: "u-human" },
  );
  expect(r1.status).toBe(200);

  const r2 = await botSave(h.env, {
    annotations: [...echo(storedAnnotations(h.db)), echo(EXTRA_ANNOTATION)],
    base_version: 2,
    meta: { run_id: "deadbeef", model: "claude-fable-5" },
  });
  expect(r2.status).toBe(200);

  const r3 = await save(
    h.env,
    { action: "endorse", annotation_id: "bbbbbbbbbbbb", base_version: 3 },
    { user: "u-human" },
  );
  expect(r3.status).toBe(200);
}

describe("GET /api/research/export.jsonl", () => {
  it("streams one JSONL line per event with exactly the contract keys, id-ascending", async () => {
    const h = setup();
    await seedEvents(h);
    const { lines, res } = await fetchExport(h);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toContain("application/x-ndjson");

    expect(lines.length).toBe(3); // modify + add + endorse
    for (const line of lines) {
      expect(Object.keys(line).sort()).toEqual(EXPECTED_KEYS);
      expect(line.slug).toBe(SLUG);
    }
    const ids = lines.map((l) => l.event_id);
    expect([...ids].sort((a, b) => a - b)).toEqual(ids);
    expect(lines.map((l) => l.event_type)).toEqual(["modify", "add", "endorse"]);
  });

  it("pseudonymizes humans (sha256(user_id+token) first 12 hex), null for pipeline, joins revision kind + run_id", async () => {
    const h = setup();
    await seedEvents(h);
    const { lines } = await fetchExport(h);
    const [modify, add, endorse] = lines;

    expect(modify.actor_type).toBe("human");
    expect(modify.pseudonym).toBe(HUMAN_PSEUDONYM);
    expect(modify.revision_kind).toBe("edit");
    expect(modify.run_id).toBeNull();
    // field_changes rides along as a parsed {field: [from, to]} object.
    expect(modify.field_changes).toMatchObject({ label: ["Abelian group", "Abelian group (edited)"] });

    expect(add.actor_type).toBe("pipeline");
    expect(add.pseudonym).toBeNull();
    expect(add.revision_kind).toBe("pipeline");
    expect(add.run_id).toBe("deadbeef"); // extracted from revisions.meta

    expect(endorse.event_type).toBe("endorse");
    // Same human, same pseudonym — the stability the survival analyses need.
    expect(endorse.pseudonym).toBe(HUMAN_PSEUDONYM);
    expect(endorse.field_changes).toMatchObject({ provenance: ["ai", "human"] });
  });

  it("pseudonyms are stable across export calls", async () => {
    const h = setup();
    await seedEvents(h);
    const first = await fetchExport(h);
    const second = await fetchExport(h);
    expect(second.raw).toBe(first.raw);
    expect(second.lines[0].pseudonym).toBe(HUMAN_PSEUDONYM);
  });

  it("carries NO PII: no emails, display names, raw user ids, or comments", async () => {
    const h = setup();
    await seedEvents(h);
    const { raw } = await fetchExport(h);
    expect(raw).not.toContain("human@example.org"); // email (users table)
    expect(raw).not.toContain("Human Tester"); // display name
    expect(raw).not.toContain("u-human"); // raw user id — pseudonym only
    expect(raw).not.toContain("private comment text"); // revision comment
    expect(raw).not.toContain('"user_id"'); // the column itself must not leak
  });

  it("admin sessions may export too (the other arm of the authz gate)", async () => {
    const h = setup();
    await seedEvents(h);
    const res = await get(h.env, "/api/research/export.jsonl", { user: "u-admin" });
    expect(res.status).toBe(200);
    const raw = await res.text();
    expect(raw.split("\n").filter((l) => l.length > 0).length).toBe(3);
  });

  it("an empty events table exports an empty (but valid) stream", async () => {
    const h = setup();
    const { raw, res } = await fetchExport(h);
    expect(res.status).toBe(200);
    expect(raw).toBe("");
  });
});
