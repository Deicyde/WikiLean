// Test-only shims for the Worker's bindings, so the real Hono app
// (src/index.ts) can be driven end-to-end via app.request(path, init, env)
// against Node's built-in SQLite — no wrangler/miniflare/network needed.
//
// makeD1() implements the slice of the D1Database contract that
// drizzle-orm/d1 actually uses (see node_modules/drizzle-orm/d1/session.js):
//   client.prepare(sql).bind(...params).run()   → inserts/updates; the app
//       reads `result.meta.changes` (CAS guard in the save handler).
//   client.prepare(sql).bind(...params).all()   → fields-less selects; needs
//       `{ results }` as row objects.
//   client.prepare(sql).bind(...params).raw()   → typed selects; needs rows
//       as positional value arrays in SELECT-clause order.
// batch()/exec()/first() are not exercised by the app and throw loudly.

import { DatabaseSync, type StatementSync } from "node:sqlite";

type SqliteParam = null | number | bigint | string | Uint8Array;

// D1 accepts JS booleans/undefined; node:sqlite does not. Normalize the few
// shapes drizzle can emit. (Dates never reach here — drizzle's column mappers
// convert timestamp/boolean modes to integers before binding.)
function toSqliteParam(v: unknown, i: number): SqliteParam {
  if (v === undefined || v === null) return null;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number" || typeof v === "bigint" || typeof v === "string") return v;
  if (v instanceof Uint8Array) return v;
  throw new Error(`d1shim: unsupported bind param at index ${i}: ${typeof v}`);
}

interface D1ShimMeta {
  duration: number;
  changes: number;
  last_row_id: number;
  rows_read: number;
  rows_written: number;
  changed_db: boolean;
  size_after: number;
}

function metaFor(changes: number, lastRowId: number): D1ShimMeta {
  return {
    duration: 0,
    changes,
    last_row_id: lastRowId,
    rows_read: 0,
    rows_written: changes,
    changed_db: changes > 0,
    size_after: 0,
  };
}

function makeStatement(db: DatabaseSync, sql: string, params: SqliteParam[]) {
  // Prepare lazily so a bad-SQL error surfaces at execution time (as it would
  // on real D1), not at .prepare() — drizzle prepares eagerly per query.
  const prep = (): StatementSync => db.prepare(sql);
  return {
    bind: (...args: unknown[]) => makeStatement(db, sql, args.map(toSqliteParam)),
    run: async () => {
      const r = prep().run(...params);
      return {
        success: true as const,
        results: [],
        meta: metaFor(Number(r.changes), Number(r.lastInsertRowid)),
      };
    },
    all: async () => {
      const results = prep().all(...params) as Record<string, unknown>[];
      return { success: true as const, results, meta: metaFor(0, 0) };
    },
    raw: async () => {
      const stmt = prep();
      // Positional rows in SELECT-clause order. node:sqlite returns objects;
      // columns() gives the authoritative result-column order and lets us
      // fail loudly on duplicate names (which an object row would collapse).
      const names = stmt.columns().map((c) => c.name);
      if (new Set(names).size !== names.length) {
        throw new Error(`d1shim: duplicate result columns in: ${sql}`);
      }
      const rows = stmt.all(...params) as Record<string, unknown>[];
      return rows.map((row) => names.map((n) => row[n]));
    },
    first: async () => {
      throw new Error("d1shim: first() not implemented");
    },
  };
}

export function makeD1(db: DatabaseSync) {
  return {
    prepare: (sql: string) => makeStatement(db, sql, []),
    batch: () => {
      throw new Error("d1shim: batch() not implemented");
    },
    exec: () => {
      throw new Error("d1shim: exec() not implemented");
    },
    dump: () => {
      throw new Error("d1shim: dump() not implemented");
    },
  };
}

// Minimal in-memory KV namespace. `store` is exposed so tests can pre-seed
// (e.g. the wp:<slug>:<revid> Wikipedia HTML key) and assert on cache writes.
export interface KVShim {
  store: Map<string, string>;
  get: (key: string) => Promise<string | null>;
  put: (key: string, value: string, opts?: unknown) => Promise<void>;
  delete: (key: string) => Promise<void>;
}

export function makeKV(seed?: Record<string, string>): KVShim {
  const store = new Map<string, string>(Object.entries(seed ?? {}));
  return {
    store,
    get: async (key) => store.get(key) ?? null,
    put: async (key, value) => {
      store.set(key, value);
    },
    delete: async (key) => {
      store.delete(key);
    },
  };
}
