// Annotation data model — mirrors the JSON sidecar schema consumed by render.py.
// Supports v1/v2 (flat decl/module/note) and v3 (nested `mathlib`, kind/label/anchors).

export interface MathlibInfo {
  decl?: string | null;
  module?: string | null;
  match_kind?: string | null;
}

export interface Anchor {
  type?: string;
  // math_alttext / theorem_box
  value?: string;
  // section + snippet (prose block)
  section?: string;
  snippet?: string;
  // prose_range
  from?: string;
  from_snippet?: string;
  to?: string;
  to_snippet?: string;
  to_math?: string;
  to_alttext?: string;
}

export interface Annotation {
  status: string;
  kind?: string;
  label?: string;
  note?: string;
  proof_note?: string;
  provenance?: string;
  match_kind?: string;
  anchor?: Anchor;
  anchors?: Anchor[];
  mathlib?: MathlibInfo;
  // flat v1/v2 fallbacks
  decl?: string;
  module?: string;
}

export type WrapperTag = "span" | "div";
