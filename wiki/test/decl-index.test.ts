// Pure-function tests for the full-Mathlib decl index build
// (scripts/build-decl-index.ts): shard-key assignment, the recursive
// prefix-split scheme, and manifest construction. No network — the fetch/write
// CLI path is exercised by running `npm run build:decl-index` for real.
import { describe, it, expect } from "vitest";
import {
  buildManifest,
  buildShards,
  moduleFromDocLink,
  pairsFromDeclarationData,
  shardKey,
  shardKeyChar,
  MAX_SHARD_BYTES,
  MIN_KEY_LEN,
  PAD,
  type DeclPair,
} from "../scripts/build-decl-index.js";

describe("shard key assignment", () => {
  it("lowercases alphanumerics", () => {
    expect(shardKey("Ideal.IsPrime", 2)).toBe("id");
    expect(shardKey("Nat.Prime", 2)).toBe("na");
    expect(shardKey("ZMod.natCast_self", 2)).toBe("zm");
  });

  it("maps non-alphanumerics to underscore", () => {
    expect(shardKeyChar(".")).toBe(PAD);
    expect(shardKeyChar("«")).toBe(PAD);
    expect(shardKey("_root_.id", 2)).toBe("_r");
    expect(shardKey("«term_+_»", 3)).toBe("_te");
    // dots inside the prefix window normalize too
    expect(shardKey("Wf.fix", 3)).toBe("wf_");
  });

  it("pads names shorter than the key length", () => {
    expect(shardKey("E", 2)).toBe("e" + PAD);
    expect(shardKey("id", 3)).toBe("id" + PAD);
    expect(shardKey("", 2)).toBe(PAD + PAD);
  });

  it("keeps digits", () => {
    expect(shardKey("Fin2.add", 4)).toBe("fin2");
  });
});

describe("moduleFromDocLink", () => {
  it("converts a docLink path to a dotted module", () => {
    expect(moduleFromDocLink("./Mathlib/Data/Nat/Prime/Defs.html#Nat.Prime")).toBe(
      "Mathlib.Data.Nat.Prime.Defs",
    );
    expect(moduleFromDocLink("./Init/Prelude.html#id")).toBe("Init.Prelude");
  });

  it("tolerates a missing fragment", () => {
    expect(moduleFromDocLink("./Mathlib/Order/Basic.html")).toBe("Mathlib.Order.Basic");
  });
});

describe("buildShards", () => {
  const pair = (d: string, m = "Mathlib.X"): DeclPair => [d, m];

  it("groups by 2-char key when everything fits", () => {
    const shards = buildShards(
      [pair("Nat.Prime"), pair("Nat.succ"), pair("Ideal.IsPrime"), pair("idCast")],
      { maxBytes: 10_000 },
    );
    expect([...shards.keys()]).toEqual(["id", "na"]);
    expect(shards.get("na")!.map(([d]) => d)).toEqual(["Nat.Prime", "Nat.succ"]);
    expect(shards.get("id")!.map(([d]) => d)).toEqual(["Ideal.IsPrime", "idCast"]);
  });

  it("sorts each shard by decl in code-unit order", () => {
    const shards = buildShards([pair("Nat.b"), pair("Nat.A"), pair("NaN")], { maxBytes: 10_000 });
    expect(shards.get("na")!.map(([d]) => d)).toEqual(["NaN", "Nat.A", "Nat.b"]);
  });

  it("splits an oversize shard onto 3-char keys", () => {
    const pairs = [pair("Nat.Prime"), pair("Nat.succ"), pair("Nab"), pair("Ideal.IsPrime")];
    // Force a split of "na" but not "id": every full "na" shard is > maxBytes,
    // while each 3-char child fits.
    const naBytes = JSON.stringify([pair("Nab"), pair("Nat.Prime"), pair("Nat.succ")]).length;
    const natBytes = JSON.stringify([pair("Nat.Prime"), pair("Nat.succ")]).length;
    const shards = buildShards(pairs, { maxBytes: Math.max(natBytes, naBytes - 1) });
    expect([...shards.keys()]).toEqual(["id", "nab", "nat"]);
    expect(shards.get("nat")!.map(([d]) => d)).toEqual(["Nat.Prime", "Nat.succ"]);
    expect(shards.get("nab")!.map(([d]) => d)).toEqual(["Nab"]);
  });

  it("recurses past 3 chars for a long shared prefix (the CategoryTheory case)", () => {
    const pairs = [
      pair("CategoryTheory.Limits.HasLimit"),
      pair("CategoryTheory.Limits.IsLimit"),
      pair("CategoryTheory.Monad.algebra"),
      pair("Cat.assoc"),
    ];
    // Tiny budget: only single-entry shards fit.
    const shards = buildShards(pairs, { maxBytes: JSON.stringify([pairs[0]]).length });
    expect(shards.size).toBe(4);
    for (const arr of shards.values()) expect(arr.length).toBe(1);
    // Leaf keys are prefix-free, so each name resolves to exactly one shard.
    const keys = [...shards.keys()];
    for (const a of keys) {
      for (const b of keys) {
        if (a !== b) expect(a.startsWith(b)).toBe(false);
      }
    }
    // The two Limits decls diverge only at "CategoryTheory.Limits.H/I" —
    // depth 23 — so their keys must be at least that long.
    const limitKeys = keys.filter((k) => k.startsWith("categorytheory_limits_"));
    expect(limitKeys.length).toBe(2);
    for (const k of limitKeys) expect(k.length).toBeGreaterThanOrEqual(23);
  });

  it("pads short names into longer keys when a split forces it", () => {
    const pairs = [pair("Na"), pair("Nat.Prime"), pair("Nat.succ")];
    const shards = buildShards(pairs, { maxBytes: JSON.stringify([pairs[1]]).length });
    // "Na" pads to "na_" at length 3.
    expect(shards.get("na" + PAD)!.map(([d]) => d)).toEqual(["Na"]);
  });

  it("terminates on names that normalize identically at every length", () => {
    // "Nat.add" and "Nat_Add" normalize to "nat_add" + padding forever — no
    // key length separates them. The maxLen guard must stop the recursion and
    // accept the oversize shard rather than loop.
    const pairs = [pair("Nat.add"), pair("Nat_Add")];
    const shards = buildShards(pairs, { maxBytes: 1, maxLen: 8 });
    expect(shards.size).toBe(1);
    const [key, arr] = [...shards.entries()][0];
    expect(key).toBe("nat_add" + PAD);
    expect(key.length).toBe(8);
    expect(arr.map(([d]) => d)).toEqual(["Nat.add", "Nat_Add"]);
  });

  it("is deterministic: keys come back sorted", () => {
    const pairs = [pair("Zeta"), pair("Alpha"), pair("Mu"), pair("Beta")];
    const keys = [...buildShards(pairs, { maxBytes: 10_000 }).keys()];
    expect(keys).toEqual([...keys].sort());
  });
});

describe("buildManifest", () => {
  it("records totals, per-shard counts, and the scheme", () => {
    const shards = buildShards(
      [
        ["Nat.Prime", "Mathlib.Data.Nat.Prime.Defs"],
        ["Nat.succ", "Init.Prelude"],
        ["Ideal.IsPrime", "Mathlib.RingTheory.Ideal.Prime"],
      ],
      { maxBytes: 10_000 },
    );
    const m = buildManifest(shards, { source: "https://example.test/d.bmp", source_sha_or_etag: '"abc"' });
    expect(m.total).toBe(3);
    expect(m.shards).toEqual({ id: 1, na: 2 });
    expect(m.source).toBe("https://example.test/d.bmp");
    expect(m.source_sha_or_etag).toBe('"abc"');
    expect(m.scheme.kind).toBe("prefix");
    expect(m.scheme.min_len).toBe(MIN_KEY_LEN);
    expect(m.scheme.max_bytes).toBe(MAX_SHARD_BYTES);
    expect(m.scheme.pad).toBe(PAD);
    expect(typeof m.built_at).toBe("string");
  });

  it("max_len tracks the longest emitted key (the client's loop bound)", () => {
    const pairs: DeclPair[] = [
      ["CategoryTheory.Limits.HasLimit", "M"],
      ["CategoryTheory.Limits.IsLimit", "M"],
    ];
    const shards = buildShards(pairs, { maxBytes: JSON.stringify([pairs[0]]).length });
    const m = buildManifest(shards, { source: "s", source_sha_or_etag: "e" });
    const longest = Math.max(...Object.keys(m.shards).map((k) => k.length));
    expect(m.scheme.max_len).toBe(longest);
    expect(longest).toBeGreaterThan(3);
  });

  it("total equals the sum of shard counts", () => {
    const pairs: DeclPair[] = Array.from({ length: 50 }, (_, i) => [`D${i}.x`, "M"]);
    const shards = buildShards(pairs, { maxBytes: 200 });
    const m = buildManifest(shards, { source: "s", source_sha_or_etag: "e" });
    expect(Object.values(m.shards).reduce((a, b) => a + b, 0)).toBe(m.total);
    expect(m.total).toBe(50);
  });
});

describe("pairsFromDeclarationData", () => {
  it("maps the doc-gen4 shape to [decl, module] pairs", () => {
    const pairs = pairsFromDeclarationData({
      declarations: {
        "Nat.Prime": { docLink: "./Mathlib/Data/Nat/Prime/Defs.html#Nat.Prime", kind: "def" },
        AddCommGroup: { docLink: "./Mathlib/Algebra/Group/Defs.html#AddCommGroup", kind: "class" },
      },
    });
    expect(pairs).toEqual([
      ["Nat.Prime", "Mathlib.Data.Nat.Prime.Defs"],
      ["AddCommGroup", "Mathlib.Algebra.Group.Defs"],
    ]);
  });
});
