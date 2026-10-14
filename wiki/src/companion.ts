// Projection of one D1 article snapshot into independent Literature Links annotations.
// The schema is vendored from Literature Links under vendor/literature-links.
import { Hono } from "hono";
import { drizzle } from "drizzle-orm/d1";
import { eq } from "drizzle-orm";
import { articles } from "./db/schema.js";
import type { Env } from "./env.js";
import {
  NATIVE_SCHEMA_VERSION, storedCompanionRecords, validateNativeItems, isAbsoluteReference, strictJsonParse,
  articleSourcePin, mathlibTarget, targetUri, type LinkPin, type LinkTarget, type JsonObject as NativeObject,
} from './native-companion.js';

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
  wikidataQid?: string | null;
  annotations: unknown[];
}

// This is the subset emitted by this adapter, not a replacement core validator.
// The item's review fields use the format's own slots; provenance and
// match_kind, which have none, stay under org.wikilean.
export interface CompanionAnnotation {
  id?: string;
  kind?: string;
  label?: string;
  status: string;
  source: { type: "html"; uri: string; pin: LinkPin };
  location: { profile: string; anchor: Record<string, string>; resolution: "not_attempted" };
  target: LinkTarget;
  note?: string;
  extensions?: { "org.wikilean": NativeObject };
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
  // JSON.parse permits lone surrogates; the Literature Links codec rejects them.
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
  const context = { wikipediaTitle: article.wikipediaTitle, revid: article.revid, wikidataQid: article.wikidataQid };
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
      const record: CompanionAnnotation = {
        ...(scalarString(annotation.id) ? { id: annotation.id } : {}),
        ...(nonempty(annotation.kind) ? { kind: annotation.kind } : {}),
        ...(nonempty(annotation.label) ? { label: annotation.label } : {}),
        status: annotation.status,
        source: { type: "html", uri: source, pin: articleSourcePin(context) },
        // WikiLean snippets use a different normalization and ranges include
        // the endpoint. Keeping the locator opaque avoids false HTML quotes
        // or silently changing an unresolved span into the entire article.
        location: { profile: ANCHOR_PROFILE, anchor, resolution: "not_attempted" },
        // The explicit module and declaration are the repository pin. No
        // commit is recorded: the legacy item does not say which one it used.
        target: mathlibTarget(module, decl),
        ...(nonempty(annotation.note) ? { note: annotation.note } : {}),
      };
      const wikilean: NativeObject = {};
      if (nonempty(annotation.provenance)) wikilean.provenance = annotation.provenance;
      const matchKind = mathlib.match_kind || annotation.match_kind;
      if (nonempty(matchKind)) wikilean.match_kind = matchKind;
      if (Object.keys(wikilean).length) record.extensions = { "org.wikilean": wikilean };
      annotations.push(record);
    }
  }
  return { annotations, diagnostics };
}

/** Each annotation on its own line; an empty list is an empty document. */
export function toJsonl(annotations: unknown[]): string {
  return annotations.map((annotation) => JSON.stringify(annotation) + "\n").join("");
}

type CompanionResult =
  | { ok: true; annotations: unknown[]; diagnostics: ExportDiagnostic[] }
  | { ok: false; status: 404 | 409; body: ObjectValue };

/** The public annotations of one article from a single read of its row. */
async function articleCompanion(env: Env, slug: string): Promise<CompanionResult> {
  const db = drizzle(env.DB);
  const row = (await db.select().from(articles).where(eq(articles.slug, slug)).limit(1))[0];
  if (!row) return { ok: false, status: 404, body: { ok: false, error: "unknown slug" } };
  if (row.schemaVersion === NATIVE_SCHEMA_VERSION) {
    const items = strictJsonParse(row.annotations) as ObjectValue[];
    if (validateNativeItems(items).length) return { ok: false, status: 409, body: { ok: false, error: "invalid native stored snapshot" } };
    const records = storedCompanionRecords(items, { wikipediaTitle: row.wikipediaTitle, revid: row.revid, wikidataQid: row.wikidataQid });
    if (records.some(record => record.base_uri !== undefined || !isAbsoluteReference(record.annotation.source.uri) || !isAbsoluteReference(targetUri(record.annotation) ?? '')
      || (record.annotation.$schema !== undefined && !isAbsoluteReference(record.annotation.$schema)))) {
      return { ok: false, status: 409, body: { ok: false, error: 'relative records require their original carrier bases',
        records_url: `/api/article/${encodeURIComponent(row.slug)}/companion-records.json` } };
    }
    return { ok: true, annotations: records.map(record => record.annotation), diagnostics: [] };
  }
  if (!isPinnedRevision(row.revid)) {
    return { ok: false, status: 409, body: { ok: false, error: "article has no pinned Wikipedia revision" } };
  }
  // No cross-version cache: rows and their pinned revid come from one read.
  return { ok: true, ...exportCompanionAnnotations({ ...row, annotations: strictJsonParse(row.annotations) as unknown[] }) };
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
    return c.json({ version: row.version, records: storedCompanionRecords(items, { wikipediaTitle: row.wikipediaTitle, revid: row.revid, wikidataQid: row.wikidataQid }) });
  });
  // One annotation per line, as Literature Links reads a list of annotations
  // from a file or a web response. The report is operational JSON, not a list.
  app.get("/api/article/:slug/companion.jsonl", async (c) => {
    const result = await articleCompanion(c.env, c.req.param("slug"));
    if (!result.ok) return c.json(result.body, result.status);
    return new Response(toJsonl(result.annotations), { headers: {
      "Content-Type": "application/x-ndjson; charset=utf-8", "Cache-Control": "no-store",
      "X-Companion-Annotations": String(result.annotations.length),
      "X-Companion-Omissions": String(result.diagnostics.length),
    } });
  });
  app.get("/api/article/:slug/companion-report.json", async (c) => {
    const result = await articleCompanion(c.env, c.req.param("slug"));
    if (!result.ok) return c.json(result.body, result.status);
    c.header("Cache-Control", "no-store");
    return c.json({ annotations: result.annotations, diagnostics: result.diagnostics });
  });
}
