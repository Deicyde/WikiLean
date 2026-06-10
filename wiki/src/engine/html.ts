// Minimal HTML entity encode/decode to match Python's `html` module behavior
// for the subset of entities that appear in MediaWiki parse output. Only the
// plain-text projection (used for snippet matching) relies on decoding, so the
// table can grow as golden tests surface missing entities.

const NAMED: Record<string, string> = {
  amp: "&", lt: "<", gt: ">", quot: '"', apos: "'",
  nbsp: " ", ensp: " ", emsp: " ", thinsp: " ",
  zwnj: "‌", zwj: "‍", shy: "­",
  ndash: "–", mdash: "—", minus: "−", horbar: "―",
  lsquo: "‘", rsquo: "’", ldquo: "“", rdquo: "”",
  sbquo: "‚", bdquo: "„", hellip: "…",
  prime: "′", Prime: "″", bull: "•", middot: "·", sdot: "⋅",
  times: "×", divide: "÷", plusmn: "±", frasl: "⁄",
  deg: "°", micro: "µ", para: "¶", sect: "§",
  copy: "©", reg: "®", trade: "™",
  larr: "←", uarr: "↑", rarr: "→", darr: "↓", harr: "↔",
  lArr: "⇐", rArr: "⇒", hArr: "⇔",
  forall: "∀", part: "∂", exist: "∃", empty: "∅",
  nabla: "∇", isin: "∈", notin: "∉", ni: "∋",
  prod: "∏", sum: "∑", lowast: "∗", radic: "√",
  prop: "∝", infin: "∞", ang: "∠",
  and: "∧", or: "∨", cap: "∩", cup: "∪", int: "∫",
  there4: "∴", sim: "∼", cong: "≅", asymp: "≈",
  ne: "≠", equiv: "≡", le: "≤", ge: "≥",
  sub: "⊂", sup: "⊃", nsub: "⊄", sube: "⊆", supe: "⊇",
  oplus: "⊕", otimes: "⊗", perp: "⊥",
  lceil: "⌈", rceil: "⌉", lfloor: "⌊", rfloor: "⌋",
  lang: "⟨", rang: "⟩", loz: "◊",
  alpha: "α", beta: "β", gamma: "γ", delta: "δ",
  epsilon: "ε", zeta: "ζ", eta: "η", theta: "θ",
  iota: "ι", kappa: "κ", lambda: "λ", mu: "μ",
  nu: "ν", xi: "ξ", omicron: "ο", pi: "π",
  rho: "ρ", sigmaf: "ς", sigma: "σ", tau: "τ",
  upsilon: "υ", phi: "φ", chi: "χ", psi: "ψ", omega: "ω",
  Alpha: "Α", Beta: "Β", Gamma: "Γ", Delta: "Δ",
  Epsilon: "Ε", Zeta: "Ζ", Eta: "Η", Theta: "Θ",
  Iota: "Ι", Kappa: "Κ", Lambda: "Λ", Mu: "Μ",
  Nu: "Ν", Xi: "Ξ", Omicron: "Ο", Pi: "Π",
  Rho: "Ρ", Sigma: "Σ", Tau: "Τ", Upsilon: "Υ",
  Phi: "Φ", Chi: "Χ", Psi: "Ψ", Omega: "Ω",
};

export function htmlUnescape(s: string): string {
  if (s.indexOf("&") === -1) return s;
  return s.replace(/&(#x[0-9a-fA-F]+|#\d+|[a-zA-Z][a-zA-Z0-9]*);/g, (m, body: string) => {
    if (body[0] === "#") {
      const cp = body[1] === "x" || body[1] === "X"
        ? parseInt(body.slice(2), 16)
        : parseInt(body.slice(1), 10);
      if (Number.isNaN(cp) || cp < 0 || cp > 0x10ffff) return m;
      try {
        return String.fromCodePoint(cp);
      } catch {
        return m;
      }
    }
    const v = NAMED[body];
    return v !== undefined ? v : m;
  });
}

export function htmlEscape(s: string, quote = true): string {
  let out = s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  if (quote) {
    out = out.replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
  }
  return out;
}
