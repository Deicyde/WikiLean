// The BrainStore seam (src/brain-store.ts, docs/BRAIN-D1.md step 3a): every
// Brain data read goes through the release's store. A substituted store must
// answer the routes, no module but brain.ts and the static store may touch the
// release's files, and the static store must keep the old load order.

import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { _resetBrainAssetMemo } from "../src/brain.js";
import type { CellEntry } from "../src/brain-api.js";
import { _setBrainStoreForTests, type AtomHead } from "../src/brain-store.js";
import { StaticStore } from "../src/brain-store-static.js";
import { setup, get, blockNetwork } from "./helpers/harness.js";
import { installBrainFixture, MODULE_CELL } from "./helpers/brain-fixture.js";

blockNetwork();

beforeEach(() => _resetBrainAssetMemo());
afterEach(() => _setBrainStoreForTests(null));

async function getJson(env: Parameters<typeof get>[0], path: string): Promise<{ status: number; j: Record<string, unknown> }> {
  const res = await get(env, path);
  return { status: res.status, j: (await res.json()) as Record<string, unknown> };
}

describe("routes read through the release's store", () => {
  it("answers /api/brain/cell from a substituted store's cellPage", async () => {
    const h = setup();
    installBrainFixture(h.env);
    class FakeCellStore extends StaticStore {
      async cellPage(id: string): Promise<CellEntry | null> {
        return id === "cell:Fake" ? { cell: { id: "cell:Fake", anchor: "Q1", label: "Fake atom" } } : null;
      }
    }
    _setBrainStoreForTests((c, release) => new FakeCellStore(c, release));
    const { status, j } = await getJson(h.env, "/api/brain/cell?key=cell:Fake");
    expect(status).toBe(200);
    expect(j).toMatchObject({ ok: true, resolved_from: "cell", id: "cell:Fake", kind: "cell", label: "Fake atom" });
  });

  it("answers /api/brain/search from a substituted store's searchNames", async () => {
    const h = setup();
    installBrainFixture(h.env);
    class FakeSearchStore extends StaticStore {
      async searchNames(): Promise<AtomHead[]> {
        return [{ kind: "cell", row: { id: "cell:Fake", label: "Fake atom" } }];
      }
    }
    _setBrainStoreForTests((c, release) => new FakeSearchStore(c, release));
    const { status, j } = await getJson(h.env, "/api/brain/search?q=zzzzqq");
    expect(status).toBe(200);
    expect(j.hits).toEqual([{ id: "cell:Fake", label: "Fake atom" }]);
  });
});

describe("the static store keeps the old reads", () => {
  // Loaders are awaited one after another: when labels.json fails, search must
  // answer 503 without ever requesting supercells.json.
  it("never requests supercells.json for a search once labels.json failed", async () => {
    const paths: string[] = [];
    const h = setup();
    installBrainFixture(h.env, { missingAssets: ["cells/labels.json"], onAssetPath: (path) => paths.push(path) });
    const { status } = await getJson(h.env, "/api/brain/search?q=vector");
    expect(status).toBe(503);
    expect(paths.some((path) => path.endsWith("/cells/labels.json"))).toBe(true);
    expect(paths.some((path) => path.endsWith("/cells/supercells.json"))).toBe(false);
  });

  // The free-text transfer fallback searches case-insensitively.
  it("resolves mixed-case free text through the transfer fallback", async () => {
    const h = setup();
    installBrainFixture(h.env);
    const { status, j } = await getJson(h.env, "/api/brain/transfer?q=Vector%20spa&direction=informal_to_formal");
    expect(status).toBe(200);
    expect(j).toMatchObject({ ok: true, resolved_from: "search", id: MODULE_CELL });
  });
});

describe("only the static store reads Brain release files", () => {
  // brain.ts owns the loaders and the selector; brain-store-static.ts is the one
  // reader. A D1 store module joins this list in step 3b if it reads the slim
  // static manifest.
  const ALLOWED = new Set(["brain.ts", "brain-store-static.ts"]);
  const srcDir = fileURLToPath(new URL("../src/", import.meta.url));
  const files = readdirSync(srcDir, { recursive: true, encoding: "utf8" })
    .filter((f) => f.endsWith(".ts"));

  it("finds the source files", () => {
    expect(files).toContain("brain-api.ts");
    expect(files).toContain("brain-store-static.ts");
  });

  it("keeps Brain asset loaders and release paths out of every other module", () => {
    const offenders: string[] = [];
    for (const f of files) {
      if (ALLOWED.has(f)) continue;
      const source = readFileSync(srcDir + f, "utf8");
      if (/\b(?:requiredBrainAssetJson|brainAssetJson)\b/.test(source)) offenders.push(`${f}: asset loader`);
      if (/["'`](?:cells\/|xref_index\.json|\/assets\/brain\/)/.test(source)) offenders.push(`${f}: release path`);
    }
    expect(offenders).toEqual([]);
  });
});
