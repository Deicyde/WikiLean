// Shared validation constants + save-path annotation diff helpers (P0
// hardening, P1 pipeline loop). Exported so other code (engine, sibling
// features) imports the single source of truth instead of re-declaring the
// status vocabulary.

// Allowed annotation `status` values. `rejected` is reserved as a tombstone for
// a sibling feature and is intentionally allowed now to avoid re-touching the
// validator later.
export const ANNOTATION_STATUSES = ["formalized", "partial", "not_formalized", "rejected"] as const;
export type AnnotationStatus = (typeof ANNOTATION_STATUSES)[number];

// Length caps (chars). Identifier/enum-ish fields (kind, match_kind, decl,
// module) stay tight; free-text fields (label, note, proof_note) get a generous
// cap because real data already runs to ~500 chars (longest note in the corpus
// is 511). The 256 KB payload cap below is the actual abuse guard — per-field
// caps just need to bound single-field injection without rejecting real edits.
export const MAX_FIELD_LEN = 300;
export const MAX_TEXT_LEN = 2000;

// Payload caps.
export const MAX_ANNOTATIONS = 2000;
export const MAX_ANNOTATIONS_BYTES = 256 * 1024; // 256 KB serialized JSON
export const MAX_META_BYTES = 16 * 1024; // pipeline revisions.meta JSON (run stats, tiny in practice)
export const MAX_FIELD_CHANGES_BYTES = 4 * 1024; // annotation_events.field_changes JSON

// Allowed flag `reason` values (anonymous reader reports). Mirrors the CHECK
// constraint in migration 0005 — the enum is defined once here and imported
// by the endpoint validator.
export const FLAG_REASONS = ["wrong_decl", "wrong_status", "irrelevant", "missing_formalization", "other"] as const;
export type FlagReason = (typeof FLAG_REASONS)[number];
export const MAX_FLAG_COMMENT_LEN = 500;

// ---------------------------------------------------------------------------
// Annotation diff helpers for the save path (P1). Annotations are loosely
// typed records here — the save handler validates field shapes separately;
// these helpers only need identity (id / anchor signature) and equality.

export type AnnRecord = Record<string, unknown>;

function annId(a: AnnRecord): string | number | undefined {
  const id = a.id;
  if (typeof id === "string" && id !== "") return id;
  if (typeof id === "number") return id;
  return undefined;
}

// Stable signature of an annotation's anchor, for matching across passes.
// LOCKSTEP with site/batch_annotate.py _anchor_sig — the two implementations
// must agree exactly. A JSON array of [type, section, snippet, value, from]
// with absent fields as null; the five fields come from (TESTAGENT#1):
//   1. the singular `anchor` when it is a plain object (even an empty one —
//      it does NOT fall through to anchors[]);
//   2. else anchors[0] when `anchors` is a non-empty array whose first
//      element is a plain object (v3 multi-anchor annotations);
//   3. else the all-null signature.
// Non-object `anchor` values (string/number/array) must never crash — they
// fall through to the anchors[]-then-all-null path.
// sort_keys is moot — entries are scalars.
export function anchorSig(a: AnnRecord): string {
  let anc: AnnRecord = {};
  if (isPlainObject(a.anchor)) {
    anc = a.anchor;
  } else if (Array.isArray(a.anchors) && a.anchors.length > 0 && isPlainObject(a.anchors[0])) {
    anc = a.anchors[0];
  }
  return JSON.stringify([
    anc.type ?? null,
    anc.section ?? null,
    anc.snippet ?? null,
    anc.value ?? null,
    anc.from ?? null,
  ]);
}

// Key-order-insensitive deep equality over JSON-shaped values (everything here
// has been through JSON.parse, so no undefined/functions/cycles).
export function deepEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    return a.every((v, i) => deepEqual(v, b[i]));
  }
  const ka = Object.keys(a);
  const kb = Object.keys(b);
  if (ka.length !== kb.length) return false;
  return ka.every((k) => deepEqual((a as AnnRecord)[k], (b as AnnRecord)[k]));
}

// Find `target`'s counterpart in `pool`: by id when both sides have ids, else
// by anchor signature (a sig match is rejected only when both sides carry ids
// that disagree — ids, once present, are authoritative).
export function findMatch(target: AnnRecord, pool: AnnRecord[]): AnnRecord | undefined {
  const tid = annId(target);
  if (tid !== undefined) {
    const byId = pool.find((p) => annId(p) === tid);
    if (byId) return byId;
  }
  const sig = anchorSig(target);
  return pool.find((p) => {
    if (tid !== undefined && annId(p) !== undefined) return false;
    return anchorSig(p) === sig;
  });
}

// Server-side twin of batch_annotate.py _preserve_human, applied to BOT writes
// only: every stored provenance='human' annotation (INCLUDING tombstones,
// status='rejected') must appear in the posted array deep-equal to the stored
// form. Returns the violations (label when present, else anchor signature) —
// empty array = write is safe.
export function findLostHuman(stored: AnnRecord[], posted: AnnRecord[]): string[] {
  const missing: string[] = [];
  for (const h of stored) {
    if (h.provenance !== "human") continue;
    const match = findMatch(h, posted);
    if (!match || !deepEqual(h, match)) {
      missing.push(typeof h.label === "string" && h.label !== "" ? h.label : anchorSig(h));
    }
  }
  return missing;
}

// ---------------------------------------------------------------------------
// By-id annotation diff (annotation_events + the /:slug/diff page). One differ
// serves both consumers: the event emitter serializes `fields` into the
// field_changes JSON; the diff route hands `fields` to the page builder.

export interface AnnotationFieldChange {
  field: string; // top-level key, or a dotted path for nested objects ("mathlib.decl")
  from: unknown; // null = absent on that side
  to: unknown;
}

export interface AnnotationChange {
  annotationId: string;
  // 'reject' = status flipped to 'rejected' (a tombstone veto) — reported
  // instead of 'modify' so the agreement signal is queryable directly.
  changeType: "add" | "modify" | "delete" | "reject";
  label: string | null; // display label (to-side preferred), for the diff page
  fields: AnnotationFieldChange[]; // changed fields; [] for add/delete
}

function labelOf(a: AnnRecord): string | null {
  return typeof a.label === "string" && a.label !== "" ? a.label : null;
}

function isPlainObject(v: unknown): v is AnnRecord {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

// Field-level diff of one annotation pair. Top-level scalar/array fields are
// compared atomically; plain-object fields (mathlib, anchor, …) descend one
// level into dotted paths so e.g. only "mathlib.decl" is reported when the
// decl alone changed. A field that is an object on one side and a non-object
// scalar on the other is compared atomically under the bare key.
export function diffFields(from: AnnRecord, to: AnnRecord): AnnotationFieldChange[] {
  const out: AnnotationFieldChange[] = [];
  const keys = [...new Set([...Object.keys(from), ...Object.keys(to)])].sort();
  for (const key of keys) {
    const a = from[key];
    const b = to[key];
    if (deepEqual(a, b)) continue;
    const objecty =
      (isPlainObject(a) || a === undefined) && (isPlainObject(b) || b === undefined);
    if (objecty) {
      const ao = isPlainObject(a) ? a : {};
      const bo = isPlainObject(b) ? b : {};
      const sub = [...new Set([...Object.keys(ao), ...Object.keys(bo)])].sort();
      for (const sk of sub) {
        if (!deepEqual(ao[sk], bo[sk])) {
          out.push({ field: `${key}.${sk}`, from: ao[sk] ?? null, to: bo[sk] ?? null });
        }
      }
    } else {
      out.push({ field: key, from: a ?? null, to: b ?? null });
    }
  }
  return out;
}

// Diff two annotation snapshots BY ID (the persisted side has full id coverage
// — the save path heals ids before calling this). Entries without a string id
// are skipped defensively: they cannot key an annotation_events row, and in
// production every stored annotation carries an id. A status flip to
// 'rejected' is classified 'reject' (instead of 'modify'); the status field
// change still appears in `fields`.
export function diffAnnotations(stored: AnnRecord[], persisted: AnnRecord[]): AnnotationChange[] {
  const storedById = new Map<string, AnnRecord>();
  for (const s of stored) {
    if (typeof s.id === "string" && s.id !== "") storedById.set(s.id, s);
  }
  const changes: AnnotationChange[] = [];
  const seen = new Set<string>();
  for (const p of persisted) {
    if (typeof p.id !== "string" || p.id === "") continue;
    seen.add(p.id);
    const s = storedById.get(p.id);
    if (!s) {
      changes.push({ annotationId: p.id, changeType: "add", label: labelOf(p), fields: [] });
      continue;
    }
    if (deepEqual(s, p)) continue;
    const changeType = s.status !== "rejected" && p.status === "rejected" ? "reject" : "modify";
    changes.push({ annotationId: p.id, changeType, label: labelOf(p) ?? labelOf(s), fields: diffFields(s, p) });
  }
  for (const [id, s] of storedById) {
    if (!seen.has(id)) {
      changes.push({ annotationId: id, changeType: "delete", label: labelOf(s), fields: [] });
    }
  }
  return changes;
}

// Serialize a change's fields into the annotation_events.field_changes JSON:
// {field: [old, new]}, capped at MAX_FIELD_CHANGES_BYTES. Over the cap, fields
// are dropped (in field order) until the JSON fits and {"_truncated":true}
// flags the cut. Returns null for an empty field list (add/delete events).
export function serializeFieldChanges(fields: AnnotationFieldChange[]): string | null {
  if (fields.length === 0) return null;
  const obj: Record<string, unknown> = {};
  for (const f of fields) obj[f.field] = [f.from, f.to];
  const json = JSON.stringify(obj);
  if (json.length <= MAX_FIELD_CHANGES_BYTES) return json;
  const kept: Record<string, unknown> = { _truncated: true };
  for (const f of fields) {
    kept[f.field] = [f.from, f.to];
    if (JSON.stringify(kept).length > MAX_FIELD_CHANGES_BYTES) delete kept[f.field];
  }
  return JSON.stringify(kept);
}

// Provenance stamping for SESSION (human) saves: new or changed annotations
// get provenance='human' forced server-side; unchanged annotations keep their
// STORED provenance. "Changed" is judged with provenance AND id excluded
// (F8): id identity is findMatch's job, so a client that merely drops the id
// field cannot launder an unchanged 'ai' annotation into 'human' (and a bare
// provenance flip cannot launder in either direction).
// Bot writes never come through here — their provenance passes verbatim.
export function stampProvenance(stored: AnnRecord[], posted: AnnRecord[]): AnnRecord[] {
  const strip = ({ provenance: _p, id: _id, ...rest }: AnnRecord): AnnRecord => rest;
  return posted.map((p) => {
    const match = findMatch(p, stored);
    if (match && deepEqual(strip(p), strip(match))) {
      return { ...p, provenance: match.provenance };
    }
    return { ...p, provenance: "human" };
  });
}
