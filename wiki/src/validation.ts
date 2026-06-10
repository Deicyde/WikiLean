// Shared validation constants for annotation save/revert (P0 hardening).
// Exported so other code (engine, sibling features) imports the single source
// of truth instead of re-declaring the status vocabulary.

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
