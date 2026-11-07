/** Pure structural migration shared by the copied-database rehearsal and D1.
 * No source fetching, inference, re-anchoring, or status coercion happens here. */
import { NATIVE_SCHEMA_VERSION, migrateLegacyItems, validateNativeItems, type JsonObject } from './native-companion.js';
import { MAX_ANNOTATIONS, MAX_ANNOTATIONS_BYTES, MAX_FIELD_LEN, MAX_TEXT_LEN } from './validation.js';

export interface MigrationArticle {
  slug: string; wikipedia_title: string; revid: number | null;
  annotations: string; version: number; schema_version: number;
}
export function missingMigrationIdInput(slug: string, index: number, item: unknown): string {
  return `${slug}\0${index}\0${JSON.stringify(item)}`;
}
function assertWritable(items: JsonObject[]): void {
  if (items.length > MAX_ANNOTATIONS || JSON.stringify(items).length > MAX_ANNOTATIONS_BYTES) throw new Error('native snapshot exceeds Worker save limits');
  for (const item of items) for (const [key, cap] of Object.entries({ kind: MAX_FIELD_LEN, match_kind: MAX_FIELD_LEN, label: MAX_TEXT_LEN, note: MAX_TEXT_LEN, proof_note: MAX_TEXT_LEN })) {
    if (typeof item[key] === 'string' && item[key].length > cap) throw new Error(`native ${key} exceeds Worker save limits`);
  }
}
export function planNativeMigration(row: MigrationArticle, missingId: (item: unknown, index: number) => string) {
  if (![3, NATIVE_SCHEMA_VERSION].includes(row.schema_version)) throw new Error(`${row.slug}: unsupported article schema version ${row.schema_version}`);
  if (!Number.isSafeInteger(row.version) || row.version < 1 || !Number.isSafeInteger(row.version + 1)) throw new Error('unsafe article version');
  const original = JSON.parse(row.annotations) as JsonObject[];
  if (!Array.isArray(original)) throw new Error(`${row.slug}: annotations must be an array`);
  const context = { wikipediaTitle: row.wikipedia_title, revid: row.revid };
  const migrated = row.schema_version !== NATIVE_SCHEMA_VERSION;
  const converted = migrated ? migrateLegacyItems(original.map((item, index) => ({
    ...item, id: item?.id ?? missingId(item, index),
  })), context) : { items: original, diagnostics: [] };
  if (migrated) converted.items.forEach((item, index) => { item.legacy = original[index]; });
  const errors = validateNativeItems(converted.items, context);
  if (errors.length) throw new Error(`${row.slug}: ${errors.join('; ')}`);
  assertWritable(converted.items);
  const counts = { formalized: 0, partial: 0, not_formalized: 0 };
  for (const item of converted.items) if (typeof item.status === 'string' && item.status in counts) counts[item.status as keyof typeof counts]++;
  return {
    items: converted.items, annotations: migrated ? JSON.stringify(converted.items) : row.annotations, counts,
    report: { slug: row.slug, version: row.version + (migrated ? 1 : 0), migrated,
      items: converted.items.length,
      links: converted.items.reduce((n, item) => n + (item.links as unknown[]).length, 0),
      unlinked: converted.items.reduce((n, item) => n + (item.unlinked as unknown[]).length, 0),
      diagnostics: converted.diagnostics },
  };
}
