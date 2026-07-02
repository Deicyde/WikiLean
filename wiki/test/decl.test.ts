// /decl/:name resolver — pure-logic tests. The shard-key scheme must mirror
// scripts/build-decl-index.ts (and editor.js's client-side copy) exactly.
import { describe, expect, it } from "vitest";
import { declShardKey, declShardFor, docsUrlFor, lookupInShard } from "../src/decl.js";

const MANIFEST = {
  scheme: { min_len: 2, max_len: 3, pad: "_" },
  shards: { na: 1, ca: 1, cat: 1, __: 1 } as Record<string, number>,
};

describe("declShardKey", () => {
  it("lowercases [a-z0-9], maps everything else to _, pads short names", () => {
    expect(declShardKey("Nat.Prime", 2)).toBe("na");
    expect(declShardKey("Nat.Prime", 4)).toBe("nat_");
    expect(declShardKey("«weird»", 2)).toBe("_w");
    expect(declShardKey("x", 2)).toBe("x_");
  });
});

describe("declShardFor", () => {
  it("prefers the longest matching key", () => {
    // "cat…" resolves to the 3-char shard, not the 2-char one.
    expect(declShardFor(MANIFEST, "CategoryTheory.Iso")).toBe("cat");
    expect(declShardFor(MANIFEST, "Cauchy")).toBe("ca");
    expect(declShardFor(MANIFEST, "Nat.Prime")).toBe("na");
  });
  it("retries upward for names shorter than their padded leaf key", () => {
    // Name "se" (2 chars) whose range split to 3-char keys: leaf is "se_".
    const m = { scheme: { min_len: 2, max_len: 4, pad: "_" }, shards: { se_: 1, set_: 1 } as Record<string, number> };
    expect(declShardFor(m, "Se")).toBe("se_");
    expect(declShardFor(m, "Set")).toBe("set_");
  });
  it("returns null when no shard matches", () => {
    expect(declShardFor(MANIFEST, "Zorn.lemma")).toBeNull();
  });
});

describe("declShardFor against the REAL manifest", () => {
  it("resolves every decl the review found missing (padded-leaf names)", async () => {
    const { readFileSync } = await import("node:fs");
    const manifest = JSON.parse(readFileSync(new URL("../public/assets/decl-index/manifest.json", import.meta.url), "utf8"));
    for (const name of ["Set", "Int", "Fin", "Add", "LE", "Algebra", "Continuous", "CategoryTheory.Functor", "Group", "Real"]) {
      expect(declShardFor(manifest, name), `shard for ${name}`).not.toBeNull();
    }
  });
});

describe("lookupInShard", () => {
  const pairs: Array<[string, string]> = [
    ["Nat.Prime", "Mathlib.Data.Nat.Prime.Defs"],
    ["Nat.Prime.two_le", "Mathlib.Data.Nat.Prime.Basic"],
    ["Nat.add", "Init.Prelude"],
  ].sort((a, b) => (a[0] < b[0] ? -1 : 1)) as Array<[string, string]>;
  it("finds exact names via binary search", () => {
    expect(lookupInShard(pairs, "Nat.Prime")).toBe("Mathlib.Data.Nat.Prime.Defs");
    expect(lookupInShard(pairs, "Nat.add")).toBe("Init.Prelude");
  });
  it("misses cleanly", () => {
    expect(lookupInShard(pairs, "Nat.Composite")).toBeNull();
    expect(lookupInShard([], "x")).toBeNull();
  });
});

describe("docsUrlFor", () => {
  it("builds the hierarchical mathlib4_docs URL", () => {
    expect(docsUrlFor("Mathlib.RingTheory.Ideal.Prime", "Ideal.IsPrime")).toBe(
      "https://leanprover-community.github.io/mathlib4_docs/Mathlib/RingTheory/Ideal/Prime.html#Ideal.IsPrime",
    );
  });
});

describe("multi-library fabric (resolveInLibraries)", () => {
  const BLOB = {
    libraries: {
      cslib: { label: "CSLib", docs_base: "https://api.cslib.io/docs/", aliases: [] },
      "formal-conjectures": { label: "Formal Conjectures", docs_base: "https://google-deepmind.github.io/formal-conjectures/doc/", aliases: [] },
    },
    decls: {
      cslib: { "Cslib.Automata.DA": "Cslib.Automata.DA.Basic" },
      "formal-conjectures": { "CollatzConjecture.collatz_conjecture": "FormalConjectures.Wikipedia.CollatzConjecture" },
    },
  };
  it("resolves a CSLib decl to its docs URL", async () => {
    const { resolveInLibraries } = await import("../src/decl.js");
    const hit = await resolveInLibraries(BLOB as never, "Cslib.Automata.DA");
    expect(hit).toMatchObject({ library: "cslib", label: "CSLib" });
    expect(hit!.docs_url).toBe("https://api.cslib.io/docs/Cslib/Automata/DA/Basic.html#Cslib.Automata.DA");
  });
  it("resolves an FC decl whose namespace differs from its module", async () => {
    const { resolveInLibraries } = await import("../src/decl.js");
    const hit = await resolveInLibraries(BLOB as never, "CollatzConjecture.collatz_conjecture");
    expect(hit!.docs_url).toContain("/doc/FormalConjectures/Wikipedia/CollatzConjecture.html#CollatzConjecture.collatz_conjecture");
  });
  it("misses cleanly on unknown names and null blobs", async () => {
    const { resolveInLibraries } = await import("../src/decl.js");
    expect(await resolveInLibraries(BLOB as never, "Nope.nope")).toBeNull();
    expect(await resolveInLibraries(null, "Cslib.Automata.DA")).toBeNull();
  });
});
