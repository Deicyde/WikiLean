// The serving rows the D1 store reads, built from a cell-asset tree the way
// brain/build_serving_db.py builds them, so the Worker tests can serve one
// fixture through both stores (brain-fixture.ts installBrainD1Fixture).
//
// This is a port, and a port can drift, so CI checks it against the real
// builder on every run: site/ops/test_brain_d1_import.py runs
// build_serving_db.build on two trees, the builder's own test fixture and the
// parity tree the Worker tests serve (written out by `parity-tree` below),
// runs this file on the same tree under `node --experimental-strip-types`, and
// compares every column listed in READ_COLUMNS, row by row (JSON columns as
// parsed values with their key order). Columns the Worker does not read yet
// (labels.libs and xy, supercells.n_sub, fhist and kids,
// hot_prefixes.live_sups, text_ids) are left out of the port and filled with
// placeholders by the fixture.
//
// Why a checked port rather than a committed SQL fixture the Python job
// regenerates: the rows then come from the same in-memory fixture the static
// store serves, with no generated file to keep in step, and the comparison
// still runs the builder's real code in CI.
//
// Erasable TypeScript only and no imports but node: built-ins, the standalone
// brain-name-parts.ts and the fixture's data (brain-fixture-data.ts), so node
// can run it without npm ci.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { nameParts } from "../../src/brain-name-parts.ts";
import { servingTreeFor } from "./brain-fixture-data.ts";

type Json = unknown;
type Obj = Record<string, Json>;
export type Cell = string | number | null;

export interface ServingTree {
  cells: Record<string, Obj>; // every cell's shard entry, by id
  aliases: Obj; // aliases.json
  labels: Obj[]; // labels.json
  supercells: Obj; // supercells.json
  traces: Record<string, Obj>; // the trace sidecar's rows, by pair
  xrefIndex: Record<string, string[]>; // xref_index.json
}

export interface ServingRows {
  tables: Record<string, { columns: string[]; rows: Cell[][] }>;
  longest_name: number;
  sup_names_from: number;
}

// The columns the D1 store reads (and the Python check compares), per table.
export const READ_COLUMNS: Record<string, string[]> = {
  cell_pages: ["seq", "id", "json"],
  aliases: ["seq", "kind", "key", "owner"],
  labels: ["seq", "ord", "id", "label", "aka", "f", "p"],
  supercells: ["seq", "ord", "path", "label", "aka", "parent", "fa", "frontier", "n_cells", "syn", "json"],
  name_keys: ["seq", "name_lc", "kind", "owner", "len16", "name"],
  hot_prefixes: ["seq", "prefix", "cells", "sups"],
  cell_under: ["seq", "anc", "ord"],
  synapse_traces: ["seq", "pair", "json"],
  xrefs: ["seq", "page_ord", "node_ord", "page", "node"],
};
export const JSON_COLUMNS = new Set(["json", "syn", "cells", "sups"]);

const AKA_SEP = "\u001f";
const ALIAS_KINDS: Array<[string, string]> = [["decls", "decl"], ["organs", "organ"], ["slugs", "slug"]];
const HOT_PREFIX_OWNERS = 100;
const QUERY_UNITS: [number, number] = [2, 200];

// Python's sorted() and SQLite's BINARY order: by code point.
export function codePointCompare(a: string, b: string): number {
  const x = Array.from(a);
  const y = Array.from(b);
  const n = Math.min(x.length, y.length);
  for (let i = 0; i < n; i++) {
    const d = x[i].codePointAt(0)! - y[i].codePointAt(0)!;
    if (d) return d;
  }
  return x.length - y.length;
}

function compact(value: Json): string {
  return JSON.stringify(value);
}

function obj(value: Json): Obj {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Obj) : {};
}

function list(value: Json): Json[] {
  return Array.isArray(value) ? value : [];
}

function str(value: Json): string {
  return value === null || value === undefined ? "None" : String(value);
}

function numbered(rows: Cell[][]): Cell[][] {
  return rows.map((row, i) => [i + 1, ...row]);
}

function slashPrefixes(path: string): string[] {
  const out: string[] = [];
  for (let i = 1; i < path.length; i++) if (path[i] === "/") out.push(path.slice(0, i));
  out.push(path);
  return out;
}

function searchPath(path: string): string {
  for (const prefix of ["path:", "frontier:"]) if (path.startsWith(prefix)) return path.slice(prefix.length);
  return path;
}

// name_keys rows in seq order (build_serving_db.name_rows).
function nameRows(labels: Obj[], supercells: Obj): Array<[string, string, string, number, string]> {
  const out: Array<[string, string, string, number, string]> = [];
  const owners: Array<[string, Array<[string, Json]>]> = [];
  for (const row of labels) {
    owners.push([String(row.id), [["label", row.label], ...list(row.aka).map((a): [string, Json] => ["aka", a])]]);
  }
  for (const [path, row] of Object.entries(supercells)) {
    const r = obj(row);
    owners.push([path, [
      ["slabel", r.label],
      ["spath", searchPath(path)],
      ...list(r.organs).map((o): [string, Json] => ["sorgan", obj(o).label]),
    ]]);
  }
  for (const [owner, names] of owners) {
    const seen = new Set<string>();
    for (const [kind, name] of names) {
      if (!name) continue;
      const nameLc = String(name).toLowerCase();
      const key = `${nameLc}\u0000${kind}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push([nameLc, kind, owner, nameLc.length, String(name)]);
    }
  }
  return out;
}

// hot_prefixes rows (build_serving_db.hot_prefix_rows), without live_sups.
function hotPrefixRows(keys: Array<{ seq: number; nameLc: string[]; owner: string; isSup: boolean }>, hotNames: number): Cell[][] {
  const hot: Array<[string, string, string]> = [];
  const walk = (prefix: string[], members: typeof keys) => {
    const depth = prefix.length + 1;
    const children = new Map<string, typeof keys>();
    for (const key of members) {
      if (key.nameLc.length < depth) continue;
      const child = key.nameLc.slice(0, depth).join("");
      const group = children.get(child) ?? [];
      group.push(key);
      children.set(child, group);
    }
    for (const [child, sub] of children) {
      if (sub.length <= hotNames) continue;
      const units = child.length;
      if (units >= QUERY_UNITS[0] && units <= QUERY_UNITS[1]) {
        const cells: number[] = [];
        const sups: number[] = [];
        const seen = [new Set<string>(), new Set<string>()];
        for (const k of sub) {
          const which = k.isSup ? 1 : 0;
          const target = k.isSup ? sups : cells;
          if (!seen[which].has(k.owner) && target.length < HOT_PREFIX_OWNERS) {
            seen[which].add(k.owner);
            target.push(k.seq);
          }
        }
        hot.push([child, compact(cells), compact(sups)]);
      }
      if (units < QUERY_UNITS[1]) walk(Array.from(child), sub);
    }
  };
  walk([], keys);
  hot.sort((a, b) => codePointCompare(a[0], b[0]));
  return hot;
}

/** The rows of every table the D1 store reads. `hotPrefixNames` is
 *  build_serving_db.HOT_PREFIX_NAMES (1,000; tests lower it to reach the hot
 *  path on a small fixture). */
export function servingRows(tree: ServingTree, opts: { hotPrefixNames?: number } = {}): ServingRows {
  const hotNames = opts.hotPrefixNames ?? 1000;
  const supercells = obj(tree.supercells.supercells);
  const labelIds = new Set(tree.labels.map((r) => String(r.id)));
  const cellIds = Object.keys(tree.cells);
  if (cellIds.length !== labelIds.size || cellIds.some((id) => !labelIds.has(id))) {
    throw new Error("labels.json and the cell shards disagree (build_serving_db refuses this tree)");
  }

  const cellPages = numbered(
    [...cellIds].sort(codePointCompare).map((id): Cell[] => [id, compact(tree.cells[id])]),
  );
  const aliases: Cell[][] = [];
  for (const [field, kind] of ALIAS_KINDS) {
    const map = obj(tree.aliases[field]);
    for (const key of Object.keys(map).sort(codePointCompare)) aliases.push([kind, key, String(map[key])]);
  }
  const labels = numbered(tree.labels.map((r, i): Cell[] => [
    i,
    String(r.id),
    (r.label ?? null) as Cell,
    list(r.aka).length ? list(r.aka).map(String).join(AKA_SEP) : null,
    (r.f ?? null) as Cell,
    (r.p ?? null) as Cell,
  ]));
  const supRows = numbered(Object.entries(supercells).map(([path, raw], ord): Cell[] => {
    const r = obj(raw);
    const organs = list(r.organs).map(obj);
    return [
      ord,
      path,
      (r.label ?? null) as Cell,
      organs.length ? organs.map((o) => str(o.label !== undefined && o.label !== null ? o.label : o.id)).join(AKA_SEP) : null,
      (r.parent ?? null) as Cell,
      (r.fa ?? null) as Cell,
      r.frontier ? 1 : null,
      list(r.cells).length || null,
      list(r.syn).length ? compact(r.syn) : null,
      compact(r),
    ];
  }));

  const names = nameRows(tree.labels, supercells);
  const nameKeys = numbered(names);
  const supNamesFrom = 1 + names.filter(([, kind]) => kind === "label" || kind === "aka").length;
  const longestName = Math.max(0, ...names.filter(([, kind]) => kind === "label" || kind === "aka").map(([, , , len]) => len));
  const hot = numbered(hotPrefixRows(
    names.map(([nameLc, , owner], i) => ({ seq: i + 1, nameLc: Array.from(nameLc), owner, isSup: i + 1 >= supNamesFrom })),
    hotNames,
  ));

  const folders = new Map<string, string[]>();
  for (const [path, raw] of Object.entries(supercells)) {
    for (const cid of list(obj(raw).cells)) {
      const got = folders.get(String(cid)) ?? [];
      got.push(path);
      folders.set(String(cid), got);
    }
  }
  const under: Array<[string, number]> = [];
  tree.labels.forEach((r, i) => {
    const ancestors = new Set<string>();
    for (const folder of [...(folders.get(String(r.id)) ?? []), ...(r.p ? [String(r.p)] : [])]) {
      for (const anc of slashPrefixes(folder)) ancestors.add(anc);
    }
    for (const anc of ancestors) under.push([anc, i]);
  });
  under.sort((a, b) => codePointCompare(a[0], b[0]) || a[1] - b[1]);

  const traces = numbered(
    Object.keys(tree.traces).sort(codePointCompare).map((pair): Cell[] => [pair, compact(tree.traces[pair])]),
  );
  const xrefs: Cell[][] = [];
  Object.entries(tree.xrefIndex).forEach(([page, nodes], pageOrd) => {
    [...new Set(nodes)].forEach((node, nodeOrd) => xrefs.push([pageOrd, nodeOrd, page, node]));
  });

  return {
    tables: {
      cell_pages: { columns: READ_COLUMNS.cell_pages, rows: cellPages },
      aliases: { columns: READ_COLUMNS.aliases, rows: numbered(aliases) },
      labels: { columns: READ_COLUMNS.labels, rows: labels },
      supercells: { columns: READ_COLUMNS.supercells, rows: supRows },
      name_keys: { columns: READ_COLUMNS.name_keys, rows: nameKeys },
      hot_prefixes: { columns: READ_COLUMNS.hot_prefixes, rows: hot },
      cell_under: { columns: READ_COLUMNS.cell_under, rows: numbered(under) },
      synapse_traces: { columns: READ_COLUMNS.synapse_traces, rows: traces },
      xrefs: { columns: READ_COLUMNS.xrefs, rows: numbered(xrefs) },
    },
    longest_name: longestName,
    sup_names_from: supNamesFrom,
  };
}

/** A built cell-asset tree on disk, as build_serving_db reads it. */
export function readTree(cellsDir: string, xrefPath: string): ServingTree {
  const read = (path: string): Json => JSON.parse(readFileSync(path, "utf8"));
  const manifest = obj(read(join(cellsDir, "manifest.json")));
  const cells: Record<string, Obj> = {};
  for (const key of Object.keys(obj(manifest.shards))) Object.assign(cells, obj(read(join(cellsDir, `${key}.json`))));
  const traceMeta = obj(manifest.traces);
  const traceDir = join(cellsDir, typeof traceMeta.dir === "string" ? traceMeta.dir : "traces");
  const traces: Record<string, Obj> = {};
  for (const key of Object.keys(obj(traceMeta.files))) Object.assign(traces, obj(read(join(traceDir, `${key}.json`))));
  return {
    cells,
    aliases: obj(read(join(cellsDir, "aliases.json"))),
    labels: list(read(join(cellsDir, "labels.json"))).map(obj),
    supercells: obj(read(join(cellsDir, "supercells.json"))),
    traces,
    xrefIndex: obj(read(xrefPath)) as Record<string, string[]>,
  };
}

/** `tree` on disk as build_serving_db reads it, the inverse of readTree:
 *  <dir>/cells (one shard, one trace file) and <dir>/xref_index.json. */
export function writeTree(tree: ServingTree, dir: string): void {
  const cells = join(dir, "cells");
  mkdirSync(join(cells, "traces"), { recursive: true });
  const write = (path: string, value: Json) => writeFileSync(path, JSON.stringify(value));
  write(join(cells, "manifest.json"), {
    _meta: { generated_at: "fixture" },
    shards: { all: Object.keys(tree.cells).length },
    traces: { dir: "traces", files: { all: Object.keys(tree.traces).length } },
  });
  write(join(cells, "all.json"), tree.cells);
  write(join(cells, "traces", "all.json"), tree.traces);
  write(join(cells, "labels.json"), tree.labels);
  write(join(cells, "aliases.json"), tree.aliases);
  write(join(cells, "supercells.json"), tree.supercells);
  write(join(dir, "xref_index.json"), tree.xrefIndex);
}

/** The FTS table the importer creates for a dataset's names
 *  (site/ops/brain_d1_import.py fts_create, which the Python test compares). */
export function namesFtsCreate(mode: "trigram" | "parts", hex: string): string {
  if (!/^[0-9a-f]{64}$/.test(hex)) throw new Error("namesFtsCreate: malformed dataset hex");
  return mode === "trigram"
    ? `CREATE VIRTUAL TABLE IF NOT EXISTS names_${hex} USING fts5(name, content='', contentless_delete=1, tokenize='trigram')`
    : `CREATE VIRTUAL TABLE IF NOT EXISTS names_${hex} USING fts5(parts, content='', contentless_delete=1, prefix='2 3')`;
}

// CLI, for site/ops/test_brain_d1_import.py:
//   rows <cells dir> <xref_index.json> <hot prefix names>   the rows, as JSON
//   parity-tree <dir>                                       write the Worker tests' parity tree (writeTree)
//   fts <trigram | parts> <hex>                             namesFtsCreate's statement
//   parts                                                   nameParts of each string of a JSON list on stdin
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [command, ...args] = process.argv.slice(2);
  if (command === "rows") {
    const rows = servingRows(readTree(args[0], args[1]), { hotPrefixNames: Number(args[2]) });
    process.stdout.write(JSON.stringify(rows));
  } else if (command === "parity-tree") {
    writeTree(servingTreeFor({ parity: true }), args[0]);
    process.stdout.write("null");
  } else if (command === "fts") {
    process.stdout.write(JSON.stringify(namesFtsCreate(args[0] === "parts" ? "parts" : "trigram", args[1])));
  } else if (command === "parts") {
    const input = JSON.parse(readFileSync(0, "utf8")) as string[];
    process.stdout.write(JSON.stringify(input.map(nameParts)));
  } else {
    process.stderr.write("usage: brain-d1-rows.ts rows <cells dir> <xref_index.json> <hot prefix names> | parity-tree <dir> | fts <mode> <hex> | parts\n");
    process.exit(2);
  }
}
