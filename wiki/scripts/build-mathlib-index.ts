// Extracts every (decl, module) pair that's been confirmed-formalized across
// the existing annotations, and writes a static JSON the editor's autocomplete
// fetches once. Bootstrapping from our own curation: every decl in here has
// already been reviewed as relevant to a math article somewhere.
//
//   node --experimental-strip-types scripts/build-mathlib-index.ts
// or imported by build-public.ts to run inline.
import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { mathlibTargets, type JsonObject } from "../src/native-companion.ts";

export function buildMathlibIndex(siteDir: string, outPath: string): number {
  const annotDir = resolve(siteDir, "annotations");
  const pairs = new Map<string, string>();
  for (const f of readdirSync(annotDir)) {
    if (!f.endsWith(".json") || f.includes(".agent1.")) continue;
    let d: { annotations?: JsonObject[] };
    try {
      d = JSON.parse(readFileSync(resolve(annotDir, f), "utf8"));
    } catch {
      continue;
    }
    for (const a of d.annotations ?? []) {
      // Only confirmed Mathlib targets belong in this Mathlib-only asset.
      for (const { decl, module } of mathlibTargets(a)) {
        if (module && a.status === "formalized" && !pairs.has(decl)) pairs.set(decl, module);
      }
    }
  }
  const arr = [...pairs.entries()].sort(([a], [b]) => a.localeCompare(b));
  if (!existsSync(dirname(outPath))) mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, JSON.stringify(arr));
  return arr.length;
}

const isCli = import.meta.url === `file://${process.argv[1]}`;
if (isCli) {
  const siteDir = resolve(process.cwd(), "..", "site");
  const outPath = resolve(process.cwd(), "public", "assets", "mathlib-index.json");
  const n = buildMathlibIndex(siteDir, outPath);
  console.log(`wrote ${outPath} — ${n} (decl, module) pairs`);
}
