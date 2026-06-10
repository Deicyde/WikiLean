import { sqliteTable, text, integer, index } from "drizzle-orm/sqlite-core";

// Page-revision model (Wikipedia-style): `articles` holds the current state;
// every save appends a full snapshot to `revisions` (audit log + revert source).

export const articles = sqliteTable("articles", {
  slug: text("slug").primaryKey(),
  wikipediaTitle: text("wikipedia_title").notNull(),
  displayTitle: text("display_title").notNull(),
  wikidataQid: text("wikidata_qid"),
  revid: integer("revid"), // pinned Wikipedia revision the annotations target
  // latest_revid / last_upstream_check are drift-detection bookkeeping; writes
  // to them must NEVER bump `version` (cache invariant — staleness UI is
  // injected per-request, never baked into the cached base page).
  latestRevid: integer("latest_revid"), // newest upstream revid seen (null = unknown)
  lastUpstreamCheck: integer("last_upstream_check"), // ms; null = never checked
  annotations: text("annotations").notNull(), // JSON array (same shape as the sidecar files)
  schemaVersion: integer("schema_version").notNull().default(3), // annotation-blob schema generation (v4 = formalizations[], deferred)
  version: integer("version").notNull().default(1), // bumped each save → busts the render cache
  createdAt: integer("created_at").notNull(),
  updatedAt: integer("updated_at").notNull(),
});

export const revisions = sqliteTable(
  "revisions",
  {
    id: integer("id").primaryKey({ autoIncrement: true }),
    slug: text("slug").notNull(),
    userId: text("user_id"), // null = system/seed
    annotations: text("annotations").notNull(), // full snapshot AFTER this edit
    comment: text("comment"),
    kind: text("kind").notNull().default("edit"), // edit | revert | seed | pipeline | contribution
    meta: text("meta"), // JSON: run_id, model, tokens, cost, mathlib_sha, auth_mode, approved_by, ...
    parentId: integer("parent_id"), // revision this edit was based on (no FK — SQLite ALTER can't add one)
    createdAt: integer("created_at").notNull(),
  },
  (t) => [index("idx_revisions_slug").on(t.slug, t.createdAt)],
);

// THE single work table (binding decision — absorbs the would-be
// article_updates). Feeds GET /api/work with one ORDER BY: flagged > drifted
// > human-edited-since-review > oldest-reviewed > new. Latest-revid data
// lives on `articles`, never duplicated here.
export const moderationState = sqliteTable(
  "moderation_state",
  {
    slug: text("slug")
      .primaryKey()
      .references(() => articles.slug),
    lastReviewedAt: integer("last_reviewed_at"), // ms; null = never reviewed
    lastReviewedVersion: integer("last_reviewed_version"), // articles.version at last review
    wpDrifted: integer("wp_drifted", { mode: "boolean" }).notNull().default(false), // upstream moved past pinned revid
    flagCount: integer("flag_count").notNull().default(0),
    state: text("state"), // null = normal; update-flow: 'needs_human' | 'moved' | 'deleted'
    proposal: text("proposal"), // JSON: pending re-anchor payload awaiting review
    updatedAt: integer("updated_at"), // ms
  },
  (t) => [index("idx_moderation_state_reviewed").on(t.lastReviewedAt)],
);

// better-auth core tables. Property names must match better-auth's field names;
// date fields use timestamp mode and emailVerified uses boolean mode, as
// better-auth's SQLite adapter expects. `role` is a better-auth additionalField.
export const users = sqliteTable("users", {
  id: text("id").primaryKey(),
  name: text("name"),
  email: text("email"),
  emailVerified: integer("email_verified", { mode: "boolean" }).notNull().default(false),
  image: text("image"),
  role: text("role").notNull().default("user"),
  createdAt: integer("created_at", { mode: "timestamp" }).notNull(),
  updatedAt: integer("updated_at", { mode: "timestamp" }).notNull(),
});

export const sessions = sqliteTable("sessions", {
  id: text("id").primaryKey(),
  userId: text("user_id")
    .notNull()
    .references(() => users.id),
  token: text("token").notNull(),
  expiresAt: integer("expires_at", { mode: "timestamp" }).notNull(),
  ipAddress: text("ip_address"),
  userAgent: text("user_agent"),
  createdAt: integer("created_at", { mode: "timestamp" }).notNull(),
  updatedAt: integer("updated_at", { mode: "timestamp" }).notNull(),
});

export const accounts = sqliteTable("accounts", {
  id: text("id").primaryKey(),
  userId: text("user_id")
    .notNull()
    .references(() => users.id),
  accountId: text("account_id").notNull(),
  providerId: text("provider_id").notNull(),
  accessToken: text("access_token"),
  refreshToken: text("refresh_token"),
  idToken: text("id_token"),
  accessTokenExpiresAt: integer("access_token_expires_at", { mode: "timestamp" }),
  refreshTokenExpiresAt: integer("refresh_token_expires_at", { mode: "timestamp" }),
  scope: text("scope"),
  password: text("password"),
  createdAt: integer("created_at", { mode: "timestamp" }).notNull(),
  updatedAt: integer("updated_at", { mode: "timestamp" }).notNull(),
});

export const verifications = sqliteTable("verifications", {
  id: text("id").primaryKey(),
  identifier: text("identifier").notNull(),
  value: text("value").notNull(),
  expiresAt: integer("expires_at", { mode: "timestamp" }).notNull(),
  createdAt: integer("created_at", { mode: "timestamp" }),
  updatedAt: integer("updated_at", { mode: "timestamp" }),
});

export type ArticleRow = typeof articles.$inferSelect;
export type RevisionRow = typeof revisions.$inferSelect;
export type ModerationStateRow = typeof moderationState.$inferSelect;
export type UserRow = typeof users.$inferSelect;
