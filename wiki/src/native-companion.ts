/** WikiLean's application records. Each link holds one Literature Links 2.0 annotation, which
 * has no version, work-item identity, moderation state, or required application extension. */
import schemaCheck from './vendor/literature-links/validate.js';

export const NATIVE_SCHEMA_VERSION = 4;
export const ANCHOR_PROFILE = 'urn:wikilean:anchor:v3';
export const MATHLIB_REPOSITORY = 'https://github.com/leanprover-community/mathlib4';
export const MATHLIB_DOCS = 'https://leanprover-community.github.io/mathlib4_docs/';
export type JsonObject = Record<string, unknown>;
/** One end of a record: a document, or part of one. */
export interface LinkEnd {
  type: 'pdf' | 'latex' | 'html' | 'lean' | 'generic';
  uri: string;
  location?: JsonObject;
  version?: string;
  sha256?: string;
  citation?: JsonObject;
  identifiers?: Record<string, string>;
  title?: string;
  role?: string;
  extensions?: JsonObject;
}
/** A Literature Links 2.0 annotation: the subject ends, usually the passage, and the object ends. */
export interface LinkRecord {
  subject: LinkEnd[];
  object: LinkEnd[];
  relation?: string;
  junk?: string[];
  status?: string;
  kind?: string;
  id?: string;
  note?: string;
  extensions?: JsonObject;
  $schema?: string;
}
export interface NativeLink { id: string; annotation: LinkRecord; base_uri?: string }
/** A source selection that no formal end is linked to yet: one end and its carrier base. */
export type UnlinkedOccurrence = LinkEnd & { base_uri?: string };
export interface NativeItem extends JsonObject {
  id?: string;
  status: string;
  links: NativeLink[];
  unlinked: UnlinkedOccurrence[];
  legacy?: JsonObject;
}
export interface SourceContext { wikipediaTitle: string; revid: number | null; wikidataQid?: string | null }
export interface MigrationDiagnostic { item: number; anchor?: number; code: string }
const LEGACY_FIELDS = ['anchor', 'anchors', 'mathlib', 'decl', 'module'];
const PUBLIC_FIELDS = ['id', 'status', 'kind', 'label', 'note', 'provenance', 'match_kind', 'links', 'unlinked'];
const EDIT_FIELDS = new Set([...PUBLIC_FIELDS, 'note', 'proof_note']);
const ABSOLUTE = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const REFERENCE = /^[^\u0000-\u0020\u007f-\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+(?![\s\S])/u;
const SIDES = ['subject', 'object'] as const;
export const isObject = (value: unknown): value is JsonObject => value !== null && typeof value === 'object' && !Array.isArray(value);
export function isNativeItem(value: unknown): value is NativeItem {
  return isObject(value) && ('links' in value || 'unlinked' in value);
}
export const isAbsoluteReference = (value: string): boolean => ABSOLUTE.test(value);
const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/** A Mathlib declaration as a lean end: the repository, and the declaration with its module. */
export function mathlibEnd(module: string, decl: string): LinkEnd {
  return { type: 'lean', uri: MATHLIB_REPOSITORY, location: { declaration: decl, module } };
}
export function mathlibDocsUri(module: string, decl: string): string {
  return MATHLIB_DOCS + module.split('.').map(encodeURIComponent).join('/') + '.html#' + encodeURIComponent(decl);
}
/** The reference a reader follows for a formal end: the docs page of a Mathlib declaration, else its uri. */
export function endReference(end: LinkEnd): string {
  const location = isObject(end.location) ? end.location : {};
  if (end.type === 'lean' && end.uri === MATHLIB_REPOSITORY && !('lines' in location) && !('profile' in location)
    && typeof location.declaration === 'string' && typeof location.module === 'string') return mathlibDocsUri(location.module, location.declaration);
  return end.uri;
}
/** Every source selection of an item: each subject end of each link, then each unlinked end, with its base. */
export function itemOccurrences(item: NativeItem): Array<{ end: LinkEnd; base_uri?: string }> {
  return [
    ...item.links.flatMap(link => (Array.isArray(link?.annotation?.subject) ? link.annotation.subject : []).map(end => ({ end, base_uri: link.base_uri }))),
    ...item.unlinked.map(end => ({ end, base_uri: end?.base_uri })),
  ];
}

// Native matching uses the entire first occurrence, not legacy snippet heuristics.
// Python site/native_annotations.py uses this same code-point key ordering.
function compareKeys(a: string, b: string): number {
  const aa = Array.from(a, c => c.codePointAt(0)!); const bb = Array.from(b, c => c.codePointAt(0)!);
  for (let i = 0; i < Math.min(aa.length, bb.length); i++) if (aa[i] !== bb[i]) return aa[i] - bb[i];
  return aa.length - bb.length;
}
export function stableNative(value: unknown): string {
  // Signature encoding, not a JSON carrier: IEEE-754 bytes avoid Python/JS
  // decimal spelling differences (1.0 vs 1, exponent padding). Strings remain
  // quoted, so a literal '#...' cannot collide with a numeric token.
  if (typeof value === 'number') {
    const bytes = new Uint8Array(8); new DataView(bytes.buffer).setFloat64(0, value === 0 ? 0 : value);
    return '#' + Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  }
  if (Array.isArray(value)) return '[' + value.map(stableNative).join(',') + ']';
  if (isObject(value)) return '{' + Object.keys(value).sort(compareKeys).map(k => JSON.stringify(k) + ':' + stableNative(value[k])).join(',') + '}';
  return JSON.stringify(value);
}
export function nativeAnchorSig(item: NativeItem): string {
  const first = itemOccurrences(item)[0];
  const source = first ? { type: first.end?.type ?? null, uri: first.end?.uri ?? null } : null;
  return stableNative(['native', source, first?.end?.location ?? null, first?.base_uri ?? null]);
}

// JSON codec checks and the two semantic rules supplement the generated exact
// upstream schema. See vendor/literature-links/provenance.json for the reference checker pin.
function jsonProblem(value: unknown, active = new Set<unknown>(), safeIntegers = false): string | null {
  if (typeof value === 'string') return /[\uD800-\uDFFF]/u.test(value) ? 'unpaired Unicode surrogate' : null;
  if (value === null || typeof value === 'boolean') return null;
  if (typeof value === 'number') return !Number.isFinite(value) ? 'nonfinite number'
    : safeIntegers && Number.isInteger(value) && !Number.isSafeInteger(value) ? 'integer exceeds lossless JavaScript storage range' : null;
  if (!Array.isArray(value) && !isObject(value)) return 'not a JSON value';
  if (active.has(value)) return 'cyclic JSON';
  active.add(value);
  for (const [key, child] of Object.entries(value)) {
    const problem = jsonProblem(key, active, safeIntegers) ?? jsonProblem(child, active, safeIntegers);
    if (problem) return problem;
  }
  active.delete(value);
  return null;
}
export function validateLinkRecord(value: unknown): string[] {
  let problem: string | null;
  try { problem = jsonProblem(value); } catch { return ['JSON nesting exceeds implementation limit']; }
  if (problem) return [problem];
  if (!schemaCheck(value)) return (schemaCheck.errors ?? []).map(e => `${e.instancePath || '/'}: ${e.message}`);
  const problems: string[] = [];
  const ann = value as LinkRecord;
  for (const side of SIDES) ann[side].forEach((end, index) => {
    const loc = end.location;
    // A location with a profile is application-defined; the built-in checks do not apply.
    if (!loc || 'profile' in loc) return;
    const path = `/${side}/${index}/location`;
    if (end.type === 'pdf' && Array.isArray(loc.rect)) {
      // Double sums, as the specification says, so every language agrees.
      const [x, y, w, h] = loc.rect as number[];
      if (w <= 0 || h <= 0 || x + w > 1 || y + h > 1) problems.push(`${path}/rect: PDF rectangle must have positive size and fit within the page`);
    } else if ((end.type === 'latex' || end.type === 'lean') && Array.isArray(loc.lines) && loc.lines.length === 2 && loc.lines[1] < loc.lines[0]) {
      problems.push(`${path}/lines: lines must end no earlier than they start`);
    }
  });
  return problems;
}

export function wikipediaSourceUri(context: SourceContext): string {
  if (!Number.isSafeInteger(context.revid) || Number(context.revid) <= 0) throw new Error('source revision is unknown');
  return `https://en.wikipedia.org/w/index.php?title=${encodeURIComponent(context.wikipediaTitle.replaceAll(' ', '_'))}&oldid=${context.revid}`;
}
/** The article's readable URL; an exported end names the revision in its version. */
export function wikipediaArticleUri(title: string): string {
  return `https://en.wikipedia.org/wiki/${encodeURIComponent(title.replaceAll(' ', '_'))}`;
}
function sourceMatches(end: LinkEnd, context?: SourceContext, baseUri?: string): boolean {
  if (!context) return false;
  try { return new URL(end.uri, baseUri).href === new URL(wikipediaSourceUri(context)).href; } catch { return false; }
}
/** Whether an end is a revision of the article itself, whatever its oldid. */
function articleLocal(end: LinkEnd, context: SourceContext, baseUri?: string): URL | null {
  try {
    const uri = new URL(end.uri, baseUri);
    return uri.origin === 'https://en.wikipedia.org' && uri.pathname === '/w/index.php'
      && uri.searchParams.get('title')?.replaceAll('_', ' ') === context.wikipediaTitle.replaceAll('_', ' ') ? uri : null;
  } catch { return null; }
}
export function legacyAnchorOccurrences(item: JsonObject, context?: SourceContext): Array<JsonObject | null> {
  if (!isNativeItem(item)) return Array.isArray(item.anchors) && item.anchors.length
    ? item.anchors.map(a => isObject(a) ? a : null) : [isObject(item.anchor) ? item.anchor : null];
  return itemOccurrences(item).map(({ end, base_uri }) => {
    if (!sourceMatches(end, context, base_uri)) return null;
    if (!('location' in end) && end.type === 'html') return { type: 'whole_resource' };
    const loc = end.location;
    if (end.type !== 'html' || !isObject(loc)) return null;
    if (loc.profile === ANCHOR_PROFILE && isObject(loc.anchor)) return wellFormedAnchor(loc.anchor);
    if ('profile' in loc) return null;
    // A built-in Literature Links location. WikiLean's own ends keep the
    // editing key beside it; any other end is read from the location itself.
    const key = anchorExtension(end);
    return isObject(key) ? wellFormedAnchor(key) : deriveAnchor(loc);
  });
}
const ANCHOR_KEYS = ['type', 'value', 'section', 'snippet', 'from', 'from_snippet', 'to', 'to_snippet', 'to_math', 'to_alttext'];
function wellFormedAnchor(anchor: JsonObject): JsonObject | null {
  if (ANCHOR_KEYS.some(key => key in anchor && typeof anchor[key] !== 'string')) return null;
  if (['math_alttext', 'theorem_box'].includes(String(anchor.type)) && !anchor.value) return null;
  if (anchor.type === 'prose_range' && (anchor.section === undefined || !(anchor.from || anchor.from_snippet))) return null;
  return anchor;
}
/** The WikiLean editing key stored in an end's extensions beside a built-in location. */
export function anchorExtension(end: { extensions?: JsonObject }): unknown {
  const wikilean = isObject(end.extensions) ? end.extensions['org.wikilean'] : undefined;
  return isObject(wikilean) ? wikilean.anchor : undefined;
}
const MATH_SELECTOR = /^(img\[alt|math\[alttext)="((?:[^"\\]|\\.)*)"\]$/;
/** The alt text named by a selector the Python resolver writes, or null. */
export function parseMathSelector(selector: string): string | null {
  const match = MATH_SELECTOR.exec(selector);
  // One pass, so an escaped backslash before "a " stays a backslash.
  return match ? match[2].replace(/\\(a |[\s\S])/g, (_, c: string) => c === 'a ' ? '\n' : c) : null;
}
/** Read a built-in HTML location as a renderer anchor: a quotation, a formula or an element id. */
export function deriveAnchor(location: JsonObject): JsonObject | null {
  const quote = location.quote;
  if (isObject(quote) && typeof quote.exact === 'string' && quote.exact) {
    const anchor: JsonObject = { type: 'quote', exact: quote.exact };
    for (const key of ['prefix', 'suffix'] as const) if (typeof quote[key] === 'string') anchor[key] = quote[key];
    return anchor;
  }
  if (typeof location.css === 'string') {
    const value = parseMathSelector(location.css);
    return value ? { type: 'math_alttext', value } : null;
  }
  if (typeof location.fragment === 'string' && location.fragment) return { type: 'fragment', value: location.fragment };
  return null;
}

function referenceProblems(base: unknown, references: string[]): string[] {
  const errors: string[] = [];
  if (base !== undefined && (typeof base !== 'string' || !ABSOLUTE.test(base) || !REFERENCE.test(base))) errors.push('base_uri must be an absolute reference');
  if (references.some(uri => !ABSOLUTE.test(uri)) && !base) errors.push('relative references require durable base_uri');
  return errors;
}
// Only the article's own Wikipedia source is constrained by its source pin;
// unrelated imported resources remain valid, without fetching any end.
function pinProblems(end: LinkEnd, base: string | undefined, context?: SourceContext): string[] {
  const uri = context ? articleLocal(end, context, base) : null;
  return uri && uri.searchParams.get('oldid') !== String(context!.revid) ? ['article occurrence source revision differs from article pin'] : [];
}
export function articleLocalOccurrences(items: JsonObject[], context: SourceContext): NativeItem[] {
  const local = (end: LinkEnd, base?: string) => articleLocal(end, context, base) !== null;
  return items.filter(isNativeItem).map(item => ({ ...item,
    links: item.links.flatMap(link => {
      const subject = link.annotation.subject.filter(end => local(end, link.base_uri));
      return subject.length ? [{ ...link, annotation: { ...link.annotation, subject } }] : [];
    }),
    unlinked: item.unlinked.filter(end => local(end, end.base_uri)),
  })).filter(item => item.links.length + item.unlinked.length > 0);
}
/** The items with every active article-local source moved to `revid`. The pin is
 * article metadata, so this is the one change a repin makes to human-owned
 * items; rejected items keep their historical pins. */
export function repinArticleSources(items: JsonObject[], context: SourceContext, revid: number): JsonObject[] {
  const next = wikipediaSourceUri({ ...context, revid });
  const move = <T extends LinkEnd>(end: T, baseUri?: string): T => sourceMatches(end, context, baseUri) ? { ...end, uri: next } : end;
  return items.map(item => !isNativeItem(item) || item.status === 'rejected' ? item : { ...item,
    links: item.links.map(link => ({ ...link, annotation: { ...link.annotation, subject: link.annotation.subject.map(end => move(end, link.base_uri)) } })),
    unlinked: item.unlinked.map(end => move(end, end.base_uri)),
  });
}
export function validateNativeItems(items: unknown[], context?: SourceContext): string[] {
  const errors: string[] = []; const ids = new Set<string>(); const itemIds = new Set<string>();
  for (const [index, value] of items.entries()) {
    if (!isNativeItem(value) || !Array.isArray(value.links) || !Array.isArray(value.unlinked)) {
      errors.push(`item ${index}: native items require links and unlinked arrays`); continue;
    }
    if (LEGACY_FIELDS.some(key => key in value)) errors.push(`item ${index}: legacy link fields are not editable native data`);
    try {
      const storageProblem = jsonProblem(value, new Set(), true);
      if (storageProblem) errors.push(`item ${index}: ${storageProblem}`);
    } catch { errors.push(`item ${index}: JSON nesting exceeds implementation limit`); }
    if (!['formalized', 'partial', 'not_formalized', 'rejected'].includes(value.status)) errors.push(`item ${index}: invalid review status`);
    if (value.id !== undefined) {
      if (typeof value.id !== 'string' || !/^[0-9a-f]{12}$/.test(value.id) || itemIds.has(value.id)) errors.push(`item ${index}: invalid or duplicate work-item id`);
      itemIds.add(value.id);
    }
    const pinContext = value.status === 'rejected' ? undefined : context;
    for (const [n, link] of value.links.entries()) {
      const prefix = `item ${index} link ${n}: `;
      if (!isObject(link) || typeof link.id !== 'string' || !link.id.length || link.id.length > 256 || ids.has(link.id)) {
        errors.push(prefix + 'internal link id must be nonempty and unique in the article'); continue;
      }
      ids.add(link.id);
      if (Object.keys(link).some(key => !['id', 'annotation', 'base_uri'].includes(key))) errors.push(prefix + 'unknown link envelope field');
      const coreErrors = validateLinkRecord(link.annotation);
      errors.push(...coreErrors.map(e => prefix + e));
      if (coreErrors.length) continue;
      const annotation = link.annotation;
      // A link is an occurrence with a formal end; a selection without one belongs in unlinked.
      if (!annotation.object.length) { errors.push(prefix + 'link annotation requires an object end; keep targetless selections in unlinked'); continue; }
      // A checked absence names what was searched; without a version it says no more than not_formalized.
      if (annotation.relation === 'noMatch' && annotation.object.some(end => end.version === undefined)) {
        errors.push(prefix + 'a noMatch object end requires the version that was searched; keep the selection in unlinked with status not_formalized');
      }
      const references = [...annotation.subject, ...annotation.object].map(end => end.uri);
      if (annotation.$schema !== undefined) references.push(annotation.$schema);
      errors.push(...referenceProblems(link.base_uri, references).map(e => prefix + e));
      for (const end of annotation.subject) errors.push(...pinProblems(end, link.base_uri, pinContext).map(e => prefix + e));
    }
    for (const [n, occ] of value.unlinked.entries()) {
      const prefix = `item ${index} unlinked ${n}: `;
      if (!isObject(occ)) { errors.push(prefix + 'expected object'); continue; }
      const { base_uri: base, ...rest } = occ;
      const end = rest as unknown as LinkEnd;
      const coreErrors = validateLinkRecord({ subject: [end], object: [] });
      errors.push(...coreErrors.map(e => prefix + e));
      if (coreErrors.length) continue;
      errors.push(...referenceProblems(base, [end.uri]).map(e => prefix + e));
      errors.push(...pinProblems(end, base, pinContext).map(e => prefix + e));
    }
  }
  return errors;
}

/** Explicit migration only. Archive the original work item; don't derive links
 * during normal reads or save native records through a legacy round trip. */
export function migrateLegacyItems(items: JsonObject[], context: SourceContext): { items: NativeItem[]; diagnostics: MigrationDiagnostic[] } {
  const uri = wikipediaSourceUri(context); const diagnostics: MigrationDiagnostic[] = [];
  const native = items.map((original, index): NativeItem => {
    if (isNativeItem(original)) return copy(original);
    const item = copy(original) as NativeItem;
    for (const key of LEGACY_FIELDS) delete item[key];
    item.links = []; item.unlinked = []; item.legacy = copy(original);
    const mathlib = isObject(original.mathlib) ? original.mathlib : {};
    if (!('match_kind' in item) && typeof mathlib.match_kind === 'string') item.match_kind = mathlib.match_kind;
    const decl = mathlib.decl || original.decl; const module = mathlib.module || original.module;
    // The legacy module and declaration become a lean object end on Mathlib, so
    // the only remaining copy of that information is not the private archive.
    let object: LinkEnd | null = null;
    if (typeof decl === 'string' && decl.trim() && typeof module === 'string' && module.trim()
      && !jsonProblem(decl) && !jsonProblem(module) && module.split('.').every(part => part && !/[\s/\\#?]/u.test(part))) {
      object = mathlibEnd(module, decl);
    }
    if (!object) diagnostics.push({ item: index, code: 'NO_FORMAL_TARGET' });
    if (object && original.status === 'not_formalized') diagnostics.push({ item: index, code: 'STATUS_TARGET_CONFLICT' });
    const spans = Array.isArray(original.anchors) && original.anchors.length ? original.anchors : ['anchor' in original ? original.anchor : {}];
    if (!('anchor' in original) && !(Array.isArray(original.anchors) && original.anchors.length)) diagnostics.push({ item: index, code: 'MISSING_ANCHOR' });
    spans.forEach((span, anchorIndex) => {
      // Unknown original fields remain in the private archive, not accidentally
      // promoted into a public extension. Known selector strings retain meaning.
      const valid = isObject(span) && ANCHOR_KEYS.every(key => span[key] === undefined || (typeof span[key] === 'string' && !jsonProblem(span[key])));
      if (!valid) diagnostics.push({ item: index, anchor: anchorIndex, code: 'INVALID_ANCHOR' });
      const anchor = valid ? Object.fromEntries(ANCHOR_KEYS.filter(key => key in span).map(key => [key, span[key]])) : {};
      const occurrence: UnlinkedOccurrence = { type: 'html', uri, location: { profile: ANCHOR_PROFILE, anchor, resolution: valid ? 'not_attempted' : 'unresolved' } };
      if (object && valid) item.links.push({ id: `${original.id || 'item' + index}:link:${anchorIndex}`, annotation: { subject: [occurrence], object: [copy(object)] } });
      else item.unlinked.push(occurrence);
    });
    return item;
  });
  const errors = validateNativeItems(native, context);
  if (errors.length) throw new Error(errors.join('; '));
  return { items: native, diagnostics };
}

/** Public native work-item view. Private archives/unknown workflow fields never
 * become portable annotation extensions or public history differences. */
export function publicItems(items: JsonObject[], strict = false): Array<JsonObject | null> {
  return items.map(item => item.status === 'rejected' ? null : (isNativeItem(item) || strict)
    ? Object.fromEntries(PUBLIC_FIELDS.filter(key => key in item).map(key => [key, copy(item[key])])) : item);
}
export function editorItems(items: JsonObject[]): JsonObject[] {
  return items.map(item => isNativeItem(item)
    ? Object.fromEntries([...EDIT_FIELDS].filter(key => key in item).map(key => [key, copy(item[key])])) : item);
}
export function restorePrivateMetadata(stored: JsonObject[], posted: JsonObject[]): JsonObject[] {
  return posted.map(item => {
    if (!isNativeItem(item)) return item;
    const old = stored.find(value => value.id !== undefined && value.id === item.id);
    const result = { ...item };
    delete result.legacy;
    if (old) for (const [key, value] of Object.entries(old)) {
      if (!EDIT_FIELDS.has(key) || (!(key in item) && ['note', 'proof_note'].includes(key))) Object.defineProperty(result, key, { value: copy(value), enumerable: true, configurable: true, writable: true });
    }
    return result;
  });
}

const nonblank = (value: unknown): string | undefined => typeof value === 'string' && value.trim() ? value : undefined;
/** The portable record for one stored link: the stored annotation plus the review
 * item's facts in the format's own fields (status, kind, id, note, and the label
 * as each subject end's title), and provenance and match_kind under org.wikilean.
 * The item stays the editable authority; stored fields are kept and only absent
 * ones are filled, except status, which is always the item's review status. */
export function companionAnnotation(item: JsonObject, link: NativeLink, _context?: SourceContext): LinkRecord {
  const stored = link.annotation;
  const label = nonblank(item.label);
  const record = {} as LinkRecord;
  if (stored.$schema !== undefined) record.$schema = stored.$schema;
  record.subject = stored.subject.map(end => end.title === undefined && label !== undefined ? { ...copy(end), title: label } : copy(end));
  record.object = copy(stored.object);
  if (stored.relation !== undefined) record.relation = stored.relation;
  if (stored.junk !== undefined) record.junk = copy(stored.junk);
  if (typeof item.status === 'string' && item.status) record.status = item.status;
  else if (stored.status !== undefined) record.status = stored.status;
  const kind = stored.kind ?? nonblank(item.kind); if (kind !== undefined) record.kind = kind;
  const id = stored.id ?? (typeof item.id === 'string' && item.id ? item.id : undefined); if (id !== undefined) record.id = id;
  const note = stored.note ?? nonblank(item.note); if (note !== undefined) record.note = note;
  const wikilean: JsonObject = isObject(stored.extensions?.['org.wikilean']) ? { ...stored.extensions['org.wikilean'] } : {};
  for (const key of ['provenance', 'match_kind'] as const) if (typeof item[key] === 'string' && item[key]) wikilean[key] = item[key];
  if (Object.keys(wikilean).length) record.extensions = { ...(stored.extensions ? copy(stored.extensions) : {}), 'org.wikilean': wikilean };
  else if (stored.extensions !== undefined) record.extensions = copy(stored.extensions);
  return record;
}
export interface CompanionRecord { item_id?: string; id: string; annotation: LinkRecord; base_uri?: string }
export function storedCompanionRecords(items: JsonObject[], context?: SourceContext): CompanionRecord[] {
  return items.flatMap(item => isNativeItem(item) && item.status !== 'rejected'
    ? item.links.map(link => ({ id: link.id, annotation: companionAnnotation(item, link, context),
      ...(link.base_uri !== undefined ? { base_uri: link.base_uri } : {}), ...(typeof item.id === 'string' ? { item_id: item.id } : {}) })) : []);
}
