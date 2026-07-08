import { readFileSync, readdirSync, existsSync } from "node:fs";
import { resolve } from "node:path";

// Builds the initial article rows from the existing static-site artifacts:
//   site/annotations/*.json   — annotation sidecars (dedup by slug, keep richest)
//   site/cache/<slug>.meta.json — pinned Wikipedia revision id
//   catalog/data/articles.jsonl — title → Wikidata QID

export interface SeedRow {
  slug: string;
  wikipedia_title: string;
  display_title: string;
  wikidata_qid: string | null;
  revid: number | null;
  annotations: string; // JSON-serialized annotation array
}

const STATUS = new Set(["formalized", "partial", "not_formalized"]);

export function buildSeedRows(siteDir: string): SeedRow[] {
  const annotDir = resolve(siteDir, "annotations");
  const cacheDir = resolve(siteDir, "cache");
  const catalog = resolve(siteDir, "..", "catalog", "data", "articles.jsonl");
  const qidMap = loadQidMap(catalog);

  // Dedup by slug, keeping the file with the most status-tagged annotations
  // (mirrors build_index.py's dedup so draft/.agent1 variants don't win).
  const best = new Map<string, { count: number; model: Record<string, unknown> }>();
  for (const f of readdirSync(annotDir)) {
    if (f.startsWith(".")) continue;
    if (f.endsWith(".agent1.json")) continue;
    if (!f.endsWith(".json")) continue;
    let model: Record<string, unknown>;
    try {
      model = JSON.parse(readFileSync(resolve(annotDir, f), "utf8"));
    } catch {
      continue;
    }
    const slug = (model.slug as string) || f.slice(0, -5);
    const anns = Array.isArray(model.annotations) ? (model.annotations as Array<Record<string, unknown>>) : [];
    const count = anns.filter((a) => STATUS.has(a.status as string)).length;
    const prev = best.get(slug);
    if (!prev || count > prev.count) best.set(slug, { count, model: { ...model, slug } });
  }

  const rows: SeedRow[] = [];
  for (const [slug, { model }] of best) {
    const title = (model.wikipedia_title as string) || slug.replace(/_/g, " ");
    rows.push({
      slug,
      wikipedia_title: title,
      display_title: (model.display_title as string) || title,
      wikidata_qid: qidMap.get(title) ?? null,
      revid: readRevid(cacheDir, slug),
      annotations: JSON.stringify(model.annotations ?? []),
    });
  }
  rows.sort((a, b) => a.slug.localeCompare(b.slug));
  return rows;
}

function loadQidMap(path: string): Map<string, string> {
  const map = new Map<string, string>();
  if (!existsSync(path)) return map;
  for (const line of readFileSync(path, "utf8").split("\n")) {
    if (!line.trim()) continue;
    try {
      const rec = JSON.parse(line);
      if (rec.title && rec.wikidata_qid) map.set(rec.title, rec.wikidata_qid);
    } catch {
      /* skip malformed line */
    }
  }
  return map;
}

function readRevid(cacheDir: string, slug: string): number | null {
  const p = resolve(cacheDir, `${slug}.meta.json`);
  if (!existsSync(p)) return null;
  try {
    const meta = JSON.parse(readFileSync(p, "utf8"));
    return typeof meta.revid === "number" ? meta.revid : null;
  } catch {
    return null;
  }
}
