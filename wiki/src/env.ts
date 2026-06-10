export interface Env {
  DB: D1Database;
  RENDER_CACHE: KVNamespace;
  WP_HTML: KVNamespace;
  ASSETS: Fetcher;
  EDIT_LIMITER: {
    limit: (opts: { key: string }) => Promise<{ success: boolean }>;
  };

  AUTH_MODE: string; // "dev" (cookie stub) | "oauth" (better-auth)

  // better-auth (used when AUTH_MODE === "oauth")
  BETTER_AUTH_SECRET?: string;
  BETTER_AUTH_URL?: string;
  GITHUB_CLIENT_ID?: string;
  GITHUB_CLIENT_SECRET?: string;
  GOOGLE_CLIENT_ID?: string;
  GOOGLE_CLIENT_SECRET?: string;
}
