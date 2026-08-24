import { describe, expect, it } from "vitest";
import { declShardFor } from "../src/decl.js";

describe("declShardFor against the real manifest", () => {
  it("resolves every declaration the review found missing", async () => {
    const { readFileSync } = await import("node:fs");
    const manifest = JSON.parse(
      readFileSync(new URL("../public/assets/decl-index/manifest.json", import.meta.url), "utf8"),
    );
    for (const name of [
      "Set",
      "Int",
      "Fin",
      "Add",
      "LE",
      "Algebra",
      "Continuous",
      "CategoryTheory.Functor",
      "Group",
      "Real",
    ]) {
      expect(declShardFor(manifest, name), `shard for ${name}`).not.toBeNull();
    }
  });
});
