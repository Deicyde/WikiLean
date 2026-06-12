export interface Env {
  DB: D1Database;
  RENDER_CACHE: KVNamespace;
  WP_HTML: KVNamespace;
  ASSETS: Fetcher;
  EDIT_LIMITER: {
    limit: (opts: { key: string }) => Promise<{ success: boolean }>;
  };
  // Anonymous flag-report limiter, keyed flag:<CF-Connecting-IP> (the flag
  // endpoint has no auth, so the user-keyed EDIT_LIMITER can't cover it).
  FLAG_LIMITER: {
    limit: (opts: { key: string }) => Promise<{ success: boolean }>;
  };

  AUTH_MODE: string; // "dev" (cookie stub) | "oauth" (better-auth)

  // Shared bearer secret for the moderation pipeline (site/moderate.py).
  // Optional: when unset, the bearer branch in getUser is disabled entirely.
  // Single-token scheme by decision — graduate to an api_tokens table when a
  // second token-holder exists.
  PIPELINE_TOKEN?: string;

  // better-auth (used when AUTH_MODE === "oauth")
  BETTER_AUTH_SECRET?: string;
  BETTER_AUTH_URL?: string;
  GITHUB_CLIENT_ID?: string;
  GITHUB_CLIENT_SECRET?: string;
  GOOGLE_CLIENT_ID?: string;
  GOOGLE_CLIENT_SECRET?: string;
}
