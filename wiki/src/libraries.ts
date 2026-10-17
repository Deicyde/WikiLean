// Library registry for the formalization atlas: a Lean library's repository
// URI → its display name and the template that turns (commit, path, lines)
// into a pinned source link. The atlas never shows a docs page as evidence:
// doc-gen4 pages exist only for a library's latest build, so the pinned
// evidence is the source blob at the record's commit.
//
// Precedence (docs/ATLAS.md, "Libraries"): a universe manifest entry's own
// display_name / blob_url_template / src_dir wins; then the seeds below; then
// the FormalFrontier organisation pattern; then any github.com repository
// (named owner/repo, linked with GitHub's blob scheme). A non-GitHub
// repository with no template gets no source link rather than a guessed one.

export interface UniverseLibrary {
  id: string;
  repo: string;
  commit: string;
  display_name?: string;
  blob_url_template?: string;
  src_dir?: string;
  brain_lib?: string;
  toolchain?: string;
  licence?: string;
  roots?: string[];
}

export interface LibraryInfo {
  repo: string;
  name: string;
  // A short label for chips: the name without a qualifier such as "(fixture)".
  short: string;
  // {repo} {commit} {path} {line} {end_line}; null = no pinned source link.
  blobTemplate: string | null;
  // Directory the record's path is relative to inside the repository.
  srcDir: string | null;
}

const GITHUB_BLOB = "{repo}/blob/{commit}/{path}#L{line}-L{end_line}";
const GITHUB_RE = /^https:\/\/github\.com\/([A-Za-z0-9_.-]{1,100})\/([A-Za-z0-9_.-]{1,100})$/;
const FORMAL_FRONTIER_RE = /^https:\/\/github\.com\/FormalFrontier\/([A-Za-z0-9_.-]{1,100})$/i;

interface Seed {
  repo: string;
  name: string;
  srcDir?: string;
}

// Seeded from repos.ts GLOBAL_SOURCES plus the universe libraries the locate
// proposal names (§2.3: Lean core and Batteries are libraries of their own).
const SEEDS: Seed[] = [
  { repo: "https://github.com/leanprover-community/mathlib4", name: "Mathlib" },
  // Init, Std and Lean live under src/ in the lean4 repository.
  { repo: "https://github.com/leanprover/lean4", name: "Lean core", srcDir: "src" },
  { repo: "https://github.com/leanprover-community/batteries", name: "Batteries" },
  { repo: "https://github.com/TauCetiProject/TauCeti", name: "TauCeti" },
  { repo: "https://github.com/google-deepmind/formal-conjectures", name: "Formal Conjectures" },
  // facebookresearch's ATLAS library, not WikiLean's atlas plane.
  { repo: "https://github.com/facebookresearch/atlas-lean", name: "ATLAS (atlas-lean)" },
];

// Lookup key: lowercase (GitHub owner/repo names are case-insensitive), no
// trailing slash or .git.
export function repoKey(repo: string): string {
  return repo.trim().replace(/\/+$/, "").replace(/\.git$/i, "").toLowerCase();
}

const SEED_BY_KEY = new Map(SEEDS.map((seed) => [repoKey(seed.repo), seed]));

function cleanRepo(repo: string): string {
  return repo.trim().replace(/\/+$/, "").replace(/\.git$/i, "");
}

export function libraryInfo(repo: string, entry?: UniverseLibrary | null): LibraryInfo {
  const clean = cleanRepo(repo);
  const seed = SEED_BY_KEY.get(repoKey(clean));
  const github = GITHUB_RE.exec(clean);
  const ff = FORMAL_FRONTIER_RE.exec(clean);
  let name = seed?.name ?? (ff ? `FormalFrontier · ${ff[1]}` : github ? `${github[1]}/${github[2]}` : clean);
  let blobTemplate: string | null = github ? GITHUB_BLOB : null;
  let srcDir: string | null = seed?.srcDir ?? null;
  if (entry) {
    if (typeof entry.display_name === "string" && entry.display_name.trim()) name = entry.display_name.trim();
    if (typeof entry.blob_url_template === "string" && validTemplate(entry.blob_url_template)) {
      blobTemplate = entry.blob_url_template;
    }
    if (typeof entry.src_dir === "string" && safeRelativePath(entry.src_dir)) srcDir = entry.src_dir;
  }
  const short = name.replace(/\s*\([^)]*\)\s*$/, "") || name;
  return { repo: clean, name, short, blobTemplate, srcDir };
}

function validTemplate(template: string): boolean {
  return template.startsWith("https://") && template.includes("{commit}") && template.includes("{path}");
}

// A repository-relative path a link may name: no absolute, parent, empty or
// backslash segments, no control characters.
export function safeRelativePath(path: string): boolean {
  if (!path || path.startsWith("/") || path.includes("\\") || /[\u0000-\u001f\u007f]/.test(path)) return false;
  return path.split("/").every((part) => part !== "" && part !== "." && part !== "..");
}

const COMMIT_RE = /^[0-9a-f]{40}$/;

// The pinned source link for one location, or null when the library has no
// template or any part fails validation. Every substituted part is encoded,
// so a hostile path can neither leave the repository nor break an attribute.
export function sourceUrl(
  info: LibraryInfo,
  loc: { commit: string; path: string; line?: number | null; end_line?: number | null },
): string | null {
  if (!info.blobTemplate || !COMMIT_RE.test(loc.commit) || !safeRelativePath(loc.path)) return null;
  const full = info.srcDir ? `${info.srcDir}/${loc.path}` : loc.path;
  if (!safeRelativePath(full)) return null;
  const line = Number.isSafeInteger(loc.line) && (loc.line as number) >= 1 ? (loc.line as number) : null;
  const end = Number.isSafeInteger(loc.end_line) && (loc.end_line as number) >= (line ?? 1) ? (loc.end_line as number) : line;
  let url = info.blobTemplate
    .replaceAll("{repo}", info.repo)
    .replaceAll("{commit}", loc.commit)
    .replaceAll("{path}", full.split("/").map(encodeURIComponent).join("/"));
  if (line === null) {
    url = url.replace(/#.*$/, "");
  } else {
    url = url.replaceAll("{line}", String(line)).replaceAll("{end_line}", String(end ?? line));
    if (end === line) url = url.replace(`#L${line}-L${line}`, `#L${line}`);
  }
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? parsed.toString() : null;
  } catch {
    return null;
  }
}

// The repository at a commit (the universe page's "commit" link).
export function treeUrl(info: LibraryInfo, commit: string): string | null {
  if (!COMMIT_RE.test(commit) || !GITHUB_RE.test(info.repo)) return null;
  return `${info.repo}/tree/${commit}`;
}
