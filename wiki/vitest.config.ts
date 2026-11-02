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
    // Vitest 5 caches test results (cache, node_modules/.vite) and
    // transformed modules (fsModuleCache, node_modules/.vitest-cache) by
    // default; the release promoter fingerprints wiki/node_modules.
    cache: false,
    fsModuleCache: false,
  },
});
