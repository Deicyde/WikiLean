// Projection of one D1 article snapshot into independent unversioned FCA draft records.
// The reference schema lives in formal-companion-annotations, not in this repo.
import { Hono } from "hono";
import { drizzle } from "drizzle-orm/d1";
import { eq } from "drizzle-orm";
import { articles } from "./db/schema.js";
import { mathlibDocsUrl } from "./engine/page.js";
import type { Env } from "./env.js";
import { NATIVE_SCHEMA_VERSION, storedCompanionRecords, validateNativeItems, isAbsoluteReference, strictJsonParse } from './native-companion.js';

type ObjectValue = Record<string, unknown>;
const ANCHOR_FIELDS = [
  "type", "value", "section", "snippet", "from", "from_snippet", "to",
  "to_snippet", "to_math", "to_alttext",
] as const;
export const ANCHOR_PROFILE = "urn:wikilean:anchor:v3";

export interface CompanionArticle {
  slug: string;
  wikipediaTitle: string;
  revid: number | null;
  version: number;
  annotations: unknown[];
}

// This is the subset emitted by this adapter, not a replacement core validator.
export interface CompanionAnnotation {
  source: { type: "generic"; uri: string };
  target: string;
  location: { profile: string; anchor: Record<string, string>; resolution: "not_attempted" };
  extensions: { "org.wikilean": ObjectValue };
}

export interface ExportDiagnostic {
  annotation: number;
  anchor?: number;
  code: "NO_FORMAL_TARGET" | "NO_ANCHOR" | "INVALID_ANCHOR" | "INVALID_TARGET";
}

function object(value: unknown): ObjectValue | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as ObjectValue : undefined;
}

function scalarString(value: unknown): value is string {
  // JSON.parse permits lone surrogates; the reference FCA codec rejects them.
  return typeof value === "string" && !/[\uD800-\uDFFF]/u.test(value);
}

function nonempty(value: unknown): value is string {
  return scalarString(value) && value.trim().length > 0;
}

export function isPinnedRevision(value: unknown): value is number {
  return Number.isSafeInteger(value) && Number(value) > 0;
}

export function exportCompanionAnnotations(article: CompanionArticle): {
  annotations: CompanionAnnotation[]; diagnostics: ExportDiagnostic[];
} {
  if (!isPinnedRevision(article.revid)) throw new Error("article has no pinned Wikipedia revision");
  if (!nonempty(article.wikipediaTitle) || !scalarString(article.slug)
    || !Number.isSafeInteger(article.version) || article.version < 1) {
    throw new Error("invalid article metadata");
  }
  const source = `https://en.wikipedia.org/w/index.php?title=${encodeURIComponent(article.wikipediaTitle.replaceAll(" ", "_"))}&oldid=${article.revid}`;
  const annotations: CompanionAnnotation[] = [];
  const diagnostics: ExportDiagnostic[] = [];
  for (const [index, value] of article.annotations.entries()) {
    const annotation = object(value);
    // Even a pipeline bearer receives only public associations. Rejected rows
    // and unknown statuses produce neither records nor identifying diagnostics.
    if (!annotation || typeof annotation.status !== "string"
      || !["formalized", "partial", "not_formalized"].includes(annotation.status)) continue;
    const mathlib = object(annotation.mathlib) ?? {};
    const decl = mathlib.decl || annotation.decl;
    const module = mathlib.module || annotation.module;
    if (annotation.status === "not_formalized" || !nonempty(decl) || !nonempty(module)) {
      diagnostics.push({ annotation: index, code: "NO_FORMAL_TARGET" });
      continue;
    }
    // This is the site's existing explicit module+declaration link convention.
    // Do not infer a module, use a search URL, or resolve a mutable index here.
    if (module.split(".").some((part) => !part || /[\s/\\#?]/u.test(part))) {
      diagnostics.push({ annotation: index, code: "INVALID_TARGET" });
      continue;
    }
    const target = mathlibDocsUrl(module, decl)!;
    const spans = Array.isArray(annotation.anchors) && annotation.anchors.length
      ? annotation.anchors : annotation.anchor !== undefined ? [annotation.anchor] : [];
    if (!spans.length) diagnostics.push({ annotation: index, code: "NO_ANCHOR" });
    for (const [anchorIndex, span] of spans.entries()) {
      const raw = object(span);
      if (!raw || ANCHOR_FIELDS.some((key) => raw[key] !== undefined && !scalarString(raw[key]))) {
        diagnostics.push({ annotation: index, anchor: anchorIndex, code: "INVALID_ANCHOR" });
        continue;
      }
      const anchor = Object.fromEntries(ANCHOR_FIELDS.flatMap((key) =>
        typeof raw[key] === "string" ? [[key, raw[key]]] : [])) as Record<string, string>;
      const metadata: ObjectValue = {
        slug: article.slug, article_version: article.version, wikipedia_revision: article.revid,
        annotation_index: index, anchor_index: anchorIndex,
        status: annotation.status, decl, module, proof_verification: "not_asserted",
        target_pinned: false,
      };
      for (const key of ["id", "kind", "label"] as const) {
        if (scalarString(annotation[key])) metadata[key] = annotation[key];
      }
      const matchKind = mathlib.match_kind || annotation.match_kind;
      if (scalarString(matchKind)) metadata.match_kind = matchKind;
      annotations.push({
        source: { type: "generic", uri: source }, target,
        // WikiLean snippets use a different normalization and ranges include
        // the endpoint. Keeping the locator opaque avoids false HTML quotes
        // or silently changing an unresolved span into the entire article.
        location: { profile: ANCHOR_PROFILE, anchor, resolution: "not_attempted" },
        extensions: { "org.wikilean": metadata },
      });
    }
  }
  return { annotations, diagnostics };
}

export function registerCompanionRoutes(app: Hono<{ Bindings: Env }>): void {
  // Lossless stored-record read. Carrier bases accompany raw references; no
  // URI normalization, source fetching, or extension projection takes place.
  app.get('/api/article/:slug/companion-records.json', async (c) => {
    const db = drizzle(c.env.DB);
    const row = (await db.select().from(articles).where(eq(articles.slug, c.req.param('slug'))).limit(1))[0];
    if (!row) return c.json({ ok: false, error: 'unknown slug' }, 404);
    if (row.schemaVersion !== NATIVE_SCHEMA_VERSION) return c.json({ ok: false, error: 'explicit native migration required' }, 409);
    const items = strictJsonParse(row.annotations) as ObjectValue[];
    if (validateNativeItems(items).length) return c.json({ ok: false, error: 'invalid native stored snapshot' }, 409);
    c.header('Cache-Control', 'no-store');
    return c.json({ version: row.version, records: storedCompanionRecords(items) });
  });
  app.get("/api/article/:slug/companion.json", async (c) => {
    const db = drizzle(c.env.DB);
    const row = (await db.select().from(articles).where(eq(articles.slug, c.req.param("slug"))).limit(1))[0];
    if (!row) return c.json({ ok: false, error: "unknown slug" }, 404);
    if (row.schemaVersion === NATIVE_SCHEMA_VERSION) {
      const items = strictJsonParse(row.annotations) as ObjectValue[];
      if (validateNativeItems(items).length) return c.json({ ok: false, error: 'invalid native stored snapshot' }, 409);
      const records = storedCompanionRecords(items);
      if (records.some(record => record.base_uri !== undefined || !isAbsoluteReference(record.annotation.source.uri) || !isAbsoluteReference(record.annotation.target)
        || (record.annotation.$schema !== undefined && !isAbsoluteReference(record.annotation.$schema)))) {
        return c.json({ ok: false, error: 'relative records require their original carrier bases',
          records_url: `/api/article/${encodeURIComponent(row.slug)}/companion-records.json` }, 409);
      }
      const annotations = records.map(record => record.annotation);
      c.header('Cache-Control', 'no-store');
      c.header('X-Companion-Annotations', String(annotations.length));
      c.header('X-Companion-Omissions', '0');
      return c.json(c.req.query('report') === '1' ? { annotations, diagnostics: [] } : annotations);
    }
    if (!isPinnedRevision(row.revid)) {
      return c.json({ ok: false, error: "article has no pinned Wikipedia revision" }, 409);
    }
    const result = exportCompanionAnnotations({ ...row, annotations: JSON.parse(row.annotations) as unknown[] });
    // No cross-version cache: rows and their pinned revid come from one read.
    c.header("Cache-Control", "no-store");
    c.header("X-Companion-Annotations", String(result.annotations.length));
    c.header("X-Companion-Omissions", String(result.diagnostics.length));
    // The default array is a discovery-compatible shard. The optional report
    // is operational output and must be saved outside a shard scan's include.
    return c.json(c.req.query("report") === "1" ? result : result.annotations);
  });
}
