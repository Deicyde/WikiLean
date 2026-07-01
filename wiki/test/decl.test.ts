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
  it("returns null when no shard matches", () => {
    expect(declShardFor(MANIFEST, "Zorn.lemma")).toBeNull();
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
