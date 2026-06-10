import { sqliteTable, text, integer, index } from "drizzle-orm/sqlite-core";

// Page-revision model (Wikipedia-style): `articles` holds the current state;
// every save appends a full snapshot to `revisions` (audit log + revert source).

export const articles = sqliteTable("articles", {
  slug: text("slug").primaryKey(),
  wikipediaTitle: text("wikipedia_title").notNull(),
  displayTitle: text("display_title").notNull(),
  wikidataQid: text("wikidata_qid"),
  revid: integer("revid"), // pinned Wikipedia revision the annotations target
  annotations: text("annotations").notNull(), // JSON array (same shape as the sidecar files)
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
    createdAt: integer("created_at").notNull(),
  },
  (t) => [index("idx_revisions_slug").on(t.slug, t.createdAt)],
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
export type UserRow = typeof users.$inferSelect;
