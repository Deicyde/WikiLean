// The site-wide write brake (the signed public-editing plan, section 2.5;
// migration 0019 creates the table and its one row).
//
// There is no Worker-only hotfix path: `wiki/src` ships only in a new release,
// a fresh P1B evidence bundle and Jack's approval, which the release chain puts
// at about 5.5 hours. Opening public editing with that and no lever would make
// an abuse bug a 5.5-hour exposure. The brake is the lever: one D1 statement by
// an admin, no deploy, and every community write surface and every agent write
// stops in a second.
//
//   UPDATE site_settings SET value = 'locked', updated_at = <now in ms>
//    WHERE key = 'site_write_mode';            -- and 'open' to reopen
//
// Kept in a module of its own, with no dependency beyond the environment, so
// the Brain's write paths can read it without pulling in the articles plane.

import type { Context } from "hono";
import type { Env } from "./env.js";

export const SITE_WRITE_MODE_KEY = "site_write_mode";
export const WRITE_MODE_OPEN = "open";

// Decision 5, option (b) of the signed public-editing plan: editing is off on
// the annotated article plane (/:slug). Migration 0019 seeds 'off'; an admin
// restores the plane's editing exactly as it was with one D1 write and no
// release. See index.ts SLUG_EDITING_OFF.
export const SLUG_EDITING_KEY = "slug_editing";

export const BRAKE_MESSAGE =
  "editing is paused site-wide; a maintainer closed it and it will reopen without a deploy";

function settingsMissing(error: unknown): boolean {
  let current: unknown = error;
  for (let depth = 0; depth < 5 && current; depth += 1) {
    const message = current instanceof Error ? current.message : String(current);
    if (message.includes("no such table: site_settings")) return true;
    current = typeof current === "object" && current !== null && "cause" in current ? (current as { cause?: unknown }).cause : null;
  }
  return false;
}

// The brake is read per write. 'open' allows it; ANY other value refuses it —
// fail closed, because the brake exists to stop a spree in a second. R1 knows
// 'open' and 'locked'; 'autoconfirmed' (the trust tiers of release R3) is not a
// tier anything can hold yet, so in R1 it refuses everything, which is the safe
// reading of an unknown value.
//
// A missing table reads as 'open': migration 0019 creates it alongside
// article_tags, so without it there is nothing to brake on this plane, and
// bricking the Brain's writes on an unapplied migration would be worse than the
// exposure it closes.
export async function siteWriteMode(env: Pick<Env, "DB">): Promise<string> {
  try {
    const { results } = await env.DB.prepare(
      "SELECT value FROM site_settings WHERE key = ?",
    ).bind(SITE_WRITE_MODE_KEY).all<{ value: string }>();
    return results[0]?.value ?? WRITE_MODE_OPEN;
  } catch (error) {
    if (!settingsMissing(error)) throw error;
    return WRITE_MODE_OPEN;
  }
}

// Whether the annotated article plane still takes edits. Without the row (an
// unapplied migration 0019, which the promoter will not let into production
// alongside this code) the plane behaves exactly as it did before R1: the
// setting turns editing OFF, it never turns it on.
export async function slugEditingAllowed(env: Pick<Env, "DB">): Promise<boolean> {
  try {
    const { results } = await env.DB.prepare(
      "SELECT value FROM site_settings WHERE key = ?",
    ).bind(SLUG_EDITING_KEY).all<{ value: string }>();
    const value = results[0]?.value;
    return value === undefined || value === "on";
  } catch (error) {
    if (!settingsMissing(error)) throw error;
    return true;
  }
}

// null = the write may proceed; a Response = the brake is on (503, no-store).
export async function writeBrake(c: Context<{ Bindings: Env }>): Promise<Response | null> {
  const mode = await siteWriteMode(c.env);
  if (mode === WRITE_MODE_OPEN) return null;
  console.log(JSON.stringify({ event: "write-brake", path: c.req.path, mode }));
  return c.json({ ok: false, error: BRAKE_MESSAGE, write_mode: mode }, 503, { "Cache-Control": "no-store" });
}
