// The articles plane's contract fixture for Worker tests: the golden store that
// scripts/articles/build_store.py builds from scripts/articles/fixtures/recordset
// (its Python test pins that the committed store equals a fresh build), served
// through a minimal R2-shaped bucket so the real ARTICLES code path runs.

import { createHash } from "node:crypto";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Env } from "../../src/env.js";

export const ARTICLES_STORE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../scripts/articles/fixtures/store");

export const WIDGET_PAGEID = 1001;
export const GADGET_PAGEID = 1003;
export const FIXTURE_CAMPAIGN = "fixture-2026-10";
export const FIXTURE_UNIVERSE_SHA = "0e6efcaae05b9aee11c90b919b55052636ee2932901127ea571268dc3a8d6a25";

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else out.push(path);
  }
  return out;
}

// Store path ("articles/v1/...") → file text.
export function articlesFixtureFiles(): Map<string, string> {
  const files = new Map<string, string>();
  for (const path of walk(ARTICLES_STORE_ROOT)) {
    files.set(relative(ARTICLES_STORE_ROOT, path).split("\\").join("/"), readFileSync(path, "utf8"));
  }
  return files;
}

export interface FakeBucket {
  get(key: string): Promise<{ text(): Promise<string> } | null>;
  gets: string[];
}

export function fakeBucket(files: Map<string, string>): FakeBucket {
  const gets: string[] = [];
  return {
    gets,
    async get(key: string) {
      gets.push(key);
      const value = files.get(key);
      return value === undefined ? null : { text: async () => value };
    },
  };
}

// Binds ARTICLES on a harness env; returns the bucket so tests can count reads.
export function bindArticles(env: Env, files: Map<string, string> = articlesFixtureFiles()): FakeBucket {
  const bucket = fakeBucket(files);
  (env as unknown as { ARTICLES: FakeBucket }).ARTICLES = bucket;
  return bucket;
}

export function fixtureArticle(pageid = WIDGET_PAGEID): Record<string, any> {
  return JSON.parse(articlesFixtureFiles().get(`articles/v1/articles/${pageid}.json`)!);
}

// ---- the /2 store the community overlay needs ----------------------------------------------
//
// The committed fixture is a cf-locate-recordset/1 store: it carries neither
// passage_key nor pair_key, and the tag routes refuse it by design (a /1 set
// cannot take an overlay — docs/ARTICLES.md, "Community tags"). These helpers
// derive a /2 shape from it in the test process alone, so the Python builder's
// golden fixture is untouched: every passage gets a passage_key and every
// location the pair_key the server recomputes from it, by the producer's own
// formulas (scripts/articles/build_store.py passage_key / pair_key).

const sha256 = (s: string) => createHash("sha256").update(s, "utf8").digest("hex");

// passage_key = sha256("<pageid>\n<span_sha256>"). The fixture records no span
// hash in its view model, so the test stands one in, deterministically.
export function passageKeyFor(pageid: number, passageId: string): string {
  return sha256(`${pageid}\n${sha256(passageId)}`);
}

// pair_key = sha256("<passage_key>\n<repository>\n<declaration>"), the
// repository being the Lean end's URI — NOT the library id.
export function pairKeyFor(passageKey: string, repository: string, declaration: string): string {
  return sha256(`${passageKey}\n${repository}\n${declaration}`);
}

export const MATHLIB_REPO = "https://github.com/leanprover-community/mathlib4";

// The golden store with the /2 keys and a /2 seal. `status_rule` 2 leaves every
// location's own status unchanged (only rule 3, R-BROAD, changes one), so the
// published statuses still hold.
export function articlesPairedFiles(files: Map<string, string> = articlesFixtureFiles()): Map<string, string> {
  const out = new Map(files);
  for (const pageid of [WIDGET_PAGEID, GADGET_PAGEID]) {
    const key = `articles/v1/articles/${pageid}.json`;
    const article = JSON.parse(out.get(key)!);
    article.seal = {
      set_id: "fixture-set",
      schema: "cf-locate-recordset/2",
      status_rule: 2,
      created: "2026-10-17T12:00:00Z",
      skill_commit: "3bceabcee0724ffdf50ce65dde3ab10048c72cba",
    };
    for (const passage of article.passages) {
      passage.passage_key = passageKeyFor(pageid, passage.id);
      for (const loc of passage.locations) {
        loc.pair_key = pairKeyFor(passage.passage_key, loc.repo, loc.declaration);
      }
    }
    out.set(key, JSON.stringify(article));
  }
  return out;
}

// One passage of the /2 fixture, by page id and index in the view model.
export function pairedPassage(pageid: number, index: number): Record<string, any> {
  const files = articlesPairedFiles();
  const article = JSON.parse(files.get(`articles/v1/articles/${pageid}.json`)!);
  return article.passages[index];
}

// ---- the article reader (build_store --reader-html) ----------------------------------------
//
// A synthetic marked revision of the Widget lemma fixture (page 1001), in the shape the builder
// stores as reader/<pageid>.html: MediaWiki-like parse HTML, each passage's text wrapped in
// <mark class="atl" data-p="<item hex>" data-status="<status>">, the first mark of each passage
// with id="p-<hex>". The passages read in another order than the store lists them (the
// definition comes first), and the bound's text runs through a link, so it is three marks. The
// conjecture has no status (it is unresolved in the store), so it is never marked: the builder
// refuses a mark on it, and the view lists it as unplaced.
// Every value is made up; the builder's own tests check the real contract.

export const READER_ORDER = ["3fa91c07", "91aa02de", "5b0e44a1"];
// The passage with no status, which the pages set aside.
export const NO_STATUS_HEX = "0c4d77b1";

const mark = (hex: string, status: string, text: string, first = true) =>
  `<mark class="atl" data-p="${hex}" data-status="${status}"${first ? ` id="p-${hex}"` : ""}>${text}</mark>`;

// Two formulas outside the passages, as Wikipedia's MathML writes them: a double-struck R (which
// MathML Core draws as a plain R) and an operator name with its function application.
const formula = (tex: string, mathml: string) =>
  `<span class="mwe-math-element mwe-math-element-inline"><span class="mwe-math-mathml-inline mwe-math-mathml-a11y">` +
  `<math xmlns="http://www.w3.org/1998/Math/MathML" alttext="{\\displaystyle ${tex}}"><semantics><mrow class="MJX-TeXAtom-ORD">` +
  `<mstyle displaystyle="true" scriptlevel="0">${mathml}</mstyle></mrow>` +
  `<annotation encoding="application/x-tex">{\\displaystyle ${tex}}</annotation></semantics></math></span></span>`;
const MATH_R = formula("\\mathbb {R} ", `<mrow class="MJX-TeXAtom-ORD"><mi mathvariant="double-struck">R</mi></mrow>`);
const MATH_LOG = formula("\\log n", `<mi>log</mi><mo>&#x2061;</mo><mi>n</mi>`);

export function readerHtml(omit: string[] = []): string {
  const m = (hex: string, status: string, text: string, first = true) => (omit.includes(hex) ? text : mark(hex, status, text, first));
  return `<div class="mw-parser-output"><style data-mw-deduplicate="TemplateStyles:r1">.mw-parser-output .hatnote{font-style:italic;padding-left:1.6em}</style>
<div role="note" class="hatnote navigation-not-searchable">This article is about widgets. For gadgets, see <a href="/articles/1003/Gadget_(mathematics)">Gadget (mathematics)</a>.</div>
<p>In combinatorics, the <b>widget lemma</b> states that ${m("3fa91c07", "formalized", "every finite widget admits a spanning gadget")} of minimal degree.<sup class="reference" id="cite_ref-1"><a href="#cite_note-1">[1]</a></sup></p>
<h2 id="Definitions">Definitions</h2>
<p>Following Smith, ${m("91aa02de", "not_formalized", "a widget is balanced if every gadget has equal weight")}.</p>
<p>A widget over ${MATH_R} has at most ${MATH_LOG} gadgets.</p>
<h2 id="Statement">Statement</h2>
<p>${m("5b0e44a1", "statement_only", "Moreover, the number of ")}<a href="https://en.wikipedia.org/wiki/Spanning_tree" target="_blank" rel="noopener noreferrer">${m("5b0e44a1", "statement_only", "spanning gadgets", false)}</a>${m("5b0e44a1", "statement_only", " of a widget is at most its rank.", false)}</p>
<h2 id="Open_problems">Open problems</h2>
<p>It is conjectured that every balanced widget is the image of a regular one.</p>
<h2 id="References">References</h2>
<ol class="references"><li id="cite_note-1"><span class="reference-text">A. Smith, <i>Widgets</i> (2020).</span></li></ol>
</div>`;
}

// The golden store with the Widget lemma's marked revision: reader/1001.html and the view's
// `reader` (its sha256, as the page checks). `omit` leaves passages unmarked (unplaced);
// `html` stores other bytes than the view names (a store written in part).
export function articlesReaderFiles(opts: { omit?: string[]; html?: string } = {}): Map<string, string> {
  const files = articlesFixtureFiles();
  const html = readerHtml(opts.omit ?? []);
  const article = fixtureArticle(WIDGET_PAGEID);
  const unplaced = article.passages
    .filter((p: { item: string }) => [...(opts.omit ?? []), NO_STATUS_HEX].some((hex) => p.item.endsWith(`#${hex}`)))
    .map((p: { id: string }) => p.id);
  article.reader = {
    sha256: createHash("sha256").update(html, "utf8").digest("hex"),
    bytes: Buffer.byteLength(html, "utf8"),
    marks: (html.match(/<mark class="atl"/g) ?? []).length,
    passages: article.passages.length - unplaced.length,
    unplaced,
  };
  files.set(`articles/v1/articles/${WIDGET_PAGEID}.json`, JSON.stringify(article));
  files.set(`articles/v1/reader/${WIDGET_PAGEID}.html`, opts.html ?? html);
  return files;
}
