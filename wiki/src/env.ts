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

  // GitHub token for the /review tool's READ calls (PR diff, comments, file
  // contents, /markdown). Authenticated reads get 5000/hr vs the shared 60/hr
  // unauthenticated limit a single page load would blow. A no-scope classic PAT
  // (or fine-grained read-only) suffices — it only reads public PRs. Posting
  // still uses the logged-in reviewer's own OAuth token.
  GITHUB_API_TOKEN?: string;
}
