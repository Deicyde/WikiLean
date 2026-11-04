// Formulas in an atlas quote (docs/ATLAS.md, "UI": the quote).
//
// A record's quote is a verbatim slice of the producer's text layer (formal-companion
// web-html/3), which is the article HTML's textContent. A Wikipedia formula appears in
// it twice: first as the MathML token text, one token per element with a space between
// ("w : G ~ → G"), then as its TeX annotation ("{\displaystyle w:{\tilde {G}}\to G}").
// Shown as it stands, a reader sees both, and a text fragment built from it matches
// nothing on the rendered page, which shows neither.
//
// quoteSegments splits a quote into prose and formulas: each annotation, with the
// MathML tokens in front of it (found by matching the tokens' letters and digits
// against the TeX's, from the annotation backwards), is one formula, rendered once from
// its TeX (mathHtml: Unicode letters and symbols, <sup>/<sub>, stacked fractions,
// matrices, cases and aligned rows laid out as rows and columns with stretched
// delimiters; the TeX itself is the formula's title). A quote the export cut inside a formula keeps
// what it has: a truncated annotation renders as far as it goes, and a run of MathML
// tokens whose annotation lies past the cut renders from the tokens. The store and the
// JSON downloads keep the quote verbatim; only the page renders it.
//
// quoteFragment builds the "in context" text fragment from the prose alone (the first
// and last runs of words), so it matches the rendered article whatever its math
// rendering. Everything here returns escaped HTML or a URL-encoded string.

import { htmlEscape } from "./engine/html.js";

const esc = (s: string) => htmlEscape(s, false);
const attr = (s: string) => htmlEscape(s, true);

// Characters the layer carries that render as nothing (MathML's function application
// and its kin, the zero-width space): whitespace for tokenizing, dropped from text.
const INVISIBLE = /[\u2061-\u2064\u200b]/g;
const SPLIT = /[\s\u2061-\u2064\u200b]+/u;

// ---- TeX to a small tree -------------------------------------------------------

type MClass = "var" | "num" | "ord" | "op" | "rel" | "punct" | "open" | "close" | "fn" | "big" | "text" | "space";
type MNode =
  | { t: "atom"; c: MClass; v: string }
  | { t: "group"; body: MNode[] }
  | { t: "scripts"; base: MNode | null; sub: MNode[] | null; sup: MNode[] | null }
  | { t: "frac"; num: MNode[]; den: MNode[]; line: boolean }
  | { t: "sqrt"; body: MNode[]; index: MNode[] | null }
  | { t: "accent"; mark: string; shown: string; body: MNode[] }
  | { t: "font"; variant: Variant; body: MNode[] }
  | { t: "env"; name: string; rows: MNode[][][]; spec: string };
type Variant = "bb" | "cal" | "frak" | "bf" | "rm" | "it" | "sf" | "tt" | "cancel";

type Tok =
  | { k: "cw"; v: string } | { k: "cs"; v: string } | { k: "ch"; v: string }
  | { k: "raw"; cmd: string; v: string }
  | { k: "open" } | { k: "close" } | { k: "sup" } | { k: "sub" } | { k: "amp" };

// Commands whose braced argument is text, read with its spaces.
const RAW_COMMANDS = new Set(["text", "mbox", "textrm", "textit", "textbf", "textsf", "texttt", "textnormal", "operatorname", "hbox"]);

// The balanced-brace group that starts at s[i] === "{": its end index (past "}") or -1.
function groupEnd(s: string, i: number): number {
  let depth = 0;
  for (let j = i; j < s.length; j++) {
    const ch = s[j];
    if (ch === "\\") { j++; continue; }
    if (ch === "{") depth++;
    else if (ch === "}") { depth--; if (depth === 0) return j + 1; }
  }
  return -1;
}

function tokenize(tex: string): Tok[] {
  const toks: Tok[] = [];
  let i = 0;
  while (i < tex.length) {
    const ch = tex[i];
    if (ch === "\\") {
      const m = /^[A-Za-z]+/.exec(tex.slice(i + 1));
      if (m) {
        const name = m[0];
        i += 1 + name.length;
        while (i < tex.length && /\s/.test(tex[i])) i++;
        if (RAW_COMMANDS.has(name) && tex[i] === "{") {
          const end = groupEnd(tex, i);
          const stop = end < 0 ? tex.length : end - 1;
          toks.push({ k: "raw", cmd: name, v: tex.slice(i + 1, stop) });
          i = end < 0 ? tex.length : end;
          continue;
        }
        toks.push({ k: "cw", v: name });
      } else if (i + 1 < tex.length) {
        toks.push({ k: "cs", v: tex[i + 1] });
        i += 2;
      } else {
        i++;
      }
      continue;
    }
    if (ch === "{") toks.push({ k: "open" });
    else if (ch === "}") toks.push({ k: "close" });
    else if (ch === "^") toks.push({ k: "sup" });
    else if (ch === "_") toks.push({ k: "sub" });
    else if (ch === "&") toks.push({ k: "amp" });
    else if (ch === "~") toks.push({ k: "cs", v: " " });
    else if (!/\s/.test(ch)) {
      const cp = tex.codePointAt(i) as number;
      const c = String.fromCodePoint(cp);
      toks.push({ k: "ch", v: c });
      i += c.length;
      continue;
    }
    i++;
  }
  return toks;
}

const GREEK: Record<string, string> = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ϵ", varepsilon: "ε", zeta: "ζ", eta: "η",
  theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ", varkappa: "ϰ", lambda: "λ", mu: "μ", nu: "ν", xi: "ξ",
  omicron: "ο", pi: "π", varpi: "ϖ", rho: "ρ", varrho: "ϱ", sigma: "σ", varsigma: "ς", tau: "τ",
  upsilon: "υ", phi: "ϕ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω", digamma: "ϝ",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π", Sigma: "Σ", Upsilon: "Υ",
  Phi: "Φ", Psi: "Ψ", Omega: "Ω",
};

// Symbol commands by class.
const SYMBOLS: Record<string, [MClass, string]> = {
  to: ["rel", "→"], rightarrow: ["rel", "→"], leftarrow: ["rel", "←"], gets: ["rel", "←"],
  Rightarrow: ["rel", "⇒"], Leftarrow: ["rel", "⇐"], Leftrightarrow: ["rel", "⇔"], leftrightarrow: ["rel", "↔"],
  iff: ["rel", "⟺"], implies: ["rel", "⟹"], impliedby: ["rel", "⟸"], mapsto: ["rel", "↦"],
  longmapsto: ["rel", "⟼"], longrightarrow: ["rel", "⟶"], longleftarrow: ["rel", "⟵"],
  Longrightarrow: ["rel", "⟹"], Longleftrightarrow: ["rel", "⟺"], longleftrightarrow: ["rel", "⟷"],
  hookrightarrow: ["rel", "↪"], hookleftarrow: ["rel", "↩"], twoheadrightarrow: ["rel", "↠"],
  rightleftharpoons: ["rel", "⇌"], uparrow: ["rel", "↑"], downarrow: ["rel", "↓"], nearrow: ["rel", "↗"],
  searrow: ["rel", "↘"], rightrightarrows: ["rel", "⇉"],
  in: ["rel", "∈"], notin: ["rel", "∉"], ni: ["rel", "∋"], owns: ["rel", "∋"],
  subset: ["rel", "⊂"], subseteq: ["rel", "⊆"], subsetneq: ["rel", "⊊"], supset: ["rel", "⊃"],
  supseteq: ["rel", "⊇"], supsetneq: ["rel", "⊋"], sqsubseteq: ["rel", "⊑"], sqsupseteq: ["rel", "⊒"],
  leq: ["rel", "≤"], le: ["rel", "≤"], leqslant: ["rel", "⩽"], geq: ["rel", "≥"], ge: ["rel", "≥"],
  geqslant: ["rel", "⩾"], neq: ["rel", "≠"], ne: ["rel", "≠"], ll: ["rel", "≪"], gg: ["rel", "≫"],
  approx: ["rel", "≈"], equiv: ["rel", "≡"], cong: ["rel", "≅"], sim: ["rel", "∼"], simeq: ["rel", "≃"],
  propto: ["rel", "∝"], mid: ["rel", "∣"], nmid: ["rel", "∤"], parallel: ["rel", "∥"], perp: ["rel", "⊥"],
  prec: ["rel", "≺"], succ: ["rel", "≻"], preceq: ["rel", "⪯"], succeq: ["rel", "⪰"], vdash: ["rel", "⊢"],
  models: ["rel", "⊨"], dashv: ["rel", "⊣"], asymp: ["rel", "≍"], doteq: ["rel", "≐"], triangleq: ["rel", "≜"],
  lhd: ["rel", "⊲"], unlhd: ["rel", "⊴"], rhd: ["rel", "⊳"], trianglelefteq: ["rel", "⊴"], triangleleft: ["rel", "◁"],
  colon: ["punct", ":"],
  times: ["op", "×"], cdot: ["op", "⋅"], circ: ["op", "∘"], ast: ["op", "∗"], star: ["op", "⋆"],
  pm: ["op", "±"], mp: ["op", "∓"], div: ["op", "÷"], oplus: ["op", "⊕"], ominus: ["op", "⊖"],
  otimes: ["op", "⊗"], odot: ["op", "⊙"], cup: ["op", "∪"], cap: ["op", "∩"], setminus: ["op", "∖"],
  smallsetminus: ["op", "∖"], sqcup: ["op", "⊔"], sqcap: ["op", "⊓"], uplus: ["op", "⊎"], wedge: ["op", "∧"],
  land: ["op", "∧"], vee: ["op", "∨"], lor: ["op", "∨"], bullet: ["op", "∙"], dagger: ["ord", "†"],
  rtimes: ["op", "⋊"], ltimes: ["op", "⋉"], wr: ["op", "≀"], amalg: ["op", "⨿"], bmod: ["op", "mod"],
  infty: ["ord", "∞"], partial: ["ord", "∂"], nabla: ["ord", "∇"], forall: ["ord", "∀"], exists: ["ord", "∃"],
  nexists: ["ord", "∄"], neg: ["ord", "¬"], lnot: ["ord", "¬"], emptyset: ["ord", "∅"], varnothing: ["ord", "∅"],
  ell: ["var", "ℓ"], hbar: ["var", "ℏ"], aleph: ["var", "ℵ"], beth: ["var", "ℶ"], wp: ["var", "℘"],
  Re: ["ord", "ℜ"], Im: ["ord", "ℑ"], imath: ["var", "ı"], jmath: ["var", "ȷ"], prime: ["ord", "′"],
  angle: ["ord", "∠"], triangle: ["ord", "△"], square: ["ord", "□"], top: ["ord", "⊤"], bot: ["ord", "⊥"],
  surd: ["ord", "√"], S: ["ord", "§"], P: ["ord", "¶"], checkmark: ["ord", "✓"], circledast: ["op", "⊛"],
  ldots: ["ord", "…"], dots: ["ord", "…"], dotsc: ["ord", "…"], dotsb: ["ord", "⋯"], cdots: ["ord", "⋯"],
  vdots: ["ord", "⋮"], ddots: ["ord", "⋱"],
  langle: ["open", "⟨"], rangle: ["close", "⟩"], lfloor: ["open", "⌊"], rfloor: ["close", "⌋"],
  lceil: ["open", "⌈"], rceil: ["close", "⌉"], lbrace: ["open", "{"], rbrace: ["close", "}"],
  lbrack: ["open", "["], rbrack: ["close", "]"], lvert: ["open", "|"], rvert: ["close", "|"],
  lVert: ["open", "‖"], rVert: ["close", "‖"], vert: ["ord", "|"], Vert: ["ord", "‖"], backslash: ["ord", "\\"],
  sum: ["big", "∑"], prod: ["big", "∏"], coprod: ["big", "∐"], int: ["big", "∫"], iint: ["big", "∬"],
  iiint: ["big", "∭"], oint: ["big", "∮"], bigcup: ["big", "⋃"], bigcap: ["big", "⋂"], bigoplus: ["big", "⨁"],
  bigotimes: ["big", "⨂"], bigvee: ["big", "⋁"], bigwedge: ["big", "⋀"], bigsqcup: ["big", "⨆"],
  quad: ["space", "\u2003"], qquad: ["space", "\u2003\u2003"], thinspace: ["space", "\u2009"], enspace: ["space", "\u2002"],
  sharp: ["ord", "♯"], flat: ["ord", "♭"], natural: ["ord", "♮"], blacksquare: ["ord", "■"], Box: ["ord", "□"],
  upharpoonright: ["op", "↾"], restriction: ["op", "↾"], thicksim: ["rel", "∼"], thickapprox: ["rel", "≈"],
  dotsm: ["ord", "⋯"], dotsi: ["ord", "⋯"], diamond: ["op", "⋄"], lozenge: ["ord", "◊"], varpropto: ["rel", "∝"],
  nleq: ["rel", "≰"], ngeq: ["rel", "≱"], coloneqq: ["rel", "≔"], leftharpoonup: ["rel", "↼"], rightharpoonup: ["rel", "⇀"],
};

// Upright function names (texvc's set, plus its sgn and mod).
const FUNCS = new Set([
  "arccos", "arcsin", "arctan", "arccot", "arcsec", "arccsc", "arg", "cos", "cosh", "cot", "coth", "csc", "csch",
  "deg", "det", "dim", "exp", "gcd", "lcm", "hom", "inf", "ker", "lg", "lim", "liminf", "limsup", "ln", "log",
  "max", "min", "Pr", "sec", "sech", "sin", "sinh", "sup", "tan", "tanh", "sgn", "sen", "mod", "injlim", "projlim",
  "argmax", "argmin", "Tr", "tr", "rank", "span", "diag", "id",
]);

// Commands with nothing to show (style and sizing switches).
const IGNORED = new Set([
  "displaystyle", "textstyle", "scriptstyle", "scriptscriptstyle", "limits", "nolimits", "big", "Big", "bigg", "Bigg",
  "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr", "Biggl", "Biggr", "bigm", "Bigm", "middle", "nonumber",
  "notag", "strut", "mathstrut", "displaylimits", "allowbreak", "nobreak", "relax",
]);

const FONTS: Record<string, Variant> = {
  mathbb: "bb", Bbb: "bb", mathbbm: "bb", mathcal: "cal", mathscr: "cal", mathfrak: "frak", mathbf: "bf",
  bf: "bf", boldsymbol: "bf", bm: "bf", mathbold: "bf", mathrm: "rm", rm: "rm", mathit: "it", it: "it",
  mathsf: "sf", sf: "sf", mathtt: "tt", tt: "tt", mathup: "rm",
};

// Accents: the combining mark the page draws, and the mark MathML's token text shows.
const ACCENTS: Record<string, [string, string]> = {
  tilde: ["\u0303", "~"], widetilde: ["\u0303", "~"], hat: ["\u0302", "^"], widehat: ["\u0302", "^"],
  bar: ["\u0304", "¯"], overline: ["\u0305", "¯"], vec: ["\u20d7", "→"], overrightarrow: ["\u20d7", "→"],
  dot: ["\u0307", "˙"], ddot: ["\u0308", "¨"], dddot: ["\u20db", "\u20db"], check: ["\u030c", "ˇ"], breve: ["\u0306", "˘"],
  acute: ["\u0301", "´"], grave: ["\u0300", "`"], mathring: ["\u030a", "˚"], underline: ["\u0332", "_"],
  overleftarrow: ["\u20d6", "←"],
};

const ENV_DELIMS: Record<string, [string, string]> = {
  pmatrix: ["(", ")"], bmatrix: ["[", "]"], Bmatrix: ["{", "}"], vmatrix: ["|", "|"], Vmatrix: ["‖", "‖"],
  matrix: ["", ""], smallmatrix: ["", ""], array: ["", ""], cases: ["{", ""], aligned: ["", ""], align: ["", ""],
  gathered: ["", ""], split: ["", ""], alignat: ["", ""], rcases: ["", "}"], dcases: ["{", ""],
};

// How an environment lays out its cells: a matrix centres them, cases set the value and
// its condition flush left, the align family pairs a right-aligned column with a
// left-aligned one (the "&" sits before the relation), a gather centres one column.
type EnvLayout = "matrix" | "cases" | "align" | "gather" | "array";
const ENV_LAYOUT: Record<string, EnvLayout> = {
  pmatrix: "matrix", bmatrix: "matrix", Bmatrix: "matrix", vmatrix: "matrix", Vmatrix: "matrix", matrix: "matrix",
  smallmatrix: "matrix", cases: "cases", dcases: "cases", rcases: "cases", aligned: "align", align: "align",
  "align*": "align", alignat: "align", "alignat*": "align", alignedat: "align", split: "align", eqnarray: "align",
  "eqnarray*": "align", gathered: "gather", gather: "gather", "gather*": "gather", array: "array", darray: "array",
  subarray: "array",
};
// Environments whose first argument is a column count or a column spec, not a cell.
const ENV_ARGUMENT = new Set(["array", "darray", "subarray", "alignat", "alignat*", "alignedat"]);

// A table's own entry: a TeX command named "constructor" or "toString" is no entry.
function own<T>(table: Record<string, T>, key: string): T | undefined {
  return Object.hasOwn(table, key) ? table[key] : undefined;
}

function charAtom(ch: string): MNode {
  if (/^[A-Za-z]$/.test(ch)) return { t: "atom", c: "var", v: ch };
  if (/^[0-9]$/.test(ch)) return { t: "atom", c: "num", v: ch };
  switch (ch) {
    case "+": return { t: "atom", c: "op", v: "+" };
    case "-": return { t: "atom", c: "op", v: "−" };
    case "*": return { t: "atom", c: "op", v: "∗" };
    case "=": case "<": case ">": case ":": return { t: "atom", c: "rel", v: ch };
    case ",": case ";": return { t: "atom", c: "punct", v: ch };
    case "(": case "[": return { t: "atom", c: "open", v: ch };
    case ")": case "]": return { t: "atom", c: "close", v: ch };
    case "'": return { t: "atom", c: "ord", v: "′" };
    default: return { t: "atom", c: /^\p{L}$/u.test(ch) ? "var" : "ord", v: ch };
  }
}

class Parser {
  private i = 0;
  constructor(private readonly toks: Tok[]) {}

  parse(): MNode[] {
    const out: MNode[] = [];
    while (this.i < this.toks.length) {
      out.push(...this.list({ brace: false, env: false }));
      // A stray "}" or "&" at the top level is skipped, never fatal.
      if (this.i < this.toks.length) this.i++;
    }
    return out;
  }

  private peek(): Tok | undefined { return this.toks[this.i]; }

  // Nodes until the group's "}" (left unconsumed), the end, or, inside an
  // environment, a cell or row break or \end.
  private list(stop: { brace: boolean; env: boolean; bracket?: boolean }): MNode[] {
    const out: MNode[] = [];
    while (this.i < this.toks.length) {
      const t = this.toks[this.i];
      if (t.k === "close") { if (stop.brace) break; this.i++; continue; }
      if (stop.env && (t.k === "amp" || (t.k === "cs" && t.v === "\\") || (t.k === "cw" && t.v === "end"))) break;
      if (stop.bracket && t.k === "ch" && t.v === "]") break;
      if (t.k === "sup" || t.k === "sub") {
        this.i++;
        const arg = this.arg();
        const last = out.length ? out[out.length - 1] : null;
        let node: MNode;
        if (last && last.t === "scripts" && (t.k === "sup" ? last.sup === null : last.sub === null)) {
          node = last;
          out.pop();
        } else {
          node = { t: "scripts", base: last, sub: null, sup: null };
          if (last) out.pop();
        }
        if (node.t === "scripts") {
          if (t.k === "sup") node.sup = arg; else node.sub = arg;
        }
        out.push(node);
        continue;
      }
      // A prime is a superscript in TeX; it reads as one beside its base.
      const atom = this.atom(stop);
      if (atom) {
        // Digits run together into one number, as MathML's <mn> does.
        const last = out[out.length - 1];
        if (atom.t === "atom" && atom.c === "num" && last && last.t === "atom" && last.c === "num") last.v += atom.v;
        // ":=" (and "=:") is one relation, never ": =".
        else if (atom.t === "atom" && atom.c === "rel" && last && last.t === "atom" && last.c === "rel"
          && ((last.v === ":" && atom.v === "=") || (last.v === "=" && atom.v === ":"))) last.v += atom.v;
        else if (atom.t === "atom" && atom.c === "ord" && atom.v === "." && last && last.t === "atom" && last.c === "num"
          && this.peek()?.k === "ch" && /^[0-9]$/.test((this.peek() as { v: string }).v)) last.v += ".";
        else out.push(atom);
      }
    }
    return out;
  }

  // A command's argument: a braced group, or the single next item.
  private arg(): MNode[] {
    const t = this.peek();
    if (!t) return [];
    if (t.k === "open") {
      this.i++;
      const body = this.list({ brace: true, env: false });
      if (this.peek()?.k === "close") this.i++;
      return body;
    }
    if (t.k === "close" || t.k === "amp") return [];
    const atom = this.atom({ brace: true, env: false });
    return atom ? [atom] : [];
  }

  // A row break's optional spacing (\\[4pt], \\[-1ex], \\[5mu]): layout only, never shown.
  private rowSpacing(): void {
    const open = this.peek();
    if (open?.k !== "ch" || open.v !== "[") return;
    let j = this.i + 1, dim = "";
    while (j < this.toks.length && j - this.i <= 12) {
      const t = this.toks[j];
      if (t.k === "ch" && t.v === "]") {
        if (/^-?\d*\.?\d+\s*(?:pt|mu|ex|em|cm|mm|in|bp|pc|sp|dd|cc)$/.test(dim)) this.i = j + 1;
        return;
      }
      if (t.k === "ch") dim += t.v;
      else if (t.k === "cw") dim += t.v;
      else return;
      j++;
    }
  }

  // The raw letters of a braced argument ("pmatrix" of \begin{pmatrix}).
  private rawArg(): string {
    if (this.peek()?.k !== "open") return "";
    this.i++;
    let name = "";
    while (this.i < this.toks.length && this.toks[this.i].k !== "close") {
      const t = this.toks[this.i++];
      if (t.k === "ch") name += t.v;
      else if (t.k === "cw") name += t.v;
    }
    if (this.peek()?.k === "close") this.i++;
    return name;
  }

  private atom(stop: { brace: boolean; env: boolean }): MNode | null {
    const t = this.toks[this.i++];
    switch (t.k) {
      case "open": {
        const body = this.list({ brace: true, env: false });
        if (this.peek()?.k === "close") this.i++;
        return { t: "group", body };
      }
      case "close": return null;
      case "amp": return { t: "atom", c: "space", v: "\u2003" };
      case "sup": case "sub": return null;
      case "ch": return charAtom(t.v);
      case "raw": {
        const value = t.v.replace(/\\[,;:! ]/g, " ").replace(/\\([A-Za-z]+)/g, "$1").replace(/[{}\\$]/g, "");
        if (t.cmd === "operatorname") return { t: "atom", c: "fn", v: value.replace(/\s+/g, "") };
        return { t: "atom", c: "text", v: value };
      }
      case "cs": {
        switch (t.v) {
          case ",": case ":": case ">": case ";": return { t: "atom", c: "space", v: "\u2009" };
          case " ": return { t: "atom", c: "space", v: " " };
          case "!": return null;
          case "{": return { t: "atom", c: "open", v: "{" };
          case "}": return { t: "atom", c: "close", v: "}" };
          case "|": return { t: "atom", c: "ord", v: "‖" };
          case "\\": this.rowSpacing(); return { t: "atom", c: "space", v: " " };
          default: return { t: "atom", c: "ord", v: t.v };
        }
      }
      case "cw": return this.command(t.v, stop);
    }
  }

  private command(name: string, stop: { brace: boolean; env: boolean }): MNode | null {
    if (IGNORED.has(name)) return null;
    if (name === "left" || name === "right") {
      const next = this.peek();
      if (next?.k === "ch" && next.v === ".") { this.i++; return null; }
      const delim = this.atom(stop);
      if (delim && delim.t === "atom" && name === "left" && delim.c !== "open") return { ...delim, c: "open" };
      if (delim && delim.t === "atom" && name === "right" && delim.c !== "close") return { ...delim, c: "close" };
      return delim;
    }
    const greek = own(GREEK, name);
    if (greek) return { t: "atom", c: greek === greek.toLowerCase() ? "var" : "ord", v: greek };
    const symbol = own(SYMBOLS, name);
    if (symbol) return { t: "atom", c: symbol[0], v: symbol[1] };
    if (FUNCS.has(name)) return { t: "atom", c: "fn", v: name };
    const font = own(FONTS, name);
    if (font) return { t: "font", variant: font, body: this.arg() };
    const accent = own(ACCENTS, name);
    if (accent) return { t: "accent", mark: accent[0], shown: accent[1], body: this.arg() };
    switch (name) {
      case "frac": case "dfrac": case "tfrac": case "cfrac": {
        const num = this.arg();
        return { t: "frac", num, den: this.arg(), line: true };
      }
      case "binom": case "dbinom": case "tbinom": case "choose": {
        const num = this.arg();
        const den = this.arg();
        return { t: "group", body: [{ t: "atom", c: "open", v: "(" }, { t: "frac", num, den, line: false }, { t: "atom", c: "close", v: ")" }] };
      }
      case "sqrt": {
        let index: MNode[] | null = null;
        const next = this.peek();
        if (next?.k === "ch" && next.v === "[") {
          this.i++;
          index = this.list({ brace: true, env: false, bracket: true });
          if (this.peek()?.k === "ch") this.i++;
        }
        return { t: "sqrt", body: this.arg(), index };
      }
      case "overset": case "stackrel": {
        const over = this.arg();
        const base = this.arg();
        return { t: "scripts", base: { t: "group", body: base }, sub: null, sup: over };
      }
      case "underset": {
        const under = this.arg();
        const base = this.arg();
        return { t: "scripts", base: { t: "group", body: base }, sub: under, sup: null };
      }
      case "overbrace": case "underbrace": case "boxed": case "mathop": case "mathbin": case "mathrel": case "mathord":
      case "mathopen": case "mathclose": case "mathpunct": case "hphantom": case "displaystyle":
        return { t: "group", body: this.arg() };
      case "phantom": case "vphantom": this.arg(); return null;
      case "cancel": case "bcancel": case "xcancel": case "sout": return { t: "font", variant: "cancel", body: this.arg() };
      case "color": case "mathcolor": this.rawArg(); return null;
      case "textcolor": this.rawArg(); return { t: "group", body: this.arg() };
      case "not": {
        const next = this.atom(stop);
        if (next && next.t === "atom") {
          if (next.v === "=") return { ...next, v: "≠" };
          if (next.v === "∈") return { ...next, v: "∉" };
          return { ...next, v: `${next.v}\u0338` };
        }
        return next;
      }
      case "pmod": {
        const body = this.arg();
        return { t: "group", body: [{ t: "atom", c: "space", v: " " }, { t: "atom", c: "open", v: "(" },
          { t: "atom", c: "fn", v: "mod" }, { t: "atom", c: "space", v: " " }, ...body, { t: "atom", c: "close", v: ")" }] };
      }
      case "pod": return { t: "group", body: [{ t: "atom", c: "open", v: "(" }, ...this.arg(), { t: "atom", c: "close", v: ")" }] };
      case "over": return { t: "atom", c: "ord", v: "/" };
      case "begin": {
        const env = this.rawArg();
        // alignedat's column count and array's column spec are arguments, not a cell.
        const spec = ENV_ARGUMENT.has(env) ? this.rawArg() : "";
        const rows: MNode[][][] = [[]];
        while (this.i < this.toks.length) {
          const cell = this.list({ brace: false, env: true });
          rows[rows.length - 1].push(cell);
          const t = this.peek();
          if (!t) break;
          this.i++;
          if (t.k === "amp") continue;
          if (t.k === "cs") { this.rowSpacing(); rows.push([]); continue; }
          this.rawArg();   // \end{env}
          break;
        }
        return { t: "env", name: env, rows: rows.filter((row) => row.some((cell) => cell.length)), spec };
      }
      case "end": this.rawArg(); return null;
      default:
        // An unknown command shows its name upright, as a reader of the TeX would.
        return { t: "atom", c: "fn", v: name };
    }
  }
}

// The parser recurses once per group, so TeX past these bounds (no Wikipedia
// formula comes near them) is shown as its text rather than parsed.
const MAX_TEX_LENGTH = 4000;
const MAX_TEX_DEPTH = 40;

function parseTex(tex: string): MNode[] {
  let depth = 0, deepest = 0;
  for (const ch of tex) {
    if (ch === "{") deepest = Math.max(deepest, ++depth);
    else if (ch === "}") depth--;
  }
  if (tex.length > MAX_TEX_LENGTH || deepest > MAX_TEX_DEPTH) return [{ t: "atom", c: "text", v: tex }];
  return new Parser(tokenize(tex)).parse();
}

// ---- the tree as HTML ------------------------------------------------------------

function mathAlpha(ch: string, variant: Variant): string {
  const cp = ch.codePointAt(0) as number;
  const upper = cp >= 65 && cp <= 90, lower = cp >= 97 && cp <= 122, digit = cp >= 48 && cp <= 57;
  const from = (base: number, holes: Record<string, number>) => holes[ch] ?? base;
  if (variant === "bb") {
    if (upper) return String.fromCodePoint(from(0x1d538 + cp - 65, { C: 0x2102, H: 0x210d, N: 0x2115, P: 0x2119, Q: 0x211a, R: 0x211d, Z: 0x2124 }));
    if (lower) return String.fromCodePoint(0x1d552 + cp - 97);
    if (digit) return String.fromCodePoint(0x1d7d8 + cp - 48);
  }
  if (variant === "cal") {
    if (upper) return String.fromCodePoint(from(0x1d49c + cp - 65, { B: 0x212c, E: 0x2130, F: 0x2131, H: 0x210b, I: 0x2110, L: 0x2112, M: 0x2133, R: 0x211b }));
    if (lower) return String.fromCodePoint(from(0x1d4b6 + cp - 97, { e: 0x212f, g: 0x210a, o: 0x2134 }));
  }
  if (variant === "frak") {
    if (upper) return String.fromCodePoint(from(0x1d504 + cp - 65, { C: 0x212d, H: 0x210c, I: 0x2111, R: 0x211c, Z: 0x2128 }));
    if (lower) return String.fromCodePoint(0x1d51e + cp - 97);
  }
  return ch;
}

function classOf(node: MNode): MClass | "frac" {
  if (node.t === "atom") return node.c;
  if (node.t === "scripts") return node.base ? classOf(node.base) : "ord";
  if (node.t === "font" || node.t === "accent") return "var";
  if (node.t === "group") return node.body.length === 1 ? classOf(node.body[0]) : "ord";
  return "frac";
}

const UNARY_BEFORE = new Set<string>(["op", "rel", "open", "punct", "big"]);

// `before`: the class of what precedes the list on the line, when it continues an
// expression: "ord" for an aligned row's right-hand cell, which starts at its relation
// (spaced as in the middle), "rel" for a display formula's piece after a relation (a
// leading minus there is a sign).
function renderList(nodes: MNode[], variant: Variant | null, before: MClass | null = null): string {
  let out = "";
  let prev: string | null = before;
  nodes.forEach((node, index) => {
    const cls = classOf(node);
    let piece = renderNode(node, variant);
    if (cls === "op" && node.t === "atom") {
      piece = prev === null || UNARY_BEFORE.has(prev) ? piece : ` ${piece} `;
    } else if (cls === "rel" && node.t === "atom") {
      piece = prev === null ? `${piece} ` : ` ${piece} `;
    } else if (cls === "punct" && node.t === "atom") {
      piece = `${piece} `;
    } else if (cls === "fn" || cls === "big") {
      const next = nodes[index + 1];
      const nextCls = next ? classOf(next) : null;
      if (nextCls && !["open", "punct", "close", "op", "rel", "space"].includes(nextCls)) piece += "\u2009";
    }
    out += piece;
    if (cls !== "space") prev = cls;
  });
  return out;
}

function renderNode(node: MNode, variant: Variant | null): string {
  switch (node.t) {
    case "atom": {
      if (node.c === "var" || node.c === "num") {
        const shown = variant === "bb" || variant === "cal" || variant === "frak"
          ? [...node.v].map((ch) => mathAlpha(ch, variant)).join("") : node.v;
        if (node.c === "var" && (variant === null || variant === "it") && /^[A-Za-zα-ωϑϕϖϱϵℓıȷ]$/u.test(node.v)) {
          return `<i>${esc(shown)}</i>`;
        }
        return esc(shown);
      }
      if (node.c === "text") return `<span class="m-text">${esc(node.v)}</span>`;
      return esc(node.v);
    }
    case "group": return renderList(node.body, variant);
    case "scripts": {
      const base = node.base ? renderNode(node.base, variant) : "";
      // Both scripts: stacked beside the base, the superscript over the subscript.
      if (node.sub && node.sup) {
        return `${base}<span class="m-ss"><span class="m-sup">${renderList(node.sup, variant)}</span>` +
          `<span class="m-sub">${renderList(node.sub, variant)}</span></span>`;
      }
      return base + (node.sub ? `<sub>${renderList(node.sub, variant)}</sub>` : "") +
        (node.sup ? `<sup>${renderList(node.sup, variant)}</sup>` : "");
    }
    case "frac":
      return `<span class="m-frac${node.line ? "" : " is-binom"}"><span class="m-num">${renderList(node.num, variant)}</span>` +
        `<span class="m-den">${renderList(node.den, variant)}</span></span>`;
    case "sqrt":
      return (node.index ? `<sup class="m-root">${renderList(node.index, variant)}</sup>` : "") +
        `√<span class="m-rad">${renderList(node.body, variant)}</span>`;
    case "accent": {
      const body = renderList(node.body, variant);
      const plain = flattenList(node.body);
      if (node.mark === "\u0305" || node.mark === "\u0304") {
        return [...plain].length === 1 ? `${body}${node.mark === "\u0305" ? "\u0304" : node.mark}` : `<span class="m-over">${body}</span>`;
      }
      if (node.mark === "\u0332") return `<span class="m-under">${body}</span>`;
      // The mark is drawn over the body by the stylesheet (::after, from data-mark) at a
      // size that reads at 1x, where a combining tilde over an italic X is a hairline; the
      // combining mark itself follows the body in a visually hidden span, so the text
      // still says "X̃" to a screen reader or a copy.
      const shown = own(ACCENT_SHOWN, node.mark);
      if (shown) {
        const italic = body.startsWith("<i>") && body.endsWith("</i>");
        const arrow = shown === "→" || shown === "←";
        return `<span class="m-acc${italic ? " is-it" : ""}${arrow ? " is-arrow" : ""}" data-mark="${attr(shown)}">${body}<span class="atlas-sr">${node.mark}</span></span>`;
      }
      // A combining mark after the body sits over its last letter.
      return body.endsWith("</i>") ? `${body.slice(0, -4)}${node.mark}</i>` : `${body}${node.mark}`;
    }
    case "font": {
      const body = renderList(node.body, node.variant === "cancel" ? variant : node.variant);
      if (node.variant === "bf") return `<b>${body}</b>`;
      if (node.variant === "sf") return `<span class="m-sf">${body}</span>`;
      if (node.variant === "tt") return `<span class="m-tt">${body}</span>`;
      if (node.variant === "it") return `<i>${body}</i>`;
      if (node.variant === "cancel") return `<span class="m-cancel">${body}</span>`;
      return body;
    }
    case "env": return envHtml(node, variant);
  }
}

// The visible mark an accent draws over its body (the combining mark stays in the text).
const ACCENT_SHOWN: Record<string, string> = {
  "\u0303": "˜", "\u0302": "ˆ", "\u030c": "ˇ", "\u0306": "˘", "\u20d7": "→", "\u20d6": "←", "\u0307": "˙",
  "\u0308": "¨", "\u20db": "⃛", "\u0301": "´", "\u0300": "`", "\u030a": "˚",
};

// A delimiter that stretches over an environment's rows: parentheses, brackets and bars
// are drawn as borders, braces as the glyph scaled to the rows. The glyph is in the text
// either way (visually hidden where a border draws it), so a copy reads "( a b )".
const DELIM_SHAPE: Record<string, string> = { "(": "paren", ")": "paren", "[": "brack", "]": "brack", "|": "vert", "‖": "dvert", "{": "brace", "}": "brace" };
function delimHtml(ch: string, side: "l" | "r", rows: number): string {
  if (!ch) return "";
  const shape = own(DELIM_SHAPE, ch) ?? "brace";
  const glyph = shape === "brace"
    ? `<span class="m-dglyph" style="--m-rows:${Math.max(1, Math.min(rows, 40))}">${esc(ch)}</span>`
    : `<span class="atlas-sr">${esc(ch)}</span>`;
  return `<span class="m-delim" data-d="${shape}" data-side="${side}">${glyph}</span>`;
}

// Each column's alignment: l, c or r.
function columnAlign(layout: EnvLayout, spec: string, column: number): "l" | "c" | "r" {
  if (layout === "align") return column % 2 === 0 ? "r" : "l";
  if (layout === "cases") return "l";
  if (layout === "array") {
    const letters = spec.replace(/[^lcr]/g, "");
    const a = letters[column] ?? "c";
    return a === "l" || a === "r" ? a : "c";
  }
  return "c";
}

// A matrix, cases or aligned rows as rows and columns: an inline table between
// stretched delimiters, its cells in the environment's alignment. The align family's
// right-hand cell starts at its relation and sits tight against its left-hand cell.
function envHtml(node: Extract<MNode, { t: "env" }>, variant: Variant | null): string {
  const [open, close] = own(ENV_DELIMS, node.name) ?? ["", ""];
  const layout = own(ENV_LAYOUT, node.name) ?? "matrix";
  const rows = node.rows.map((row) => `<span class="m-row">${row.map((cell, j) => {
    const align = columnAlign(layout, node.spec, j);
    const tight = layout === "align" && j % 2 === 1;
    return `<span class="m-cell${tight ? " is-tight" : ""}" data-a="${align}">${renderList(cell, variant, tight ? "ord" : null).trim()}</span>`;
  }).join("")}</span>`).join("");
  const small = node.name === "smallmatrix" ? " is-small" : "";
  // Many columns (alignedat's): a phone flows each row instead of a table wider than it.
  const wide = Math.max(0, ...node.rows.map((row) => row.length)) > 4 ? " data-wide=\"\"" : "";
  return `<span class="m-env${small}" data-layout="${layout}"${wide}>${delimHtml(open, "l", node.rows.length)}` +
    `<span class="m-table">${rows}</span>${delimHtml(close, "r", node.rows.length)}</span>`;
}

// One formula as HTML: <span class="atlas-math"> titled with its TeX. A display
// formula is set in pieces that each end at a top-level relation, so a narrow screen
// breaks it after "=" rather than in the middle of a term.
export function mathHtml(tex: string, display = false): string {
  const nodes = parseTex(tex);
  let html: string;
  if (display) {
    const chunks: MNode[][] = [[]];
    nodes.forEach((node, index) => {
      chunks[chunks.length - 1].push(node);
      if (node.t === "atom" && node.c === "rel" && index > 0 && index < nodes.length - 1) chunks.push([]);
    });
    html = chunks.length > 1
      ? chunks.map((chunk, n) => `<span class="m-chunk">${renderList(chunk, null, n > 0 ? "rel" : null).trim()}</span>`).join(" ")
      : renderList(nodes, null);
  } else {
    html = renderList(nodes, null);
  }
  html = html.replace(/<\/i><i>/g, "").trim();
  return `<span class="atlas-math" title="${attr(`TeX: ${tex.trim()}`)}">${html}</span>`;
}

// ---- matching the MathML tokens to the TeX --------------------------------------

// The formula's visible characters in MathML's order (base, then its accent mark;
// a sub- and superscript after their base, the subscript first, as msubsup holds
// them; a fraction's numerator, then its denominator), letters as written: the
// MathML token text shows \mathbb{R} as "R".
function flattenList(nodes: MNode[]): string {
  return nodes.map(flattenNode).join("");
}

function flattenNode(node: MNode): string {
  switch (node.t) {
    case "atom": return node.c === "space" ? "" : node.v;
    case "group": return flattenList(node.body);
    case "scripts": return (node.base ? flattenNode(node.base) : "") + (node.sub ? flattenList(node.sub) : "") + (node.sup ? flattenList(node.sup) : "");
    case "frac": return flattenList(node.num) + flattenList(node.den);
    case "sqrt": return flattenList(node.body) + (node.index ? flattenList(node.index) : "");
    case "accent": return flattenList(node.body) + node.shown;
    case "font": return flattenList(node.body);
    case "env": {
      const [open, close] = own(ENV_DELIMS, node.name) ?? ["", ""];
      return open + node.rows.map((row) => row.map(flattenList).join("")).join("") + close;
    }
  }
}

// Letters and digits after compatibility folding (ℝ → R, ² → 2, ϕ → φ): what the
// MathML tokens and the TeX must agree on.
function alnum(s: string): string[] {
  return [...s.normalize("NFKC")].filter((ch) => /[\p{L}\p{N}]/u.test(ch));
}
const SYMBOL_FOLD: Record<string, string> = { "-": "−", "*": "∗", "'": "′", "·": "⋅", "|": "∣" };
function symbols(s: string): string[] {
  return [...s.normalize("NFKC")].filter((ch) => !/[\p{L}\p{N}\p{M}\s]/u.test(ch)).map((ch) => SYMBOL_FOLD[ch] ?? ch);
}

interface Token { start: number; end: number; text: string }

// Whitespace-separated tokens. MathML's function application (U+2061, between a
// function name and its argument) stays a token of its own, so a run can see it.
function tokensOf(s: string, offset: number): Token[] {
  const out: Token[] = [];
  const re = /[^\s\u200b]+/gu;
  let m: RegExpExecArray | null;
  while ((m = re.exec(s))) out.push({ start: offset + m.index, end: offset + m.index + m[0].length, text: m[0] });
  return out;
}

// How many of `tokens` (from the end) are the formula's MathML tokens, for a whole
// annotation: their letters and digits, read backwards, must be the TeX's, read
// backwards, and run out together; a token of symbols only passes inside the formula
// and, before its first letter, where the TeX leads with the same symbols. null when
// the tokens do not account for the TeX.
function matchBack(tokens: Token[], tex: MNode[]): number | null {
  const flat = flattenList(tex);
  const want = alnum(flat);
  const firstAlnum = [...flat].findIndex((ch) => alnum(ch).length > 0);
  const lead = symbols(firstAlnum < 0 ? flat : [...flat].slice(0, firstAlnum).join(""));
  let p = want.length, q = lead.length, k = tokens.length;
  while (k > 0) {
    const tok = tokens[k - 1].text;
    const a = alnum(tok);
    if (a.length) {
      if (p < a.length || a.some((ch, j) => want[p - a.length + j] !== ch)) break;
      p -= a.length;
      k--;
      continue;
    }
    if (p > 0) { k--; continue; }
    const sym = symbols(tok);
    if (sym.length && q >= sym.length && sym.every((ch, j) => lead[q - sym.length + j] === ch)) {
      q -= sym.length;
      k--;
      continue;
    }
    break;
  }
  if (p > 0 || k === tokens.length) return null;
  return tokens.length - k;
}

// Function names MathML shows as one token, and the symbols that mark a run of
// tokens as a formula.
const MATH_SYMBOL = /[=<>≤≥≠∈∉⊂⊆⊃⊇→←↦⇒⇔↔()[\]{}+−×⋅∘±∑∏∫∞≅≈≡∼∣|:∀∃√∂∇⊕⊗∪∩⟨⟩]/u;
function mathLike(tok: string): boolean {
  const t = tok.replace(INVISIBLE, "");
  if (t === "" || /^\p{L}$/u.test(t) || /^\p{N}+(?:[.,]\p{N}+)?$/u.test(t) || FUNCS.has(t)) return true;
  return !/[\p{L}\p{N}—–]/u.test(t);
}

// Where the math-like tokens at the end of `tokens` begin: a token is math-like on
// its own (mathLike), or as the name in front of a function application ("im", U+2061, "(").
function runStart(tokens: Token[]): number {
  let k = tokens.length;
  while (k > 0 && (mathLike(tokens[k - 1].text) || (k < tokens.length && /^[\u2061-\u2064]+$/.test(tokens[k].text)))) k--;
  return k;
}

// The trailing tokens that look like a formula's MathML tokens: a run of
// math-like tokens holding at least one operator, relation or bracket.
function trailingRun(tokens: Token[]): number {
  const run = tokens.slice(runStart(tokens));
  return run.some((tok) => MATH_SYMBOL.test(tok.text)) ? run.length : 0;
}

// For an annotation the cut truncated: the MathML tokens are whole, the TeX is a
// head, so the tokens' letters must start with the TeX's. The run is the longest
// math-like run before the annotation; null when no start in it fits.
function matchFront(tokens: Token[], tex: MNode[]): number | null {
  const want = alnum(flattenList(tex));
  for (let start = runStart(tokens); start < tokens.length; start++) {
    const have = tokens.slice(start).flatMap((tok) => alnum(tok.text));
    if (want.every((ch, j) => have[j] === ch)) return tokens.length - start;
  }
  return null;
}

// ---- segments ----------------------------------------------------------------------

export type QuoteSegment =
  | { kind: "text"; start: number; end: number; text: string }
  | { kind: "math"; start: number; end: number; tex: string | null; tokens: string; complete: boolean; how: MatchHow };

// How a formula's MathML tokens were found: matched against its whole TeX ("back"),
// against the head of a truncated one ("front"), as the math-like run in front of
// it when neither fits ("run"), or none at all ("none"); "tail" is a run of tokens
// at the cut whose annotation the export left out.
export type MatchHow = "back" | "front" | "run" | "none" | "tail";

const ANNOTATION = /\{\\(?:displaystyle|textstyle|scriptstyle)(?![A-Za-z])/g;

// The quote split into prose and formulas. `cutEnd`: the text stops where the export
// cut it, so it may end inside a formula's tokens.
export function quoteSegments(raw: string, cutEnd = false): QuoteSegment[] {
  const s = String(raw ?? "");
  const out: QuoteSegment[] = [];
  let cursor = 0;
  const pushText = (start: number, end: number) => {
    if (end > start) out.push({ kind: "text", start, end, text: s.slice(start, end) });
  };
  ANNOTATION.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = ANNOTATION.exec(s))) {
    const at = m.index;
    if (at < cursor) continue;
    const close = groupEnd(s, at);
    const end = close < 0 ? s.length : close;
    const inner = s.slice(at + 1, close < 0 ? s.length : close - 1).replace(/^\\(?:displaystyle|textstyle|scriptstyle)\s*/, "");
    const tree = parseTex(inner);
    const tokens = tokensOf(s.slice(cursor, at), cursor);
    let n = close < 0 ? matchFront(tokens, tree) : matchBack(tokens, tree);
    let how: MatchHow = close < 0 ? "front" : "back";
    if (n === null) { n = trailingRun(tokens); how = n > 0 ? "run" : "none"; }
    const start = n > 0 ? tokens[tokens.length - n].start : at;
    pushText(cursor, start);
    out.push({ kind: "math", start, end, tex: inner, tokens: s.slice(start, at).trim(), complete: close >= 0, how });
    cursor = end;
    ANNOTATION.lastIndex = end;
  }
  if (cutEnd) {
    const tokens = tokensOf(s.slice(cursor), cursor);
    const n = trailingRun(tokens);
    if (n > 0) {
      const start = tokens[tokens.length - n].start;
      pushText(cursor, start);
      out.push({ kind: "math", start, end: s.length, tex: null, tokens: s.slice(start).trim(), complete: false, how: "tail" });
      return out;
    }
  }
  pushText(cursor, s.length);
  return out;
}

const TOKEN_REL = new Set(["=", "<", ">", "≤", "≥", "≠", "∈", "∉", "⊂", "⊆", "⊃", "⊇", "→", "←", "↦", "⇒", "⇔", "↔", "≅", "≈", "≡", "∼", ":", "∣"]);
const TOKEN_OP = new Set(["+", "−", "×", "⋅", "∘", "±", "∗"]);

// A formula known only by its MathML tokens (its annotation lies past the cut):
// letters italic, relations and operators spaced, brackets and commas tight.
function tokensHtml(tokens: string): string {
  const parts = tokens.replace(INVISIBLE, " ").split(SPLIT).filter(Boolean);
  let out = "";
  let prevAlnum = false;
  parts.forEach((tok, index) => {
    const isAlnum = /^[\p{L}\p{N}]/u.test(tok);
    let piece = /^\p{L}$/u.test(tok) && !/^[Α-Ω]$/u.test(tok) ? `<i>${esc(tok)}</i>` : esc(tok);
    if (TOKEN_REL.has(tok)) piece = index === 0 ? `${piece} ` : ` ${piece} `;
    else if (TOKEN_OP.has(tok)) piece = index === 0 ? piece : ` ${piece} `;
    else if (tok === ",") piece = ", ";
    else if (isAlnum && prevAlnum) piece = `\u2009${piece}`;
    out += piece;
    prevAlnum = isAlnum;
  });
  return `<span class="atlas-math" title="${attr(`MathML text: ${tokens}`)}">${out.replace(/<\/i><i>/g, "").trim()}</span>`;
}

function segmentHtml(seg: QuoteSegment): string {
  if (seg.kind === "text") return esc(seg.text.replace(INVISIBLE, ""));
  if (seg.tex === null) return tokensHtml(seg.tokens);
  // A truncated annotation is a head of the formula, its MathML tokens all of it:
  // the tokens show more whenever they hold letters or digits the head lacks.
  if (!seg.complete && seg.tokens && alnum(seg.tokens).length > alnum(flattenList(parseTex(seg.tex))).length) {
    return tokensHtml(seg.tokens);
  }
  return mathHtml(seg.tex);
}

export interface RenderedQuote { prefix: string; exact: string; suffix: string }

// The quote's three parts as HTML, each formula rendered once. A formula is drawn in
// the part its annotation starts in, or in the quote itself when it straddles the
// quote's edge (its tokens in the prefix, its TeX in the quote, or the reverse).
export function renderQuote(quote: { prefix?: string; exact: string; suffix?: string }, cutEnd: boolean): RenderedQuote {
  let prefix = String(quote.prefix ?? "");
  const exact = String(quote.exact ?? "");
  const suffix = String(quote.suffix ?? "");
  // A prefix that starts inside an annotation: drop through its last unmatched
  // closing brace, which ends that annotation.
  let depth = 0, cut = 0;
  for (let i = 0; i < prefix.length; i++) {
    if (prefix[i] === "\\") { i++; continue; }
    if (prefix[i] === "{") depth++;
    else if (prefix[i] === "}") { if (depth > 0) depth--; else cut = i + 1; }
  }
  prefix = prefix.slice(cut);
  const full = prefix + exact + suffix;
  const a = prefix.length, b = a + exact.length;
  const out: RenderedQuote = { prefix: "", exact: "", suffix: "" };
  let afterMath = false;
  for (const seg of quoteSegments(full, cutEnd || suffix !== "")) {
    if (seg.kind === "math") {
      const zone = seg.start < a ? (seg.end > a ? "exact" : "prefix") : seg.start < b ? "exact" : "suffix";
      out[zone] += segmentHtml(seg);
      afterMath = true;
      continue;
    }
    // The layer has a space between a formula and the punctuation after it, which
    // the rendered page does not show ("O(n²) ." reads "O(n²).").
    const lead = afterMath ? /^\s+(?=[,.;:!?)\]])/.exec(seg.text)?.[0].length ?? 0 : 0;
    afterMath = false;
    const pieces: [keyof RenderedQuote, number, number][] = [["prefix", 0, a], ["exact", a, b], ["suffix", b, full.length]];
    for (const [zone, lo, hi] of pieces) {
      const start = Math.max(seg.start + lead, lo), end = Math.min(seg.end, hi);
      if (end > start) out[zone] += esc(full.slice(start, end).replace(INVISIBLE, ""));
    }
  }
  return out;
}

// ---- a passage's full text --------------------------------------------------------

// A store's optional verified passage text (build_store.text_segments): segments that
// spell the passage's layer slice in order. Prose and citation markers are the layer's
// own text; a formula is its TeX (the MathML text and annotation the layer holds for it,
// drawn once), on a line of its own when Wikipedia displays it; a formula the passage's
// edge cuts keeps the layer text inside the passage.
export type PassageSegment =
  | { text: string }
  | { ref: string }
  | { tex: string; display?: true }
  | { tex: string; cut: string };

// The passage as HTML, each formula drawn once from its TeX; null when the segments are
// not a list of the four shapes (a corrupt store), so the page falls back to the quote.
export function passageTextHtml(segments: unknown): string | null {
  if (!Array.isArray(segments) || segments.length === 0) return null;
  let out = "";
  let afterMath = false;
  for (const raw of segments) {
    if (!raw || typeof raw !== "object") return null;
    const seg = raw as Record<string, unknown>;
    if (typeof seg.tex === "string") {
      if (typeof seg.cut === "string") {
        // Only part of the formula is inside the passage: its layer text there, as a quote's
        // cut formula renders (MathML tokens, or the head of the TeX annotation).
        out += renderQuote({ exact: seg.cut }, true).exact;
      } else {
        out += seg.display === true ? `<span class="atlas-math-display">${mathHtml(seg.tex, true)}</span>` : mathHtml(seg.tex);
      }
      afterMath = true;
      continue;
    }
    if (typeof seg.ref === "string") {
      out += `<sup class="atlas-ref">${esc(seg.ref.replace(INVISIBLE, ""))}</sup>`;
      afterMath = false;
      continue;
    }
    if (typeof seg.text !== "string") return null;
    // The layer has a space between a formula and the punctuation after it, which the
    // rendered article does not show (as renderQuote).
    const textValue = afterMath ? seg.text.replace(/^\s+(?=[,.;:!?)\]])/, "") : seg.text;
    out += esc(textValue.replace(INVISIBLE, ""));
    afterMath = false;
  }
  return out;
}

// ---- the text fragment -----------------------------------------------------------

// A prose word a text fragment can match on the rendered page: not a formula's
// leftover token (a lone letter other than "a", "A" or "I", a symbol).
function wordy(tok: string): boolean {
  if (!/[\p{L}\p{N}]/u.test(tok)) return false;
  if (/^[\p{L}\p{N}]$/u.test(tok)) return /^[aAI]$/.test(tok);
  return !MATH_SYMBOL.test(tok.replace(/^[(\[“"‘']+|[)\]”"’',.;:!?]+$/g, ""));
}

const EDGE_START = /^[(\[“"‘'«]+/;
const EDGE_END = /[)\]”"’',.;:!?»]+$/;
const encodeDirective = (s: string) => encodeURIComponent(s).replace(/-/g, "%2D");

// The text directive for a quote ("text=" not included), from its prose: the first
// run of at least two words, and the last run, as start and end of a range; a quote
// with no formula and no stray token is quoted whole, as it stands. null when there
// is no run of two words to match on.
export function quoteFragment(exact: string, cutEnd = false): string | null {
  const segments = quoteSegments(exact, cutEnd);
  const runs: string[][] = [];
  let current: string[] = [];
  let clean = true;
  for (const seg of segments) {
    if (seg.kind === "math") {
      clean = false;
      if (current.length) runs.push(current);
      current = [];
      continue;
    }
    for (const tok of seg.text.replace(INVISIBLE, " ").split(SPLIT).filter(Boolean)) {
      if (wordy(tok)) { current.push(tok); continue; }
      clean = false;
      if (current.length) runs.push(current);
      current = [];
    }
  }
  if (current.length) runs.push(current);
  const whole = exact.replace(INVISIBLE, "").trim();
  if (clean && whole) return encodeDirective(whole);
  const trim = (words: string[]) => words.join(" ").replace(EDGE_START, "").replace(EDGE_END, "");
  // The start is found anywhere on the page, so it must be specific: three words,
  // or two that make 15 characters. The end is looked for only after it.
  const startAt = runs.findIndex((run) => run.length >= 3 || (run.length === 2 && trim(run).length >= 15));
  if (startAt < 0) return null;
  const first = runs[startAt];
  const later = runs.slice(startAt + 1).filter((run) => run.length >= 2);
  const last = later.length ? later[later.length - 1] : null;
  if (!last) {
    if (first.length <= 12) return encodeDirective(trim(first));
    return `${encodeDirective(trim(first.slice(0, 8)))},${encodeDirective(trim(first.slice(-6)))}`;
  }
  return `${encodeDirective(trim(first.slice(0, 8)))},${encodeDirective(trim(last.slice(-6)))}`;
}
