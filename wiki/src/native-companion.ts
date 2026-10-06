/** WikiLean's application records. The FCA annotation itself has no version,
 * work-item identity, moderation state, or required application extension. */
import schemaCheck from './vendor/fca/validate.js';

export const NATIVE_SCHEMA_VERSION = 4;
export const ANCHOR_PROFILE = 'urn:wikilean:anchor:v3';
export type JsonObject = Record<string, unknown>;
export interface FCARecord {
  source: { type: 'pdf' | 'latex' | 'html' | 'generic'; uri: string; extensions?: JsonObject };
  target: string;
  location?: JsonObject;
  id?: string;
  $schema?: string;
  extensions?: JsonObject;
}
export interface NativeLink { id: string; annotation: FCARecord; base_uri?: string }
export interface UnlinkedOccurrence { source: FCARecord['source']; location?: JsonObject; base_uri?: string }
export interface NativeItem extends JsonObject {
  id?: string;
  status: string;
  links: NativeLink[];
  unlinked: UnlinkedOccurrence[];
  legacy?: JsonObject;
}
export interface SourceContext { wikipediaTitle: string; revid: number | null }
export interface MigrationDiagnostic { item: number; anchor?: number; code: string }
const LEGACY_FIELDS = ['anchor', 'anchors', 'mathlib', 'decl', 'module'];
const PUBLIC_FIELDS = ['id', 'status', 'kind', 'label', 'provenance', 'match_kind', 'links', 'unlinked'];
const EDIT_FIELDS = new Set([...PUBLIC_FIELDS, 'note', 'proof_note']);
const ABSOLUTE = /^[A-Za-z][A-Za-z0-9+.-]*:/;
const REFERENCE = /^[^\u0000-\u0020\u007f-\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+(?![\s\S])/u;
export const isObject = (value: unknown): value is JsonObject => value !== null && typeof value === 'object' && !Array.isArray(value);
export function isNativeItem(value: unknown): value is NativeItem {
  return isObject(value) && ('links' in value || 'unlinked' in value);
}
export const isAbsoluteReference = (value: string): boolean => ABSOLUTE.test(value);
const copy = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;

/** Strict transport decoding before schema checks. JSON.parse alone silently
 * discards duplicate members and rounds oversized integer tokens. This scanner
 * uses JSON.parse for grammar/string decoding, not a second annotation schema. */
export function strictJsonParse(text: string): unknown {
  const parsed: unknown = JSON.parse(text);
  let at = 0;
  const whitespace = () => { while (at < text.length && /[\t\n\r ]/.test(text[at])) at++; };
  const string = (): string => {
    const start = at++;
    while (at < text.length) {
      if (text[at] === '\\') { at += 2; continue; }
      if (text[at++] === '"') return JSON.parse(text.slice(start, at)) as string;
    }
    throw new Error('unterminated JSON string');
  };
  const value = (): void => {
    whitespace();
    if (text[at] === '{') {
      at++; whitespace(); const keys = new Set<string>();
      if (text[at] === '}') { at++; return; }
      while (at < text.length) {
        whitespace(); const key = string();
        if (keys.has(key)) throw new Error(`duplicate JSON member ${JSON.stringify(key)}`);
        keys.add(key); whitespace(); at++; value(); whitespace();
        if (text[at++] === '}') return;
      }
    } else if (text[at] === '[') {
      at++; whitespace(); if (text[at] === ']') { at++; return; }
      while (at < text.length) { value(); whitespace(); if (text[at++] === ']') return; }
    } else if (text[at] === '"') { string(); }
    else {
      const start = at;
      while (at < text.length && !/[\t\n\r ,}\]]/.test(text[at])) at++;
      const token = text.slice(start, at);
      if (/^-?\d/.test(token)) {
        if (!Number.isFinite(Number(token))) throw new Error('nonfinite JSON number');
        if (/^-?\d+$/.test(token) && (BigInt(token) > 9007199254740991n || BigInt(token) < -9007199254740991n)) throw new Error('integer exceeds lossless JavaScript storage range');
      }
    }
  };
  value();
  return parsed;
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
  const first = item.links?.[0];
  const occurrence = first ? { ...first.annotation, base_uri: first.base_uri } : item.unlinked?.[0];
  return stableNative(['native', occurrence?.source ?? null, occurrence?.location ?? null, occurrence?.base_uri ?? null]);
}

// JSON codec checks and the two semantic rules supplement the generated exact
// upstream schema. See vendor/fca/provenance.json for the reference checker pin.
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
function decimal(value: number): [bigint, number] {
  const [mantissa, exponent = '0'] = String(value).split('e');
  const [whole, fraction = ''] = mantissa.split('.');
  return [BigInt(whole + fraction), fraction.length - Number(exponent)];
}
function exceedsOne(a: number, b: number): boolean {
  const [x, xs] = decimal(a); const [y, ys] = decimal(b); const scale = Math.max(0, xs, ys);
  return x * 10n ** BigInt(scale - xs) + y * 10n ** BigInt(scale - ys) > 10n ** BigInt(scale);
}
export function validateFCA(value: unknown): string[] {
  let problem: string | null;
  try { problem = jsonProblem(value); } catch { return ['JSON nesting exceeds implementation limit']; }
  if (problem) return [problem];
  if (!schemaCheck(value)) return (schemaCheck.errors ?? []).map(e => `${e.instancePath || '/'}: ${e.message}`);
  const ann = value as FCARecord;
  const loc = ann.location;
  if (ann.source.type === 'pdf' && Array.isArray(loc?.rect)) {
    const [x, y, w, h] = loc.rect as number[];
    if (w <= 0 || h <= 0 || exceedsOne(x, w) || exceedsOne(y, h)) return ['PDF rectangle must have positive size and fit within the page'];
  }
  if (ann.source.type === 'latex' && Array.isArray(loc?.lines) && loc.lines[1] < loc.lines[0]) return ['LaTeX lines must be ordered'];
  return [];
}

export function wikipediaSourceUri(context: SourceContext): string {
  if (!Number.isSafeInteger(context.revid) || Number(context.revid) <= 0) throw new Error('source revision is unknown');
  return `https://en.wikipedia.org/w/index.php?title=${encodeURIComponent(context.wikipediaTitle.replaceAll(' ', '_'))}&oldid=${context.revid}`;
}
function sourceMatches(source: FCARecord['source'], context?: SourceContext, baseUri?: string): boolean {
  if (!context) return false;
  try { return new URL(source.uri, baseUri).href === new URL(wikipediaSourceUri(context)).href; } catch { return false; }
}
export function legacyAnchorOccurrences(item: JsonObject, context?: SourceContext): Array<JsonObject | null> {
  if (!isNativeItem(item)) return Array.isArray(item.anchors) && item.anchors.length
    ? item.anchors.map(a => isObject(a) ? a : null) : [isObject(item.anchor) ? item.anchor : null];
  const occurrences = [...item.links.map(link => ({ ...link.annotation, base_uri: link.base_uri })), ...item.unlinked];
  return occurrences.map(occurrence => {
    if (!sourceMatches(occurrence.source, context, occurrence.base_uri)) return null;
    if (!('location' in occurrence) && ['html', 'generic'].includes(occurrence.source.type)) return { type: 'whole_resource' };
    const loc = occurrence.location;
    if (occurrence.source.type !== 'generic' || loc?.profile !== ANCHOR_PROFILE || !isObject(loc.anchor)) return null;
    const keys = ['type', 'value', 'section', 'snippet', 'from', 'from_snippet', 'to', 'to_snippet', 'to_math', 'to_alttext'];
    const anchor = loc.anchor;
    if (keys.some(key => key in anchor && typeof anchor[key] !== 'string')) return null;
    if (['math_alttext', 'theorem_box'].includes(String(loc.anchor.type)) && !loc.anchor.value) return null;
    if (loc.anchor.type === 'prose_range' && (loc.anchor.section === undefined || !(loc.anchor.from || loc.anchor.from_snippet))) return null;
    return loc.anchor;
  });
}
export function itemFormalTargets(item: JsonObject): string[] {
  return isNativeItem(item) ? item.links.map(link => link.annotation.target) : [];
}

function occurrenceProblems(occ: UnlinkedOccurrence, target: string, context?: SourceContext): string[] {
  const errors = validateFCA({ source: occ.source, target, ...('location' in occ ? { location: occ.location } : {}) });
  if (errors.length) return errors;
  if (occ.base_uri !== undefined && (typeof occ.base_uri !== 'string' || !ABSOLUTE.test(occ.base_uri) || !REFERENCE.test(occ.base_uri))) errors.push('base_uri must be an absolute reference');
  if ((!ABSOLUTE.test(occ.source?.uri ?? '') || !ABSOLUTE.test(target)) && !occ.base_uri) errors.push('relative references require durable base_uri');
  if (context && typeof occ.source?.uri === 'string') {
    // Only the article's own Wikipedia source is constrained by its source pin;
    // unrelated imported resources remain valid, without fetching any target.
    try {
      const uri = new URL(occ.source.uri, occ.base_uri);
      if (uri.origin === 'https://en.wikipedia.org' && uri.pathname === '/w/index.php'
        && uri.searchParams.get('title')?.replaceAll('_', ' ') === context.wikipediaTitle.replaceAll('_', ' ')
        && uri.searchParams.get('oldid') !== String(context.revid)) errors.push('article occurrence source revision differs from article pin');
    } catch { /* Core references are lexical; opaque or unfamiliar references stay intact. */ }
  }
  return errors;
}
export function articleLocalOccurrences(items: JsonObject[], context: SourceContext): NativeItem[] {
  const local = (occurrence: UnlinkedOccurrence) => {
    try {
      const uri = new URL(occurrence.source.uri, occurrence.base_uri);
      return uri.origin === 'https://en.wikipedia.org' && uri.pathname === '/w/index.php'
        && uri.searchParams.get('title')?.replaceAll('_', ' ') === context.wikipediaTitle.replaceAll('_', ' ');
    } catch { return false; }
  };
  return items.filter(isNativeItem).map(item => ({ ...item,
    links: item.links.filter(link => local({ ...link.annotation, base_uri: link.base_uri })),
    unlinked: item.unlinked.filter(local),
  })).filter(item => item.links.length + item.unlinked.length > 0);
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
    for (const [n, link] of value.links.entries()) {
      const prefix = `item ${index} link ${n}: `;
      if (!isObject(link) || typeof link.id !== 'string' || !link.id.length || link.id.length > 256 || ids.has(link.id)) {
        errors.push(prefix + 'internal link id must be nonempty and unique in the article'); continue;
      }
      ids.add(link.id);
      if (Object.keys(link).some(key => !['id', 'annotation', 'base_uri'].includes(key))) errors.push(prefix + 'unknown link envelope field');
      const coreErrors = validateFCA(link.annotation);
      errors.push(...coreErrors.map(e => prefix + e));
      if (!coreErrors.length && link.annotation.$schema && !ABSOLUTE.test(link.annotation.$schema) && !link.base_uri) errors.push(prefix + 'relative schema hint requires durable base_uri');
      if (!coreErrors.length) errors.push(...occurrenceProblems({ ...link.annotation, base_uri: link.base_uri }, link.annotation.target, value.status === 'rejected' ? undefined : context).map(e => prefix + e));
    }
    for (const [n, occ] of value.unlinked.entries()) {
      if (!isObject(occ)) { errors.push(`item ${index} unlinked ${n}: expected object`); continue; }
      if (Object.keys(occ).some(key => !['source', 'location', 'base_uri'].includes(key))) errors.push(`item ${index} unlinked ${n}: unknown occurrence field`);
      errors.push(...occurrenceProblems(occ as unknown as UnlinkedOccurrence, 'urn:wikilean:validation-only', value.status === 'rejected' ? undefined : context).map(e => `item ${index} unlinked ${n}: ${e}`));
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
    let target: string | null = null;
    if (typeof decl === 'string' && decl.trim() && typeof module === 'string' && module.trim()
      && !jsonProblem(decl) && !jsonProblem(module) && module.split('.').every(part => part && !/[\s/\\#?]/u.test(part))) {
      target = 'https://leanprover-community.github.io/mathlib4_docs/' + module.split('.').map(encodeURIComponent).join('/') + '.html#' + encodeURIComponent(decl);
    }
    if (!target) diagnostics.push({ item: index, code: 'NO_FORMAL_TARGET' });
    if (target && original.status === 'not_formalized') diagnostics.push({ item: index, code: 'STATUS_TARGET_CONFLICT' });
    const spans = Array.isArray(original.anchors) && original.anchors.length ? original.anchors : ['anchor' in original ? original.anchor : {}];
    if (!('anchor' in original) && !(Array.isArray(original.anchors) && original.anchors.length)) diagnostics.push({ item: index, code: 'MISSING_ANCHOR' });
    spans.forEach((span, anchorIndex) => {
      // Unknown original fields remain in the private archive, not accidentally
      // promoted into a public extension. Known selector strings retain meaning.
      const keys = ['type', 'value', 'section', 'snippet', 'from', 'from_snippet', 'to', 'to_snippet', 'to_math', 'to_alttext'];
      const valid = isObject(span) && keys.every(key => span[key] === undefined || (typeof span[key] === 'string' && !jsonProblem(span[key])));
      if (!valid) diagnostics.push({ item: index, anchor: anchorIndex, code: 'INVALID_ANCHOR' });
      const anchor = valid ? Object.fromEntries(keys.filter(key => key in span).map(key => [key, span[key]])) : {};
      const occurrence: UnlinkedOccurrence = { source: { type: 'generic', uri }, location: { profile: ANCHOR_PROFILE, anchor, resolution: valid ? 'not_attempted' : 'unresolved' } };
      if (target && valid) item.links.push({ id: `${original.id || 'item' + index}:link:${anchorIndex}`, annotation: { ...occurrence, target } });
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
export function storedCompanionRecords(items: JsonObject[]): Array<NativeLink & { item_id?: string }> {
  return items.flatMap(item => isNativeItem(item) && item.status !== 'rejected'
    ? item.links.map(link => ({ ...copy(link), ...(typeof item.id === 'string' ? { item_id: item.id } : {}) })) : []);
}
