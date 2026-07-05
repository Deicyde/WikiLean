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
  // Looser limiter for API/bearer brain-edge writes (scripts + agents post in
  // bursts). Keyed brainapi:<user.id>. Browser brain-edge writes use EDIT_LIMITER.
  BRAIN_API_LIMITER: {
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

  // Dedicated GitHub *App* for the /review tool's POSTING flow only — kept SEPARATE
  // from the wiki login (GITHUB_CLIENT_ID above stays identity-only). Register a
  // GitHub App (NOT an OAuth app) with: permission "Pull requests: Read & write" +
  // "Issues: Read & write" (the top-level summary is an issue comment); user
  // authorization callback {BETTER_AUTH_URL}/review/auth/callback; "Request user
  // authorization (OAuth) during installation" optional. CLIENT_ID/SECRET are the
  // App's. A reviewer who clicks "Connect GitHub" grants ONLY PR-comment write (via
  // a user-to-server token), never public_repo. To post into an org the App must be
  // INSTALLED there; APP_SLUG builds the install URL shown when a post is blocked.
  REVIEW_GITHUB_CLIENT_ID?: string;
  REVIEW_GITHUB_CLIENT_SECRET?: string;
  REVIEW_GITHUB_APP_SLUG?: string;
  // Personal-token posting (option A): a classic PAT (public_repo) that posts the
  // PR comments so they land WITHOUT the org installing the App — classic PATs are
  // exempt from OAuth-App restrictions. SECURITY: used ONLY for its owner — the
  // submitter must be connected AS the PAT account — so this public endpoint can't
  // post through it on anyone else's behalf. Comments appear from the PAT owner.
  REVIEW_POSTING_PAT?: string;
}
