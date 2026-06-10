import { readFileSync, readdirSync, existsSync } from "node:fs";
import { resolve } from "node:path";
import { describe, it, expect } from "vitest";
import { absolutizeWikipediaUrls, wrapAnnotations } from "../src/engine/wrap.js";
import type { Annotation } from "../src/engine/types.js";

// Golden test: for every article where we have the cached Wikipedia HTML, the
// canonical annotations, AND a render.py-produced page, assert the TS engine
// reproduces the exact wrapped <main> body that render.py emitted.

const SITE = resolve(process.cwd(), "../site");
const CACHE = resolve(SITE, "cache");
const ANNOT = resolve(SITE, "annotations");
const OUT = resolve(SITE, "out");

const MAIN_OPEN = '<main class="wl-article-body">\n';
const MAIN_CLOSE = "\n</main>";

function goldenSlugs(): string[] {
  const ann = new Set(
    readdirSync(ANNOT).filter((f) => f.endsWith(".json")).map((f) => f.slice(0, -5)),
  );
  const slugs: string[] = [];
  for (const f of readdirSync(OUT)) {
    if (!f.endsWith(".html")) continue;
    const slug = f.slice(0, -5);
    if (["index", "concepts", "about", "404"].includes(slug)) continue;
    if (!ann.has(slug)) continue;
    if (!existsSync(resolve(CACHE, `${slug}.html`))) continue;
    slugs.push(slug);
  }
  return slugs.sort();
}

function expectedBody(slug: string): string | null {
  const out = readFileSync(resolve(OUT, `${slug}.html`), "utf8");
  const start = out.indexOf(MAIN_OPEN);
  if (start === -1) return null;
  const bodyStart = start + MAIN_OPEN.length;
  const bodyEnd = out.lastIndexOf(MAIN_CLOSE);
  if (bodyEnd === -1 || bodyEnd < bodyStart) return null;
  return out.slice(bodyStart, bodyEnd);
}

function actualBody(slug: string): string {
  const cache = readFileSync(resolve(CACHE, `${slug}.html`), "utf8");
  const model = JSON.parse(readFileSync(resolve(ANNOT, `${slug}.json`), "utf8"));
  const annotations: Annotation[] = model.annotations ?? [];
  const src = absolutizeWikipediaUrls(cache);
  return wrapAnnotations(src, annotations).html;
}

function firstDiff(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) if (a[i] !== b[i]) return i;
  return a.length === b.length ? -1 : n;
}

describe("engine golden parity with render.py", () => {
  const slugs = goldenSlugs();

  it("has golden inputs", () => {
    expect(slugs.length).toBeGreaterThan(200);
  });

  it("reproduces render.py output byte-for-byte", () => {
    const failures: Array<{ slug: string; at: number; a: string; b: string; lenA: number; lenB: number }> = [];
    let compared = 0;
    for (const slug of slugs) {
      const expected = expectedBody(slug);
      if (expected === null) continue;
      compared += 1;
      const actual = actualBody(slug);
      if (actual !== expected) {
        const at = firstDiff(actual, expected);
        failures.push({
          slug,
          at,
          lenA: actual.length,
          lenB: expected.length,
          a: actual.slice(Math.max(0, at - 60), at + 60),
          b: expected.slice(Math.max(0, at - 60), at + 60),
        });
      }
    }
    if (failures.length) {
      const total = slugs.length;
      // eslint-disable-next-line no-console
      console.log(`\nGOLDEN FAIL: ${failures.length}/${total} articles differ\n`);
      for (const f of failures.slice(0, 8)) {
        // eslint-disable-next-line no-console
        console.log(
          `--- ${f.slug}  (first diff @${f.at}, lenTS=${f.lenA} lenPY=${f.lenB})\n` +
            `  TS: …${JSON.stringify(f.a)}…\n` +
            `  PY: …${JSON.stringify(f.b)}…\n`,
        );
      }
    }
    expect(failures.map((f) => f.slug)).toEqual([]);
    expect(compared).toBeGreaterThan(200);
  });
});
