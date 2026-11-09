// Loopback-only fixture for Python's real client → Worker → validator test.
// No production credentials, external fetches, or model calls.
import { serve } from "@hono/node-server";
import { app } from "../../src/index.js";
import { CC0_AGREEMENT_VERSION } from "../../src/articles-overlay.js";
import { setup } from "./fixture.js";
const harness = setup();
harness.db.prepare("INSERT INTO user_agreements (user_id,cc0_version,accepted_at) VALUES (?,?,?)")
  .run("u-human", CC0_AGREEMENT_VERSION, Date.now());
globalThis.fetch = async () => { throw new Error("unexpected external request in intake fixture"); };
const server = serve({ hostname: "127.0.0.1", port: 0, fetch: request => app.fetch(request, harness.env) }, info => {
  console.log(JSON.stringify({ origin: `http://127.0.0.1:${info.port}` }));
});
process.on("SIGTERM", () => server.close(() => process.exit(0)));
