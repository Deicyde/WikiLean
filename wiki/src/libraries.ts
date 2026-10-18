// Library registry for the formalization atlas: a Lean library's repository
// URI → its display name and the template that turns (commit, path, lines)
// into a pinned source link. The atlas never shows a docs page as evidence:
// doc-gen4 pages exist only for a library's latest build, so the pinned
// evidence is the source blob at the record's commit.
//
// Naming and links come from WikiLean, not from the record set (docs/ATLAS.md,
// contract note 6). A library is named by the seeds below, then the
// FormalFrontier organisation pattern, then owner/repo for any other GitHub
// repository. A universe entry's display_name is used only for a repository
// that is not seeded, and never when it would name a seeded library, so a
// universe cannot relabel some repository as Mathlib. Every "Source @sha7"
// link uses GitHub's blob scheme for the library's own github.com/<owner>/<repo>:
// a universe's blob_url_template is ignored (the builder refuses one), and a
// non-GitHub repository gets no source link rather than a guessed one. The
// universe's src_dir (the directory a record's path is relative to) is kept.

export interface UniverseLibrary {
  id: string;
  repo: string;
  commit: string;
  display_name?: string;
  // Not part of the contract (the builder refuses it); ignored if present.
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
// A repository URL as build_store.REPO_RE accepts it: https, a dotted host, one
// to four path segments, no query or fragment.
const REPO_RE = /^https:\/\/[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?:\/[A-Za-z0-9_.~-]{1,100}){1,4}$/;

const dotSegment = (part: string) => part === "." || part === "..";

// [owner, repo] of a github.com repository URL, or null. "." and ".." are not
// names: "https://github.com/../.." would normalise to another URL entirely.
function githubParts(repo: string): [string, string] | null {
  const match = GITHUB_RE.exec(repo);
  if (!match || dotSegment(match[1]) || dotSegment(match[2])) return null;
  return [match[1], match[2]];
}

// The repository itself as a link target (the universe page), or null.
export function repoHref(repo: string): string | null {
  if (!REPO_RE.test(repo) || repo.split("/").slice(3).some(dotSegment)) return null;
  return repo;
}

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

// Comparison form of a library name: lowercase, single spaces, no trailing
// "(qualifier)". A display_name with the form of a seed's name is not used.
function nameForm(name: string): string {
  return name.replace(/\s*\([^)]*\)\s*$/, "").replace(/\s+/g, " ").trim().toLowerCase();
}
const SEED_NAMES = new Set(SEEDS.flatMap((seed) => [seed.name, seed.name.replace(/\s*\([^)]*\)\s*$/, "")]).map(nameForm));

function cleanRepo(repo: string): string {
  return repo.trim().replace(/\/+$/, "").replace(/\.git$/i, "");
}

export function libraryInfo(repo: string, entry?: UniverseLibrary | null): LibraryInfo {
  const clean = cleanRepo(repo);
  const seed = SEED_BY_KEY.get(repoKey(clean));
  const github = githubParts(clean);
  const ff = github && github[0].toLowerCase() === "formalfrontier" ? github[1] : null;
  let name = seed?.name ?? (ff ? `FormalFrontier · ${ff}` : github ? `${github[0]}/${github[1]}` : clean);
  const blobTemplate: string | null = github ? GITHUB_BLOB : null;
  let srcDir: string | null = seed?.srcDir ?? null;
  if (entry) {
    const display = typeof entry.display_name === "string" ? entry.display_name.replace(/\s+/g, " ").trim() : "";
    // "FormalFrontier · …" names belong to the FormalFrontier organisation.
    const claimsFormalFrontier = !ff && /^formal\s*frontier\b/i.test(display);
    if (!seed && display && !SEED_NAMES.has(nameForm(display)) && !claimsFormalFrontier) name = display;
    if (typeof entry.src_dir === "string" && safeRelativePath(entry.src_dir)) srcDir = entry.src_dir;
  }
  const short = name.replace(/\s*\([^)]*\)\s*$/, "") || name;
  return { repo: clean, name, short, blobTemplate, srcDir };
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
  if (!COMMIT_RE.test(commit) || !githubParts(info.repo)) return null;
  return `${info.repo}/tree/${commit}`;
}
