import { existsSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

const root = resolve(import.meta.dirname, "../..");
const requirements = [
  {
    path: resolve(root, "site/cache"),
    description: "cached Wikipedia HTML (more than 200 *.html files)",
    validate: (path) => existsSync(path) && readdirSync(path).filter((name) => name.endsWith(".html")).length > 200,
  },
  {
    path: resolve(root, "site/out"),
    description: "Python renderer output (more than 200 *.html files)",
    validate: (path) => existsSync(path) && readdirSync(path).filter((name) => name.endsWith(".html")).length > 200,
  },
  {
    path: resolve(root, "site/annotations"),
    description: "annotation corpus (more than 250 *.json files)",
    validate: (path) => existsSync(path) && readdirSync(path).filter((name) => name.endsWith(".json")).length > 250,
  },
  {
    path: resolve(root, "wiki/public/assets/decl-index/manifest.json"),
    description: "generated Mathlib declaration-index manifest",
    validate: existsSync,
  },
];

const missing = requirements.filter(({ path, validate }) => !validate(path));
if (missing.length > 0) {
  console.error("Corpus test preflight failed. These opt-in tests need generated local data:");
  for (const requirement of missing) {
    console.error(`  - ${requirement.description}: ${requirement.path}`);
  }
  console.error("See CONTRIBUTING.md for corpus setup. The hermetic CI suite is `npm test`.");
  process.exit(1);
}

console.log("Corpus test preflight passed: renderer, annotation, and declaration-index inputs are present.");
