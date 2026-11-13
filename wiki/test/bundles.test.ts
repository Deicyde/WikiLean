// Token-gated index bundle downloads (src/bundles.ts, migration 0022): registration by the
// pipeline bearer from the bucket's own bytes, the lever, who may download, whole and
// ranged reads straight from R2, the per-owner daily quota and the download log.
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { describe, it, expect } from "vitest";
import { app } from "../src/index.js";
import { CC0_AGREEMENT_VERSION } from "../src/articles-overlay.js";
import { BUNDLE_SCHEMA, INDEX_DAILY_BYTES, byteRange, parseManifest } from "../src/bundles.js";
import { setup, post, get, blockNetwork, PIPELINE_TOKEN, type Harness } from "./helpers/harness.js";

blockNetwork();
const sha256 = (bytes: string | Uint8Array) => createHash("sha256").update(bytes).digest("hex");
const encoder = new TextEncoder();

interface Bucket {
  objects: Map<string, Uint8Array>;
  gets: Array<{ key: string; range?: { offset: number; length: number } }>;
  get(key: string, options?: { range?: { offset: number; length: number } }): Promise<unknown>;
  list(options: { prefix: string; cursor?: string; limit?: number }): Promise<unknown>;
}

function fakeContrib(objects = new Map<string, Uint8Array>()): Bucket {
  const gets: Bucket["gets"] = [];
  return {
    objects, gets,
    async get(key, options) {
      gets.push({ key, range: options?.range });
      const whole = objects.get(key);
      if (!whole) return null;
      const range = options?.range;
      const part = range ? whole.slice(range.offset, range.offset + range.length) : whole;
      return {
        key, size: whole.length,
        body: new Blob([part]).stream(),
        arrayBuffer: async () => part.slice().buffer,
        text: async () => new TextDecoder().decode(part),
      };
    },
    async list({ prefix }) {
      return { objects: [...objects].filter(([key]) => key.startsWith(prefix)).map(([key, value]) => ({ key, size: value.length })),
        truncated: false };
    },
  };
}

const INDEX = '{"schema":"wikilean.pipeline.index/1","universe_sha256":"' + "a".repeat(64) + '"}\n{"declaration":"Alpha.foo"}\n';
function bundleFiles(overrides: Record<string, unknown> = {}, extra: Record<string, string> = {}) {
  const contents: Record<string, string> = {
    "index/decl-index.jsonl": INDEX,
    "search/declarations.tsv": "alpha\tAlpha.foo\ttheorem\n",
    "licences/alpha/LICENSE": "Apache License 2.0\n",
    "LICENCES.spdx.json": '{"packages":[{"name":"alpha"}]}\n',
    ...extra,
  };
  const files = Object.fromEntries(Object.entries(contents).map(([rel, text]) =>
    [rel, { sha256: sha256(text), bytes: encoder.encode(text).length }]));
  const manifest = {
    schema: BUNDLE_SCHEMA, bundle: "u-test/v1",
    universe: { id: "u-test", universe_sha256: "a".repeat(64) },
    index: { path: "index/decl-index.jsonl", sha256: files["index/decl-index.jsonl"].sha256 },
    licences: { document: "LICENCES.spdx.json", no_licence_recorded: [], use_restricted: ["alpha"],
      texts: { alpha: ["licences/alpha/LICENSE"] } },
    bytes: Object.values(files).reduce((sum, file) => sum + file.bytes, 0),
    files,
    ...overrides,
  };
  const raw = encoder.encode(JSON.stringify(manifest));
  const sha = sha256(raw);
  const objects = new Map<string, Uint8Array>([[`index/v1/${sha}/MANIFEST.json`, raw]]);
  for (const [rel, text] of Object.entries(contents)) objects.set(`index/v1/${sha}/${rel}`, encoder.encode(text));
  return { sha, raw, objects, contents };
}

function bind(h: Harness, bucket: Bucket) {
  (h.env as unknown as { CONTRIB: Bucket }).CONTRIB = bucket;
}
async function agent(h: Harness, owner = "u-human", agreed = true) {
  if (agreed) h.db.prepare("INSERT OR IGNORE INTO user_agreements (user_id,cc0_version,accepted_at) VALUES (?,?,?)")
    .run(owner, CC0_AGREEMENT_VERSION, Date.now());
  const res = await post(h.env, "/api/agents", { name: "Laptop" }, { user: owner });
  expect(res.status).toBe(201);
  return await res.json() as { token: string; agent: { id: string } };
}
function register(h: Harness, sha: string, bearer = PIPELINE_TOKEN) {
  return post(h.env, "/api/index/bundles", { manifest_sha256: sha }, { bearer, origin: null });
}
function lever(h: Harness, value: string) {
  h.db.prepare("UPDATE site_settings SET value=? WHERE key='index_downloads'").run(value);
}
function fetchFile(h: Harness, path: string, token: string, headers: Record<string, string> = {}, method = "GET") {
  return app.request(path, { method, headers: { Authorization: `Bearer ${token}`, ...headers } }, h.env);
}
function logged(h: Harness) {
  return h.db.prepare("SELECT owner_id, agent_id, path, range_start, bytes FROM index_downloads ORDER BY id").all();
}
// A registered bundle, the lever open and an agent whose owner accepted CC0.
async function ready() {
  const h = setup();
  const bundle = bundleFiles();
  const bucket = fakeContrib(new Map(bundle.objects));
  bind(h, bucket);
  expect((await register(h, bundle.sha)).status).toBe(201);
  lever(h, "open");
  const a = await agent(h);
  return { h, bundle, bucket, a, base: `/api/index/bundles/${bundle.sha}` };
}

describe("migration 0022", () => {
  it("seeds the lever shut, and re-applying it changes nothing", () => {
    const h = setup();
    expect(h.db.prepare("SELECT value FROM site_settings WHERE key='index_downloads'").get()).toEqual({ value: "closed" });
    lever(h, "open");
    h.db.exec(readFileSync(new URL("../migrations/0022_index_bundles.sql", import.meta.url), "utf8"));
    expect(h.db.prepare("SELECT value FROM site_settings WHERE key='index_downloads'").get()).toEqual({ value: "open" });
  });
});

describe("registering a bundle", () => {
  it("takes the pipeline bearer only, and reads the manifest from the bucket", async () => {
    const h = setup();
    const bundle = bundleFiles();
    bind(h, fakeContrib(new Map(bundle.objects)));
    const a = await agent(h);
    expect((await register(h, bundle.sha, a.token)).status).toBe(403);
    expect((await post(h.env, "/api/index/bundles", { manifest_sha256: bundle.sha }, { user: "u-admin" })).status).toBe(403);
    const res = await register(h, bundle.sha);
    expect(res.status).toBe(201);
    const body = await res.json() as { bundle: Record<string, unknown> };
    expect(body.bundle).toMatchObject({ manifest_sha256: bundle.sha, bundle: "u-test/v1", universe_sha256: "a".repeat(64),
      index_path: "index/decl-index.jsonl", restricted: ["alpha"], manifest_url: `/api/index/bundles/${bundle.sha}` });
    // A replay is the same row.
    expect((await register(h, bundle.sha)).status).toBe(200);
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM index_bundles").get()).toEqual({ n: 1 });
  });

  it("refuses a bundle the bucket does not hold whole, or one not fit to hand out", async () => {
    const h = setup();
    const good = bundleFiles();
    const bucket = fakeContrib();
    bind(h, bucket);
    const code = async (sha: string) => (await (await register(h, sha)).json() as { code: string }).code;
    // nothing there
    expect(await code(good.sha)).toBe("bundle_incomplete");
    // a listed file missing
    for (const [key, value] of good.objects) if (!key.endsWith("LICENSE")) bucket.objects.set(key, value);
    expect(await code(good.sha)).toBe("bundle_incomplete");
    // bytes that do not hash to the key they are stored under
    bucket.objects.set(`index/v1/${"b".repeat(64)}/MANIFEST.json`, good.raw);
    expect(await code("b".repeat(64))).toBe("manifest_digest");
    // an older schema, and a bundle without its licence texts
    for (const overrides of [{ schema: "wikilean.pipeline.index-bundle/1" },
      { licences: { document: "LICENCES.spdx.json", no_licence_recorded: [], use_restricted: [], texts: {} } }]) {
      const odd = bundleFiles(overrides);
      for (const [key, value] of odd.objects) bucket.objects.set(key, value);
      expect(await code(odd.sha), JSON.stringify(overrides)).toBe("manifest_shape");
    }
    expect((await register(h, "not-a-sha")).status).toBe(422);
    expect(h.db.prepare("SELECT COUNT(*) AS n FROM index_bundles").get()).toEqual({ n: 0 });
  });

  it("stops under the brake, and without the bucket", async () => {
    const h = setup({ siteWriteMode: "locked" });
    const bundle = bundleFiles();
    bind(h, fakeContrib(new Map(bundle.objects)));
    expect((await register(h, bundle.sha)).status).toBe(503);
    const unbound = setup();
    expect(((await (await register(unbound, bundle.sha)).json()) as { code: string }).code).toBe("not_configured");
  });
});

describe("downloading", () => {
  it("serves nothing until the lever is opened, then the manifest's exact bytes", async () => {
    const { h, bundle, a, base } = await ready();
    lever(h, "closed");
    const shut = await fetchFile(h, base, a.token);
    expect(shut.status).toBe(503);
    expect((await shut.json() as { code: string }).code).toBe("downloads_closed");
    lever(h, "open");
    const res = await fetchFile(h, base, a.token);
    expect(res.status).toBe(200);
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(bundle.raw);
    expect(res.headers.get("ETag")).toBe(`"${bundle.sha}"`);
    expect(res.headers.get("Cache-Control")).toBe("private, no-store");
    expect(res.headers.get("Content-Type")).toBe("application/json");
  });

  it("refuses anyone but an owned agent whose owner accepted CC0", async () => {
    const { h, base } = await ready();
    expect((await get(h.env, base)).status).toBe(401);
    expect((await get(h.env, base, { user: "u-human" })).status).toBe(403);
    expect((await get(h.env, base, { bearer: PIPELINE_TOKEN })).status).toBe(403);
    const unagreed = await agent(h, "u-patroller", false);
    const res = await fetchFile(h, base, unagreed.token);
    expect(res.status).toBe(403);
    expect((await res.json() as { code: string }).code).toBe("agreement_required");
    expect(logged(h)).toEqual([]);
  });

  it("streams a listed file whole or by one range, and logs each range it served", async () => {
    const { h, bundle, bucket, a, base } = await ready();
    const path = `${base}/files/index/decl-index.jsonl`;
    const text = bundle.contents["index/decl-index.jsonl"];
    const size = encoder.encode(text).length;
    const whole = await fetchFile(h, path, a.token);
    expect(whole.status).toBe(200);
    expect(await whole.text()).toBe(text);
    expect(whole.headers.get("Content-Length")).toBe(String(size));
    expect(whole.headers.get("Content-Disposition")).toBe('attachment; filename="decl-index.jsonl"');
    expect(whole.headers.get("Accept-Ranges")).toBe("bytes");

    const part = await fetchFile(h, path, a.token, { Range: "bytes=2-5" });
    expect(part.status).toBe(206);
    expect(await part.text()).toBe(text.slice(2, 6));
    expect(part.headers.get("Content-Range")).toBe(`bytes 2-5/${size}`);
    const tail = await fetchFile(h, path, a.token, { Range: "bytes=-3" });
    expect(await tail.text()).toBe(text.slice(-3));
    const rest = await fetchFile(h, path, a.token, { Range: `bytes=${size - 4}-` });
    expect(await rest.text()).toBe(text.slice(size - 4));
    // A stale If-Range or several ranges get the whole file.
    const stale = await fetchFile(h, path, a.token, { Range: "bytes=2-5", "If-Range": '"' + "0".repeat(64) + '"' });
    expect(stale.status).toBe(200);
    const several = await fetchFile(h, path, a.token, { Range: "bytes=0-1,3-4" });
    expect(several.status).toBe(200);
    const past = await fetchFile(h, path, a.token, { Range: `bytes=${size}-` });
    expect(past.status).toBe(416);
    expect(past.headers.get("Content-Range")).toBe(`bytes */${size}`);

    expect(bucket.gets.filter(g => g.key.endsWith("decl-index.jsonl")).map(g => g.range))
      .toEqual([undefined, { offset: 2, length: 4 }, { offset: size - 3, length: 3 }, { offset: size - 4, length: 4 }, undefined, undefined]);
    expect(logged(h)).toEqual([
      { owner_id: "u-human", agent_id: a.agent.id, path: "index/decl-index.jsonl", range_start: 0, bytes: size },
      { owner_id: "u-human", agent_id: a.agent.id, path: "index/decl-index.jsonl", range_start: 2, bytes: 4 },
      { owner_id: "u-human", agent_id: a.agent.id, path: "index/decl-index.jsonl", range_start: size - 3, bytes: 3 },
      { owner_id: "u-human", agent_id: a.agent.id, path: "index/decl-index.jsonl", range_start: size - 4, bytes: 4 },
      { owner_id: "u-human", agent_id: a.agent.id, path: "index/decl-index.jsonl", range_start: 0, bytes: size },
      { owner_id: "u-human", agent_id: a.agent.id, path: "index/decl-index.jsonl", range_start: 0, bytes: size },
    ]);
  });

  it("serves only what the manifest lists, as it lists it", async () => {
    const { h, a, base } = await ready();
    for (const rel of ["index/missing.jsonl", "index%2Fdecl-index.jsonl", "MANIFEST.json", "index/decl-index.jsonl/"]) {
      expect((await fetchFile(h, `${base}/files/${rel}`, a.token)).status, rel).toBe(404);
    }
    expect((await fetchFile(h, `/api/index/bundles/${"c".repeat(64)}`, a.token)).status).toBe(404);
    expect(logged(h)).toEqual([]);
  });

  it("answers If-None-Match and HEAD without reading the bucket or logging", async () => {
    const { h, bundle, bucket, a, base } = await ready();
    const path = `${base}/files/search/declarations.tsv`;
    const etag = `"${sha256(bundle.contents["search/declarations.tsv"])}"`;
    expect((await fetchFile(h, path, a.token, { "If-None-Match": etag })).status).toBe(304);
    const head = await fetchFile(h, path, a.token, {}, "HEAD");
    expect(head.status).toBe(200);
    expect(head.headers.get("Content-Length")).toBe(String(encoder.encode(bundle.contents["search/declarations.tsv"]).length));
    expect(await head.text()).toBe("");
    expect(bucket.gets.filter(g => g.key.endsWith("declarations.tsv"))).toEqual([]);
    expect(logged(h)).toEqual([]);
  });

  it("stops the next request when the bundle is retired or the token revoked", async () => {
    const { h, a, base } = await ready();
    expect((await fetchFile(h, base, a.token)).status).toBe(200);
    h.db.prepare("UPDATE index_bundles SET state='retired'").run();
    expect((await fetchFile(h, base, a.token)).status).toBe(404);
    h.db.prepare("UPDATE index_bundles SET state='live'").run();
    h.db.prepare("UPDATE agent_tokens SET revoked_at=1 WHERE agent_id=?").run(a.agent.id);
    expect((await fetchFile(h, base, a.token)).status).toBe(401);
  });

  it("counts every agent of one owner against one daily quota", async () => {
    const { h, bundle, a, base } = await ready();
    const second = await agent(h);
    const path = `${base}/files/index/decl-index.jsonl`;
    const size = encoder.encode(bundle.contents["index/decl-index.jsonl"]).length;
    h.db.prepare("INSERT INTO index_downloads (owner_id,agent_id,bundle_sha256,path,range_start,bytes,at) VALUES (?,?,?,?,?,?,?)")
      .run("u-human", a.agent.id, bundle.sha, "index/decl-index.jsonl", 0, INDEX_DAILY_BYTES - 3, Date.now());
    for (const token of [a.token, second.token]) {
      const over = await fetchFile(h, path, token);
      expect(over.status).toBe(429);
      expect((await over.json() as { code: string }).code).toBe("quota_bytes");
      expect(Number(over.headers.get("Retry-After"))).toBeGreaterThanOrEqual(60);
    }
    // what is left of the day still downloads
    expect((await fetchFile(h, path, second.token, { Range: "bytes=0-2" })).status).toBe(206);
    expect((await fetchFile(h, path, second.token, { Range: "bytes=0-0" })).status).toBe(429);
    // another owner's agents have their own
    const other = await agent(h, "u-admin");
    expect((await fetchFile(h, path, other.token)).status).toBe(200);
    expect(size).toBeGreaterThan(3);
  });

  it("answers 503 not configured without the bucket", async () => {
    const { h, a, base } = await ready();
    (h.env as unknown as { CONTRIB?: Bucket }).CONTRIB = undefined;
    const res = await fetchFile(h, base, a.token);
    expect(res.status).toBe(503);
    expect((await res.json() as { code: string }).code).toBe("not_configured");
  });
});

describe("listing the bundles", () => {
  it("shows an agent the live bundles and whether downloads are open", async () => {
    const { h, bundle, a } = await ready();
    const res = await fetchFile(h, "/api/index/bundles", a.token);
    expect(res.status).toBe(200);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    const body = await res.json() as { downloads_open: boolean; bundles: Array<{ manifest_sha256: string }> };
    expect(body.downloads_open).toBe(true);
    expect(body.bundles.map(b => b.manifest_sha256)).toEqual([bundle.sha]);
    h.db.prepare("UPDATE index_bundles SET state='retired'").run();
    expect((await (await fetchFile(h, "/api/index/bundles", a.token)).json() as { bundles: unknown[] }).bundles).toEqual([]);
    expect((await get(h.env, "/api/index/bundles")).status).toBe(401);
  });
});

describe("byteRange", () => {
  it("reads one range as RFC 9110 does, and ignores what it may", () => {
    const etag = '"x"';
    expect(byteRange(undefined, undefined, etag, 10)).toBeNull();
    expect(byteRange("bytes=0-4", undefined, etag, 10)).toEqual({ offset: 0, length: 5 });
    expect(byteRange("bytes=8-20", undefined, etag, 10)).toEqual({ offset: 8, length: 2 });
    expect(byteRange("bytes=-4", undefined, etag, 10)).toEqual({ offset: 6, length: 4 });
    expect(byteRange("bytes=-40", undefined, etag, 10)).toEqual({ offset: 0, length: 10 });
    expect(byteRange("bytes=3-", undefined, etag, 10)).toEqual({ offset: 3, length: 7 });
    expect(byteRange("bytes=10-", undefined, etag, 10)).toBe("unsatisfiable");
    expect(byteRange("bytes=-0", undefined, etag, 10)).toBe("unsatisfiable");
    expect(byteRange("bytes=5-2", undefined, etag, 10)).toBeNull();
    expect(byteRange("bytes=0-1,4-5", undefined, etag, 10)).toBeNull();
    expect(byteRange("items=0-1", undefined, etag, 10)).toBeNull();
    expect(byteRange("bytes=0-4", '"y"', etag, 10)).toBeNull();
    expect(byteRange("bytes=0-4", etag, etag, 10)).toEqual({ offset: 0, length: 5 });
  });
});

describe("parseManifest", () => {
  it("refuses a path that would need encoding or leave the bundle", () => {
    for (const rel of ["a b.tsv", "../x", ".hidden", "x//y", "-x"]) {
      const odd = bundleFiles({}, { [rel]: "?" });
      const manifest = JSON.parse(new TextDecoder().decode(odd.raw));
      expect(typeof parseManifest(manifest), rel).toBe("string");
    }
    const good = JSON.parse(new TextDecoder().decode(bundleFiles().raw));
    expect(typeof parseManifest(good)).toBe("object");
  });
});
