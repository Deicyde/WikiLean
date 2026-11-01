// Anonymous rate-limit keys (limiterIp in src/security.ts): an IPv4 address keys
// whole, an IPv6 address by its /64, so one host can't rotate through its /64 for
// a fresh budget per address. Pins the parser and the key each anonymous limiter
// (flags, the CSP sink, /mcp, the Brain REST reads) receives.

import { describe, it, expect } from "vitest";
import { limiterIp } from "../src/security.js";
import { setup, post, get, blockNetwork, SLUG } from "./helpers/harness.js";

blockNetwork();

describe("limiterIp", () => {
  it("keeps IPv4 and ::/64 addresses (loopback, IPv4-mapped) whole", () => {
    for (const ip of ["203.0.113.7", "::ffff:203.0.113.7", "::ffff:cb00:7107", "::1"]) {
      expect(limiterIp(ip)).toBe(ip);
    }
  });

  it("keys an IPv6 address by its /64, however it is written", () => {
    for (const ip of ["2001:db8:1:2:3:4:5:6", "2001:0DB8:0001:0002::ffff", "2001:db8:1:2::", "2001:db8:1:2:aaaa::1"]) {
      expect(limiterIp(ip)).toBe("2001:db8:1:2::/64");
    }
    expect(limiterIp("2001:db8::1")).toBe("2001:db8:0:0::/64");
  });

  it("passes a missing header and anything it can't parse through unchanged", () => {
    for (const ip of [undefined, "", "unknown", "2001:db8::1::2", "1:2:3:4:5:6:7:8:9", "1:2:3:4:5:6:7:8::", "2001:db8::g", "64:ff9b::192.0.2.1"]) {
      expect(limiterIp(ip)).toBe(ip);
    }
  });
});

describe("anonymous limiters", () => {
  it("give every address in one IPv6 /64 the same key", async () => {
    const h = setup();
    const keys: string[] = [];
    const deny = {
      limit: async ({ key }: { key: string }) => {
        keys.push(key);
        return { success: false };
      },
    };
    Object.assign(h.env, { FLAG_LIMITER: deny, BRAIN_API_LIMITER: deny });
    for (const ip of ["2001:db8:1:2::a", "2001:db8:1:2:ffff::b"]) {
      expect((await post(h.env, `/api/flag/${SLUG}`, { reason: "other" }, { ip })).status).toBe(429);
      expect((await post(h.env, "/api/csp-report", {}, { ip })).status).toBe(429);
      expect((await post(h.env, "/mcp", { jsonrpc: "2.0", id: 1, method: "ping" }, { ip })).status).toBe(429);
      expect((await get(h.env, "/api/brain/cell?id=x", { ip })).status).toBe(429);
    }
    const once = ["flag:", "csp:", "mcp:", "brainapi-ip:"].map((prefix) => `${prefix}2001:db8:1:2::/64`);
    expect(keys).toEqual([...once, ...once]);
  });
});
