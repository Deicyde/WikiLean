import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    exclude: [
      "test/e2e/**",
      "test/engine.golden.test.ts",
      "test/decl.corpus.test.ts",
      "test/seed.test.ts",
    ],
    testTimeout: 120000,
  },
});
