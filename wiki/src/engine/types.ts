import type { NativeLink, UnlinkedOccurrence } from "../native-companion.js";
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
  // quote (a built-in Literature Links quotation read as an anchor)
  exact?: string;
  prefix?: string;
  suffix?: string;
}

export interface Annotation {
  [key: string]: unknown;
  links?: NativeLink[];
  unlinked?: UnlinkedOccurrence[];
  legacy?: Record<string, unknown>;
  // Stable 12-hex id (assigned by the pipeline/editor/Worker heal; immutable).
  id?: string;
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
