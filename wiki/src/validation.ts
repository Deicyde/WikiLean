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
// Mirrors site/batch_annotate.py _anchor_sig: a JSON array of [type, section,
// snippet, value, from] with absent fields as null (annotations lacking an
// anchor get the all-null signature). sort_keys is moot — entries are scalars.
export function anchorSig(a: AnnRecord): string {
  const anc = (typeof a.anchor === "object" && a.anchor !== null && !Array.isArray(a.anchor)
    ? a.anchor
    : {}) as AnnRecord;
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

// Provenance stamping for SESSION (human) saves: new or changed annotations
// get provenance='human' forced server-side; unchanged annotations keep their
// STORED provenance. "Changed" is judged with provenance excluded, so a bare
// provenance flip cannot launder 'ai' work into 'human' (or vice versa).
// Bot writes never come through here — their provenance passes verbatim.
export function stampProvenance(stored: AnnRecord[], posted: AnnRecord[]): AnnRecord[] {
  const strip = ({ provenance: _p, ...rest }: AnnRecord): AnnRecord => rest;
  return posted.map((p) => {
    const match = findMatch(p, stored);
    if (match && deepEqual(strip(p), strip(match))) {
      return { ...p, provenance: match.provenance };
    }
    return { ...p, provenance: "human" };
  });
}
