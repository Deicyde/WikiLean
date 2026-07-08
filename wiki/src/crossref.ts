export interface CrossRefSpec {
  db: string;
  attr: string;
  label: string;
  idPattern: RegExp;
  idSource: string;
  urlPrefix: string;
  queueKey: string;
}

export const CROSSREF_SPECS: Record<string, CrossRefSpec> = {
  wikidata: {
    db: "wikidata",
    attr: "wikidata",
    label: "Wikidata",
    idPattern: /^Q\d+$/,
    idSource: "Q\\d+",
    urlPrefix: "https://www.wikidata.org/wiki/",
    queueKey: "wikidata:queue",
  },
  lmfdb: {
    db: "lmfdb",
    attr: "lmfdb",
    label: "LMFDB",
    idPattern: /^[a-z0-9_.]+$/,
    idSource: "[a-z0-9_.]+",
    urlPrefix: "https://www.lmfdb.org/knowledge/show/",
    queueKey: "crossref:lmfdb:queue",
  },
};

export function crossRefSpec(db: string | null | undefined): CrossRefSpec | null {
  const key = (db || "wikidata").toLowerCase();
  return CROSSREF_SPECS[key] ?? null;
}

export function crossRefDb(db: string | null | undefined): string {
  return crossRefSpec(db)?.db ?? "wikidata";
}

export function crossRefUrl(db: string, id: string): string {
  const spec = crossRefSpec(db) ?? CROSSREF_SPECS.wikidata;
  return spec.urlPrefix + encodeURIComponent(id);
}

export function crossRefTagRegex(db: string): RegExp {
  const spec = crossRefSpec(db) ?? CROSSREF_SPECS.wikidata;
  return new RegExp(`\\b${spec.attr}\\s+(${spec.idSource})\\b`);
}

export function crossRefReviewMarker(db: string, id: string): string {
  return `wikilean-review:${crossRefDb(db)}:${id}`;
}

export function crossRefBotMarkerRegex(db: string): RegExp {
  const spec = crossRefSpec(db) ?? CROSSREF_SPECS.wikidata;
  if (spec.db === "wikidata") return /crossref-bot:(?:wikidata:)?(Q\d+)/;
  return new RegExp(`crossref-bot:${spec.db}:(${spec.idSource})\\b`);
}

export function crossRefReviewMarkerRegex(db: string): RegExp {
  const spec = crossRefSpec(db) ?? CROSSREF_SPECS.wikidata;
  if (spec.db === "wikidata") return /wikilean-review:(?:wikidata:)?(Q\d+)/;
  return new RegExp(`wikilean-review:${spec.db}:(${spec.idSource})\\b`);
}
