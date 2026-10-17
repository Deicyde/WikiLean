// The atlas contract fixture for Worker tests: the golden store that
// scripts/atlas/build_store.py builds from scripts/atlas/fixtures/recordset
// (its Python test pins that the committed store equals a fresh build), served
// through a minimal R2-shaped bucket so the real ATLAS code path runs.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Env } from "../../src/env.js";

export const ATLAS_STORE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../../../scripts/atlas/fixtures/store");

export const WIDGET_PAGEID = 1001;
export const GADGET_PAGEID = 1003;
export const FIXTURE_CAMPAIGN = "fixture-2026-10";
export const FIXTURE_UNIVERSE_SHA = "0e6efcaae05b9aee11c90b919b55052636ee2932901127ea571268dc3a8d6a25";

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else out.push(path);
  }
  return out;
}

// Store path ("atlas/v1/...") → file text.
export function atlasFixtureFiles(): Map<string, string> {
  const files = new Map<string, string>();
  for (const path of walk(ATLAS_STORE_ROOT)) {
    files.set(relative(ATLAS_STORE_ROOT, path).split("\\").join("/"), readFileSync(path, "utf8"));
  }
  return files;
}

export interface FakeBucket {
  get(key: string): Promise<{ text(): Promise<string> } | null>;
  gets: string[];
}

export function fakeBucket(files: Map<string, string>): FakeBucket {
  const gets: string[] = [];
  return {
    gets,
    async get(key: string) {
      gets.push(key);
      const value = files.get(key);
      return value === undefined ? null : { text: async () => value };
    },
  };
}

// Binds ATLAS on a harness env; returns the bucket so tests can count reads.
export function bindAtlas(env: Env, files: Map<string, string> = atlasFixtureFiles()): FakeBucket {
  const bucket = fakeBucket(files);
  (env as unknown as { ATLAS: FakeBucket }).ATLAS = bucket;
  return bucket;
}

export function fixtureArticle(pageid = WIDGET_PAGEID): Record<string, any> {
  return JSON.parse(atlasFixtureFiles().get(`atlas/v1/articles/${pageid}.json`)!);
}
