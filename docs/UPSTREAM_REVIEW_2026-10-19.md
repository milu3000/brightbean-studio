# Upstream review — 2026-10-19

Reviewed upstream `brightbeanxyz/brightbean-studio` main at
`48801fdcd1009e6ed4863fa9942222a478a297e2` against the fork's upstream baseline
`96ccc1e88fefa171c4e5ca981dc9f289bdf60d39`.
The integration base is `milu/stable` at
`c06434c16a22232d6afeee218f44f970e6ae1942`, not fork `main`.

## Existing work and selected change

Reuse draft PR #24 and its existing `maintenance/upstream-2026-10-12` branch.
Its head `9954c948b7a70eee00fbc04b70d4e87fa4d5da52` already selected upstream
`a93930a4b828eb8dd4e52c9949f00a4ff76f683a` (#208): remove the redundant inline
calendar filter chevron while retaining the CSS chevron and selection controls.
Append a merge of current stable into that maintenance branch; do not rewrite
history, duplicate the fix in another PR, or merge the maintenance branch into
stable. Open synthetic experiment PRs #9 and #10 are independent and excluded.

Relative to current stable, the executable change remains one deleted SVG line
in `templates/components/ui_select.html`. This review record is the other change.
There are no model, migration, dependency, authentication, credential, API quota,
MCP Events, worker, provider, deployment or retention changes in this candidate.

## Deferred upstream groups

| Upstream sources | Decision and reason |
| --- | --- |
| #141 / `51c578e`, `c50dc53`; #206 / `9246a48`; #221 / `7353775`, `20bd891`, `9f75643` | Review separately as an authentication migration. The fork already enforces valid, unexpired, email-matched, atomic organization invitation signup and independently gates Google application login. Upstream SIGNUP_MODE/custom redirect is not a drop-in equivalent. In particular, `9f75643` passes `require_email_match=False` during invite signup, whereas this fork requires the invited email. Do not replace or remove the fork policy. Password recovery and Google Business provider OAuth must remain available. |
| #207 / `e1e25c8`, `d6fd8e7` | Deferred: replaces API read auditing with usage counters and adds quota/cap behavior and a migration. This is an audit/limit policy decision, not a routine UI fix. |
| #209 / `1b0e569`, `f9ce4a0`, `2272973` | Deferred as a credential-storage and analytics migration group; includes new persistent Instagram credential state. Review current account compatibility, migration numbering and credential isolation before selection. |
| #210 / `a854306`; #212 / `cce18b6`; #213 / `2f5e2e9` | Deferred: Instagram DM ingestion/OAuth and default permission requests overlap the fork's custom DM route, original timestamp, echo, deduplication and send-window controls. Scope expansion is not selected. |
| #214 / `4414d52`, `7ca7abb`, `8d318e1`, `0d15d53` | Deferred: changes shared worker admission/concurrency, analytics backfill scheduling and credential refresh locking. Requires a separate compatibility review of durable event recovery, delivery locks, scheduling and shutdown behavior. No worker command/config change is made here. |
| #215 / `b9a1edf`; #220 / `cb0abbd`, `fd52f4c` | Deferred as the YouTube compliance/reporting group. It includes persistent models, migrations, retention/revocation behavior and dependent UI changes. Do not partially apply branding changes whose surrounding templates are absent from this fork. |
| #223 / `91cbb9a26ac636cd1bbebf68e874ab4ec30decc0` | Useful publishing fix, deferred pending worker/provider integration review. Despite the Mastodon title it changes 13 files, adds shared upload-cache/idempotency/shutdown interfaces and rewrites Instagram, Threads and LinkedIn waits; it depends on the unselected worker group. Do not cherry-pick a partial provider implementation or silently alter shared retry behavior. |

No upstream group above is declared permanently rejected. Each needs an explicit
follow-up comparison and the appropriate behavior/migration acceptance before
selection. This pass does not repair unrelated automation/schedule discovery.

## Migration and policy incompatibilities to resolve before future selection

- Fork inbox already has `0005_inboxconversation_conversationmessage_and_more`,
  `0006_reply_coordination` and the
  later migration `0020_legacy_action_reference`. Upstream independently adds
  `0005_meta_dm_timestamps` (#210) and `0006_inboxmessage_api_refreshed_at` (#215).
- Fork social_accounts already has
  `0022_socialaccount_analytics_auth_updated_at_and_more`; upstream #209 adds
  `0022_instagram_analytics_credentials` on its own graph.
- Fork composer already has `0023_platformpost_analytics_availability_and_more`;
  upstream #215 adds
  `0023_platformpost_youtube_video_checked_at` on its own graph.

Do not apply these upstream migration files directly or edit applied fork
migrations. A later selected feature needs a new migration against the fork's
actual leaf, preservation of existing rows and old-writer compatibility tests.
Upstream SIGNUP_MODE defaults to open and its social-invite email relaxation is
not an approved replacement for this fork's policy. The worker files required by
#223 do not exist in this fork; they cannot be imported as incidental dependencies.
In particular, the current action/receipt state and migration 0020 mean that
reverting the writer to old stable `9430b139` is not automatically a safe rollback.

## Validation and release boundary

Use the Python 3.12/PostgreSQL full regression gates in `MAINTAINING_FORK.md`,
including invitation/password recovery/Google provider separation, MCP
workspace isolation and callback verification, DM echo/replay/deduplication and
original timestamp/window guards. Record exact-head results in the draft PR.
A focused calendar render/negative control and Node tests supplement the full
suite; they do not replace it.

No migration is added or changed, so this candidate introduces no schema
rollback step. Keep the existing stable release and its migration/rollback
boundaries; do not reverse the earlier inbox migrations for this UI change.
The latest deployment SHA supplied by the release record is `c06434c`; this
environment has no Railway connector and does not independently attest current
web/worker runtime versions. Merge and deployment are not authorized by this
weekly draft update.
