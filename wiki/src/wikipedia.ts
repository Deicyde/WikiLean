// Fetches MediaWiki-parsed article HTML, pinned to a revision id, cached in KV.
// Mirrors render.py.fetch_article_html: prop=text|revid in one call. (KV rather
// than R2 because R2 isn't enabled on the account; values are well under KV's
// 25 MB limit and immutable per revid.)

const API = "https://en.wikipedia.org/w/api.php";
const UA = "WikiLean/1.0 (https://wikilean.jackmccarthy.org; jack.mccarthy.1@stonybrook.edu)";

export interface WpResult {
  html: string;
  revid: number | null;
}

function kvKey(slug: string, revid: number | null): string {
  return `wp:${slug}:${revid ?? "latest"}`;
}

export async function getWikipediaHtml(
  kv: KVNamespace,
  slug: string,
  wikipediaTitle: string,
  revid: number | null,
): Promise<WpResult> {
  if (revid !== null) {
    const cached = await kv.get(kvKey(slug, revid));
    if (cached) return { html: cached, revid };
  }

  const params = new URLSearchParams({
    action: "parse",
    prop: "text|revid",
    format: "json",
    formatversion: "2",
    redirects: "1",
  });
  if (revid !== null) params.set("oldid", String(revid));
  else params.set("page", wikipediaTitle);

  const resp = await fetch(`${API}?${params.toString()}`, { headers: { "User-Agent": UA } });
  if (!resp.ok) throw new Error(`MediaWiki HTTP ${resp.status}`);
  const data = (await resp.json()) as { parse?: { text?: string; revid?: number } };
  const text = data.parse?.text;
  if (!text) throw new Error("MediaWiki returned no parse block");
  const resolvedRevid = data.parse?.revid ?? revid;

  // 90d TTL: entries are immutable per revid but re-fetchable by oldid, so a
  // TTL caps unbounded (slug × revid) growth; re-renders re-warm the cache.
  await kv.put(kvKey(slug, resolvedRevid), text, { expirationTtl: 60 * 60 * 24 * 90 });
  return { html: text, revid: resolvedRevid };
}
