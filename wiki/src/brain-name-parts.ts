// A name split into parts the way site/ops/brain_d1_import.py's name_parts
// splits it before filling a parts-mode names index (names_mode "parts",
// docs/BRAIN-D1.md section 4): a space before each ASCII capital that follows
// a letter or digit, then a split at dots, underscores, slashes and
// whitespace. The D1 store splits a query the same way, so a fragment such as
// "fourierCoeff" meets the tokens "fourier" and "coeff" the index holds.
//
// No imports: site/ops/test_brain_d1_import.py runs this file under
// `node --experimental-strip-types` and compares it with the Python original.
// Python's \s, which differs from JavaScript's: it includes U+001C-U+001F and
// U+0085 and leaves out U+FEFF.
const PART_BREAK = /[._/\t\n\v\f\r \x1c-\x1f\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+/u;

export function nameParts(name: string): string {
  const spaced = name.replace(/(?<=[\p{L}\p{N}])(?=[A-Z])/gu, " ");
  return spaced.split(PART_BREAK).filter((part) => part.length > 0).join(" ");
}
