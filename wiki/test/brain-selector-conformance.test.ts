import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { parseBrainReleaseSelector } from "../src/brain";

interface SelectorCase {
  name: string;
  valid: boolean;
  selector: unknown;
}

const fixture = JSON.parse(readFileSync(resolve(
  import.meta.dirname,
  "../../brain/authority/fixtures/release-selector-v2-conformance.json",
), "utf8")) as { cases: SelectorCase[] };

describe("release selector cross-language conformance", () => {
  for (const testCase of fixture.cases) {
    it(testCase.name, async () => {
      const parsed = await parseBrainReleaseSelector(testCase.selector);
      expect(parsed !== null).toBe(testCase.valid);
    });
  }
});
