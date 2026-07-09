// Anchor-resolution engine — a faithful TypeScript port of site/render.py.
// Given MediaWiki-parsed article HTML and a list of annotations, it locates each
// annotation's target span and wraps it in a <span|div class="anno ..."> marker,
// reproducing render.py byte-for-byte (verified by golden tests).
//
// Offsets are UTF-16 code-unit indices (JS-native). render.py uses Python
// code-point indices; the two agree for all BMP text, and for astral chars the
// final sliced output is still identical because slices always fall on char
// boundaries.

import { htmlEscape, htmlUnescape } from "./html.js";
import type { Annotation, Anchor, WrapperTag } from "./types.js";

// ---- Python str.find / rfind with start/end bounds ----

function pyFind(src: string, sub: string, start = 0, end = src.length): number {
  const idx = src.indexOf(sub, start);
  if (idx === -1 || idx + sub.length > end) return -1;
  return idx;
}

function pyRFind(src: string, sub: string, start: number, end: number): number {
  const from = end - sub.length;
  if (from < start) return -1;
  const idx = src.lastIndexOf(sub, from);
  return idx >= start ? idx : -1;
}

// ---- ranged regex iteration (emulates Python finditer(src, pos, endpos)) ----

function* finditer(re: RegExp, src: string, start = 0, end = src.length): Generator<RegExpExecArray> {
  const flags = re.flags.includes("g") ? re.flags : re.flags + "g";
  const g = new RegExp(re.source, flags);
  g.lastIndex = start;
  let m: RegExpExecArray | null;
  while ((m = g.exec(src)) !== null) {
    if (m.index >= end) break;
    if (m.index + m[0].length > end) {
      g.lastIndex = m.index + 1;
      continue;
    }
    yield m;
    if (m[0].length === 0) g.lastIndex++;
  }
}

function reSearch(re: RegExp, src: string, start = 0, end = src.length): RegExpExecArray | null {
  for (const m of finditer(re, src, start, end)) return m;
  return null;
}

function isUpper(ch: string): boolean {
  return /\p{Lu}/u.test(ch);
}

// ---- absolutize_wikipedia_urls ----

export function absolutizeWikipediaUrls(body: string): string {
  body = body.replace(/((?:src|href|srcset)=")\/\//g, "$1https://");
  body = body.replace(/(,\s*)\/\//g, "$1https://");
  body = body.replace(/((?:src|href|srcset)=")(\/(?:wiki|w)\/)/g, "$1https://en.wikipedia.org$2");
  return body;
}

// ---- theorem_box ----

const THEOREM_BOX_OPEN = /<div class="math(?:_|&#95;)theorem"[^>]*>/i;

function findTheoremBox(src: string, needle: string): [number, number] | null {
  for (const m of finditer(THEOREM_BOX_OPEN, src)) {
    let depth = 1;
    let pos = m.index + m[0].length;
    while (depth > 0 && pos < src.length) {
      const nxOpen = src.indexOf("<div", pos);
      const nxClose = src.indexOf("</div>", pos);
      if (nxClose === -1) break;
      if (nxOpen !== -1 && nxOpen < nxClose) {
        depth += 1;
        pos = nxOpen + 4;
      } else {
        depth -= 1;
        pos = nxClose + "</div>".length;
      }
    }
    if (depth !== 0) continue;
    const inner = src.slice(m.index + m[0].length, pos - "</div>".length);
    let text = inner.replace(/<[^>]+>/g, " ");
    text = htmlUnescape(text);
    text = text.replace(/\s+/g, " ").trim();
    if (text.includes(needle)) return [m.index, pos];
  }
  return null;
}

// ---- wikitext markup strip ----

function stripWikitextMarkup(s: string): string {
  s = s.replace(/\[\[([^\]|]+)\|([^\]]+)\]\]/g, "$2");
  s = s.replace(/\[\[([^\]]+)\]\]/g, "$1");
  s = s.replace(/'''([^']+)'''/g, "$1");
  s = s.replace(/''([^']+)''/g, "$1");
  s = s.replace(/\{\{(?:math|mvar|nowrap)\|([^}]+)\}\}/gi, "$1");
  s = s.replace(/\{\{[^}]*\}\}/g, "");
  s = s.replace(/&\w+;/g, " ");
  s = s.replace(/\s+/g, " ").trim();
  return s;
}

// ---- section bounds ----

const H_HEADING_RE = /<(h[234])\b[^>]*>([\s\S]*?)<\/\1>/i;

function findSectionBounds(src: string, section: string): [number, number] | null {
  const mDis = section.trim().match(/^(.*?)\s*\((\d+)\)$/);
  let base: string;
  let occurrence: number;
  if (mDis) {
    base = mDis[1].trim().toLowerCase();
    occurrence = parseInt(mDis[2], 10);
  } else {
    base = section.trim().toLowerCase();
    occurrence = 1;
  }

  if (base === "(lead)") {
    const headings = [...finditer(H_HEADING_RE, src)];
    const first = headings.length ? headings[0].index : src.length;
    return [0, first];
  }

  const headings = [...finditer(H_HEADING_RE, src)];
  for (const exactOnly of [true, false]) {
    let seen = 0;
    for (let idx = 0; idx < headings.length; idx++) {
      const m = headings[idx];
      let headText = m[2].replace(/<[^>]+>/g, " ");
      headText = headText.replace(/\s+/g, " ").trim().toLowerCase();
      const match = exactOnly ? headText === base : headText.includes(base);
      if (match) {
        seen += 1;
        if (seen === occurrence) {
          const sectionPos = m.index + m[0].length;
          const level = parseInt(m[1][1], 10);
          let sectionEnd = src.length;
          for (let n = idx + 1; n < headings.length; n++) {
            if (parseInt(headings[n][1][1], 10) <= level) {
              sectionEnd = headings[n].index;
              break;
            }
          }
          return [sectionPos, sectionEnd];
        }
      }
    }
  }
  return null;
}

// ---- block close finder ----

function closeBlock(src: string, openPos: number, closeTag: string, depthTrack: boolean): number | null {
  let pos = src.indexOf(">", openPos);
  if (pos === -1) return null;
  pos += 1;
  if (!depthTrack) {
    const end = src.indexOf(closeTag, pos);
    return end !== -1 ? end + closeTag.length : null;
  }
  let depth = 1;
  while (depth > 0 && pos < src.length) {
    const nxOpen = src.indexOf("<div", pos);
    const nxClose = src.indexOf(closeTag, pos);
    if (nxClose === -1) return null;
    if (nxOpen !== -1 && nxOpen < nxClose) {
      depth += 1;
      pos = nxOpen + 4;
    } else {
      depth -= 1;
      pos = nxClose + closeTag.length;
    }
  }
  return pos;
}

// ---- plain-text projection of an HTML region ----

const MATH_ELEMENT_RE = /<math\b[^>]*>[\s\S]*?<\/math>/gi;

function blockPlainText(src: string, start: number, end: number): string {
  const body = src.slice(start, end).replace(MATH_ELEMENT_RE, " ");
  let t = body.replace(/<[^>]+>/g, " ");
  t = htmlUnescape(t);
  return t.replace(/\s+/g, " ").trim();
}

interface PlainTextMap {
  text: string;
  starts: number[];
  ends: number[];
}

function buildPlainTextMap(src: string, start: number, end: number, lowercase = true): PlainTextMap {
  const chars: string[] = [];
  const starts: number[] = [];
  const ends: number[] = [];
  let inTag = false;
  let lastWasSpace = true;

  const push = (ch: string, s: number, e: number): void => {
    const cp = ch.codePointAt(0)!;
    if (/\s/.test(ch) || cp === 0x00a0 || cp === 0x2009 || cp === 0x200b) {
      if (lastWasSpace) return;
      chars.push(" ");
      starts.push(s);
      ends.push(e);
      lastWasSpace = true;
    } else {
      chars.push(lowercase ? ch.toLowerCase() : ch);
      starts.push(s);
      ends.push(e);
      lastWasSpace = false;
    }
  };

  let i = start;
  while (i < end) {
    const c = src[i];
    if (c === "<") {
      if (
        src.slice(i, i + 5).toLowerCase() === "<math" &&
        i + 5 < src.length &&
        (/\s/.test(src[i + 5]) || src[i + 5] === ">")
      ) {
        const close = pyFind(src, "</math>", i, end);
        if (close === -1) break;
        i = close + "</math>".length;
        continue;
      }
      inTag = true;
      i += 1;
      continue;
    }
    if (c === ">") {
      inTag = false;
      i += 1;
      continue;
    }
    if (inTag) {
      i += 1;
      continue;
    }
    if (c === "&") {
      const semi = pyFind(src, ";", i, Math.min(i + 16, end));
      if (semi !== -1) {
        const entity = src.slice(i, semi + 1);
        const decoded = htmlUnescape(entity);
        if (decoded !== entity) {
          for (let k = 0; k < decoded.length; k++) push(decoded[k], i, semi + 1);
          i = semi + 1;
          continue;
        }
      }
    }
    push(c, i, i + 1);
    i += 1;
  }

  return { text: chars.join(""), starts, ends };
}

function findPlainTextRange(src: string, start: number, end: number, snippet: string): [number, number] | null {
  const snippetNorm = snippet.replace(/\s+/g, " ").trim().toLowerCase();
  if (!snippetNorm) return null;
  const { text, starts, ends } = buildPlainTextMap(src, start, end, true);
  const idx = text.indexOf(snippetNorm);
  if (idx === -1) return null;
  return [starts[idx], ends[idx + snippetNorm.length - 1]];
}

function findSentenceRange(src: string, blockStart: number, blockEnd: number, snippet: string): [number, number] | null {
  const snippetNorm = snippet.replace(/\s+/g, " ").trim().toLowerCase();
  if (!snippetNorm) return null;
  const { text: plain, starts: pStarts, ends: pEnds } = buildPlainTextMap(src, blockStart, blockEnd, false);
  const idx = plain.toLowerCase().indexOf(snippetNorm);
  if (idx === -1) return null;

  let sentStart = 0;
  for (let i = idx - 1; i > 0; i--) {
    if (
      plain[i] === " " &&
      i > 0 &&
      ".!?".includes(plain[i - 1]) &&
      i + 1 < plain.length &&
      isUpper(plain[i + 1])
    ) {
      sentStart = i + 1;
      break;
    }
  }

  const snippetEnd = idx + snippetNorm.length;
  let sentEnd = plain.length;
  for (let i = snippetEnd; i < plain.length; i++) {
    if (".!?".includes(plain[i])) {
      const rest = plain.slice(i + 1);
      if (rest.length === 0 || (rest[0] === " " && (rest.length < 2 || isUpper(rest[1])))) {
        sentEnd = i + 1;
        break;
      }
    }
  }

  sentEnd = Math.min(sentEnd, pEnds.length);
  return [pStarts[sentStart], pEnds[sentEnd - 1]];
}

function matchedEndsWithColon(src: string, htmlStart: number, htmlEnd: number): boolean {
  const { text: plain } = buildPlainTextMap(src, htmlStart, htmlEnd, false);
  return plain.replace(/\s+$/, "").endsWith(":");
}

function findFollowingList(src: string, afterPos: number, limit: number): number | null {
  let p = afterPos;
  while (p < limit && /\s/.test(src[p])) p += 1;
  if (p >= limit) return null;
  const m = src.slice(p, p + 6).match(/^<(ul|ol|dl)\b/i);
  if (!m) return null;
  const tag = m[1].toLowerCase();
  const closeTag = `</${tag}>`;
  const close = pyFind(src, closeTag, p, limit);
  if (close === -1) return null;
  return close + closeTag.length;
}

// Extends a prose-block wrap over any display-math blocks that immediately
// follow it (skipping whitespace only). Captures the natural "definition +
// equation" pattern: a paragraph whose meaning includes the equation below it.
// Recognized shapes:
//   <span class="mwe-math-element mwe-math-element-block">…</span>
//   <dl>…</dl>  where the content contains mwe-math-element-block (MediaWiki's
//                `:`-indented equation form).
function findFollowingDisplayMath(src: string, afterPos: number, limit: number): number | null {
  const DISPLAY_MATH_OPEN = `<span class="mwe-math-element mwe-math-element-block">`;
  let p = afterPos;
  let endPos: number | null = null;
  while (p < limit) {
    while (p < limit && /\s/.test(src[p])) p += 1;
    if (p >= limit) break;
    // display-math span
    if (src.startsWith(DISPLAY_MATH_OPEN, p)) {
      const close = src.indexOf("</span>", p + DISPLAY_MATH_OPEN.length);
      if (close === -1 || close >= limit) break;
      endPos = close + "</span>".length;
      p = endPos;
      continue;
    }
    // <dl> whose body contains display math (indented equation form)
    const dlMatch = src.slice(p, p + 64).match(/^<dl\b[^>]*>/i);
    if (dlMatch) {
      const innerStart = p + dlMatch[0].length;
      const close = src.indexOf("</dl>", innerStart);
      if (close === -1 || close >= limit) break;
      if (!src.slice(innerStart, close).includes("mwe-math-element-block")) break;
      endPos = close + "</dl>".length;
      p = endPos;
      continue;
    }
    break;
  }
  return endPos;
}

const INLINE_TAGS = ["a", "b", "i", "em", "strong", "span", "sup", "q", "cite", "dfn", "code"];

function findProseBlock(src: string, section: string, snippet: string): [number, number, WrapperTag] | null {
  const bounds = findSectionBounds(src, section);
  if (!bounds) return null;
  const [sectionPos, sectionEnd] = bounds;

  const mathStripped = snippet.replace(/\[MATH\]/g, " ");
  const rawKeys = [snippet, stripWikitextMarkup(snippet), mathStripped];
  const seen = new Set<string>();
  const keys: string[] = [];
  for (const k of rawKeys) {
    const kk = k.replace(/\s+/g, " ").trim();
    if (kk && !seen.has(kk)) {
      seen.add(kk);
      keys.push(kk);
    }
  }
  const keysLower = keys.map((k) => k.toLowerCase());

  // 1. inline elements — prefer the smallest plain-text content
  const inlineMatches: Array<[number, number, number]> = []; // [contentLen, start, end]
  for (const tag of INLINE_TAGS) {
    const openRe = new RegExp(`<${tag}(?:\\s[^>]*)?>`, "i");
    const closeTag = `</${tag}>`;
    for (const m of finditer(openRe, src, sectionPos, sectionEnd)) {
      const ep = pyFind(src, closeTag, m.index + m[0].length, sectionEnd);
      if (ep === -1) continue;
      const content = blockPlainText(src, m.index + m[0].length, ep);
      const cl = content.toLowerCase();
      if (keysLower.some((k) => cl.includes(k))) {
        inlineMatches.push([content.length, m.index, ep + closeTag.length]);
      }
    }
  }
  if (inlineMatches.length) {
    inlineMatches.sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2]);
    const [, s, e] = inlineMatches[0];
    return [s, e, "span"];
  }

  // 2. block elements
  const blockOpenRe = /<(p|blockquote|dl|dd|li|div(?=\s+class="math(?:_|&#95;)theorem"))\b[^>]*>/i;
  for (const m of finditer(blockOpenRe, src, sectionPos, sectionEnd)) {
    const tag = m[1].toLowerCase();
    let end: number | null;
    if (tag === "div") {
      end = closeBlock(src, m.index, "</div>", true);
    } else {
      const closeTag = `</${tag}>`;
      const ep = pyFind(src, closeTag, m.index + m[0].length, sectionEnd);
      end = ep !== -1 ? ep + closeTag.length : null;
    }
    if (end === null) continue;
    const closeTag = `</${tag}>`;
    const blockText = blockPlainText(src, m.index + m[0].length, end - closeTag.length);
    const btl = blockText.toLowerCase();
    if (keysLower.some((k) => btl.includes(k))) {
      for (const k of keys) {
        const rng = findSentenceRange(src, m.index, end, k);
        if (rng) {
          if (matchedEndsWithColon(src, rng[0], rng[1])) {
            // ":" introduces a list — extend the wrap to absorb it.
            const listEnd = findFollowingList(src, end, sectionEnd);
            if (listEnd !== null) return [m.index, listEnd, "div"];
            // ":" also introduces a display equation ("we have:" + <dl><math>).
            const mathEnd = findFollowingDisplayMath(src, end, sectionEnd);
            if (mathEnd !== null) return [m.index, mathEnd, "div"];
          }
          // Wrap the sentence range as-is. Display math nested inside the
          // sentence (e.g. "If x = y, then …") used to force a promotion to a
          // whole-block <div>, which dragged in sibling sentences in the same
          // <p> — the snippet-edit bug. We don't need the promotion any more:
          // the .anno-X .mwe-math-element-block CSS rule paints the equation
          // itself, so a <span> sentence wrap with a display:block child still
          // reads as one continuously highlighted statement.
          return [rng[0], rng[1], "span"];
        }
      }
      // Block-fallback wrap: just the block. (Older versions absorbed
      // immediately-following display math here too, but that captured
      // unrelated equations; per-equation math_alttext anchors handle math
      // coverage now.)
      return [m.index, end, "div"];
    }
  }
  return null;
}

function findMathSpan(src: string, alttextValue: string): [number, number] | null {
  const candidates = [alttextValue, htmlEscape(alttextValue, false), htmlEscape(alttextValue, true)];
  let needlePos = -1;
  for (const cand of candidates) {
    needlePos = src.indexOf(`alttext="${cand}"`);
    if (needlePos !== -1) break;
  }
  if (needlePos === -1) return null;

  const openPos = pyRFind(src, '<span class="mwe-math-element', 0, needlePos);
  if (openPos === -1) return null;

  let mathEnd = src.indexOf("</math>", needlePos);
  if (mathEnd === -1) return null;
  mathEnd += "</math>".length;

  const span1 = src.indexOf("</span>", mathEnd);
  if (span1 === -1) return null;
  const span2 = src.indexOf("</span>", span1 + "</span>".length);
  if (span2 === -1) return null;
  return [openPos, span2 + "</span>".length];
}

const BLOCK_OPEN_RE = /<(p|div|dl|blockquote|ul|ol|table|pre|figure|h[1-6])\b[^>]*>/i;

function* iterTopLevelBlocks(src: string, sectionPos: number, sectionEnd: number): Generator<[number, number]> {
  let pos = sectionPos;
  while (pos < sectionEnd) {
    const m = reSearch(BLOCK_OPEN_RE, src, pos, sectionEnd);
    if (!m) return;
    const tag = m[1].toLowerCase();
    const openRe = new RegExp(`<${tag}\\b[^>]*>`, "i");
    const closeTag = `</${tag}>`;
    let depth = 1;
    let cursor = m.index + m[0].length;
    while (depth > 0 && cursor < sectionEnd) {
      const nxClose = pyFind(src, closeTag, cursor, sectionEnd);
      if (nxClose === -1) break;
      const nxOpenM = reSearch(openRe, src, cursor, nxClose);
      if (nxOpenM) {
        depth += 1;
        cursor = nxOpenM.index + nxOpenM[0].length;
      } else {
        depth -= 1;
        cursor = nxClose + closeTag.length;
      }
    }
    if (depth === 0) {
      yield [m.index, cursor];
      pos = cursor;
    } else {
      return;
    }
  }
}

function findProseRange(
  src: string,
  section: string,
  fromSnippet: string,
  toMathAlttext?: string,
  toSnippet?: string,
): [number, number, WrapperTag] | null {
  const bounds = findSectionBounds(src, section);
  if (!bounds) return null;
  const [sectionPos, sectionEnd] = bounds;

  let rng = findPlainTextRange(src, sectionPos, sectionEnd, fromSnippet);
  if (!rng) {
    const stripped = stripWikitextMarkup(fromSnippet);
    if (stripped && stripped !== fromSnippet) {
      rng = findPlainTextRange(src, sectionPos, sectionEnd, stripped);
    }
    if (!rng) return null;
  }
  const fromPos = rng[0];

  let endPos: number | null = null;
  if (toMathAlttext) {
    const m = findMathSpan(src, toMathAlttext);
    if (m && m[0] >= fromPos) endPos = m[1];
  }
  if (endPos === null && toSnippet) {
    const toRng = findPlainTextRange(src, fromPos, sectionEnd, toSnippet);
    if (toRng) endPos = toRng[1];
  }
  if (endPos === null) return null;

  const blocks = [...iterTopLevelBlocks(src, sectionPos, sectionEnd)];
  const first = blocks.find((b) => b[0] <= fromPos && fromPos < b[1]) ?? null;
  let last: [number, number] | null = null;
  for (let i = blocks.length - 1; i >= 0; i--) {
    if (blocks[i][0] < endPos && endPos <= blocks[i][1]) {
      last = blocks[i];
      break;
    }
  }
  if (first && last && (first[0] !== last[0] || first[1] !== last[1])) {
    return [first[0], last[1], "div"];
  }
  return [fromPos, endPos, "span"];
}

// ---- nested-edit application ----

const STATUS_PRIORITY: Record<string, number> = { formalized: 0, partial: 1, not_formalized: 2 };

type Edit = [number, number, string, string]; // start, end, openTag, closeTag

interface EditNode {
  start: number;
  end: number;
  open: string;
  close: string;
  children: EditNode[];
}

function applyNestedEdits(src: string, edits: Edit[]): string {
  if (!edits.length) return src;
  const sorted = [...edits].sort((a, b) => a[0] - b[0] || b[1] - a[1]);

  const root: EditNode = { start: 0, end: src.length, open: "", close: "", children: [] };
  const stack: EditNode[] = [root];
  for (const [start, end, openTag, closeTag] of sorted) {
    while (stack[stack.length - 1] !== root && stack[stack.length - 1].end <= start) stack.pop();
    const parent = stack[stack.length - 1];
    if (end > parent.end) {
      // partial overlap — would produce invalid nesting; drop it.
      console.warn(`dropping edit (${start}, ${end}) — partially overlaps (${parent.start}, ${parent.end})`);
      continue;
    }
    const node: EditNode = { start, end, open: openTag, close: closeTag, children: [] };
    parent.children.push(node);
    stack.push(node);
  }

  const render = (node: EditNode): string => {
    const parts: string[] = [node.open];
    let cursor = node.start;
    for (const child of node.children) {
      parts.push(src.slice(cursor, child.start));
      parts.push(render(child));
      cursor = child.end;
    }
    parts.push(src.slice(cursor, node.end));
    parts.push(node.close);
    return parts.join("");
  };

  return render(root);
}

export interface WrapResult {
  html: string;
  matched: boolean[];
}

export function wrapAnnotations(bodyHtml: string, annotations: Annotation[]): WrapResult {
  const matched = new Array<boolean>(annotations.length).fill(false);

  // group key "start:end" → list of [annotationIndex, wrapperTag]
  const groups = new Map<string, Array<[number, WrapperTag]>>();
  const groupRange = new Map<string, [number, number]>();

  for (let i = 0; i < annotations.length; i++) {
    const a = annotations[i];
    // Human-deletion tombstone: status="rejected" is a human veto, so no wrap
    // is ever emitted for it. matched[i] is reported as `true` (meaning
    // "excluded, not an anchor failure") because every consumer of matched[]
    // treats `false` as anchor rot — the save response's "X/Y anchored"
    // warning (index.ts) and render.py's unmatched warning would otherwise
    // flag every veto forever. page.ts independently excludes rejected
    // annotations from the badge / display-math counts and from the anonymous
    // client data. Mirrored in render.py wrap_annotations (golden parity).
    if (a.status === "rejected") {
      matched[i] = true;
      continue;
    }
    const anchors: Anchor[] = a.anchors && a.anchors.length ? a.anchors : [a.anchor ?? {}];
    for (const anchor of anchors) {
      const t = anchor.type;
      let loc: [number, number, WrapperTag] | null = null;
      if (t === "math_alttext") {
        const r = findMathSpan(bodyHtml, anchor.value ?? "");
        if (!r) continue;
        loc = [r[0], r[1], "span"];
      } else if (t === "theorem_box") {
        const r = findTheoremBox(bodyHtml, anchor.value ?? "");
        if (!r) continue;
        loc = [r[0], r[1], "div"];
      } else if (t === "prose_range") {
        const r = findProseRange(
          bodyHtml,
          anchor.section ?? "",
          anchor.from ?? anchor.from_snippet ?? "",
          anchor.to_math ?? anchor.to_alttext,
          anchor.to ?? anchor.to_snippet,
        );
        if (!r) continue;
        loc = r;
      } else if (anchor.section !== undefined && anchor.snippet !== undefined) {
        const r = findProseBlock(bodyHtml, anchor.section, anchor.snippet);
        if (!r) continue;
        loc = r;
      } else {
        continue;
      }
      matched[i] = true;
      const key = `${loc[0]}:${loc[1]}`;
      if (!groups.has(key)) {
        groups.set(key, []);
        groupRange.set(key, [loc[0], loc[1]]);
      }
      groups.get(key)!.push([i, loc[2]]);
    }
  }

  const edits: Edit[] = [];
  for (const [key, members] of groups) {
    const [start, end] = groupRange.get(key)!;
    const indices = members.map((m) => m[0]);
    const wrapper: WrapperTag = members.some((m) => m[1] === "div") ? "div" : "span";
    // Agent-1-era extracted annotations can lack status. Keep parity with
    // render.py by treating them as the conservative lowest-priority bucket.
    const statuses = indices.map((i) => annotations[i].status);
    const rep = [...statuses].sort((x, y) => (STATUS_PRIORITY[x] ?? 99) - (STATUS_PRIORITY[y] ?? 99))[0] || "not_formalized";
    let decl: string | null = null;
    for (const i of indices) {
      const a = annotations[i];
      const d = (a.mathlib && a.mathlib.decl) || a.decl;
      if (d) {
        decl = d;
        break;
      }
    }
    // Representative provenance: any human-authored annotation in the group
    // promotes the wrap to "human-curated". Anything else is treated as AI.
    const provs = indices.map((i) => annotations[i].provenance);
    const repProv = provs.includes("human") ? "human" : "ai";
    // Annotation-derived fields (status, provenance, decl) are authored by
    // logged-in users and the wrapped output is cached and served to anonymous
    // readers, so every value reaching the attribute string must be
    // attribute-escaped to prevent stored XSS. `indices` is numeric (join of
    // array indices) and needs no escaping.
    const repEsc = htmlEscape(rep, true);
    const repProvEsc = htmlEscape(repProv, true);
    const attrs = [
      `class="anno anno-${repEsc}"`,
      `data-status="${repEsc}"`,
      `data-provenance="${repProvEsc}"`,
      `data-anno-indices="${indices.join(",")}"`,
    ];
    if (decl) attrs.push(`data-decl="${htmlEscape(decl, true)}"`);
    const openTag = `<${wrapper} ${attrs.join(" ")}>`;
    const closeTag = `</${wrapper}>`;
    edits.push([start, end, openTag, closeTag]);
  }

  return { html: applyNestedEdits(bodyHtml, edits), matched };
}
