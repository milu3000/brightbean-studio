# Analytics availability and reconnect evidence

Analytics retrieval, remote post visibility, and BrightBean publishing status are separate facts.

- A token rejection or a confirmed missing analytics grant can require reconnecting an account. Evidence is classified from typed exceptions and specific platform codes/scopes, not generic `permission`, `scope`, or `forbidden` text.
- An unavailable post stays local to that post. Meta's unsupported-object error does not prove whether the post was archived, deleted, private, or inaccessible for another reason.
- Temporary transport/rate/server failures and unknown failures remain distinct. Existing snapshots are retained and labeled as saved observations; failed fetches do not write fabricated zero metrics.
- `archived` and `deleted` require an authoritative provider-normalized state or explicit user confirmation. No current Meta adapter claims that a generic unavailable object proves either state.

## Read surfaces

Account and post analytics REST responses, MCP `get_account_analytics` / `get_post_analytics`, and the analytics UI expose the same `analytics_status` information. It includes availability, evidence source, check time, attempt time, error category, and a version for safe updates. Stored evidence uses a closed set of codes and diagnostic identifiers. It never includes exception text, raw error bodies, credentials, or access-token URLs.

The historical `analytics_needs_reconnect` boolean remains present. Existing true flags with no new evidence are **unverified**, not an automatic reconnect instruction. The migration does not clear them. A successful account-insights request can recover an older legacy flag; a successful Threads post-insights request can also do so because this is Threads' analytics endpoint. A post success cannot clear an account-level scope failure. A newly observed failure cannot be erased by another success from the same mixed-result pass. An unresolved account-insights scope cause is retained across later token or post failures; `checked_at` advances to the latest relevant authorization failure to fence older in-flight successes, even when the earlier scope evidence is retained.

Fresh OAuth authorization resets the prior verdict and updates an authorization-generation timestamp. In-flight requests using the older generation cannot overwrite it. A token refresh advances the same fence without pretending the grant was reauthorized.

## Explicit local annotations

The post drawer's confirmation form, `POST /api/v1/analytics/platform-posts/{platform_post_id}/availability`, and MCP `confirm_post_analytics_status` accept:

- `availability`: `archived`, `deleted`, or `unknown` to remove a confirmation
- `expected_version`: the current `analytics_status.version`
- `confirmed`: literal `true`, only after the user explicitly confirms the platform state

These operations require `create_posts` and, for API/MCP, `view_analytics`, as well as the existing workspace/account allowlist. Stale versions are rejected. The browser form also requires CSRF and current workspace permission. API/MCP actions are audited by their existing transport. These are local annotations: they do not archive or delete the remote post, alter publishing state, or erase metrics. Confirmed archived/deleted posts stop background analytics requests and expose no next-sync ETA. Removing a confirmation allows normal polling to resume. Polling preserves user confirmations and newer observations.

## Migration and release boundaries

The composer and social-account migrations only add fields with neutral defaults. Existing publication state, snapshot rows, annotations, and legacy reconnect flags are not inferred or rewritten.

Local regression tests use synthetic data and block all network calls. This execution environment cannot open PostgreSQL sockets, so SQLite results are not PostgreSQL concurrency/migration validation. Before release, run the exact candidate through PostgreSQL CI, review migrations and explicit-annotation permissions, and check reconnect followed by backfill and mixed old/new-post results. Do not clear production flags or annotate a real post as archived as a side effect of deploying this code.

This change fixes a verified classification defect. Without the original sanitized Meta response for the reported incident, it does not establish why a particular historical post became inaccessible.
