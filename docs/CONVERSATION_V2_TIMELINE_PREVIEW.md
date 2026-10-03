# Conversation V2: synthetic read-only timeline preview

This is a local design and authorization preview, based on deployed/stable
`8835d030046c15288a2295f92141b6e65ddbf2c6`. It does not depend on the separate
dispatcher harness PR. No production URL, sidebar, model, migration, MCP
schema, event or existing inbox behavior changes. The cancelled grouped inbox
is not restored. There is no provider dispatcher or production enrollment.

## Run locally

Use the repository's installed Python 3.12 dependencies. From the repository
root, in a shell with `DATABASE_URL`, `DJANGO_SETTINGS_MODULE` and
`BRIGHTBEAN_SYNTHETIC_ROOT` unset:

```sh
python -m tests.conversation_preview.run --port 8765
```

Open `http://127.0.0.1:8765/`. The launcher prints the synthetic email
`preview-only@example.invalid` and a fresh `DEMO-LOCAL-ONLY-…` password.
These credentials exist only in that newly created temporary SQLite database;
never enter a real login. Django's password/session login is used, with a real
User, OrgMembership, WorkspaceMembership and CustomRole. The custom role grants
only `use_inbox`. Production allauth login UX is not part of this preview.

The launcher binds only to loopback, never reads `.env` or imports deployment
settings, refuses inherited database/settings selection, and creates a private
`/tmp/brightbean-synthetic-preview-…` directory. A marker, exact SQLite path and
absence of an existing database are checked before migration. It never clears
or migrates an unknown database. All records are invented, accounts are
disconnected with empty provider credentials, and enrollment pins only the
fixture UUIDs in that process. No worker starts. Outbound socket connections
are disabled in the preview process. Stop with Ctrl+C; the printed temporary
directory contains only disposable synthetic data.

## Review the scenarios

Use the visible navigation: short text burst with an unavailable attachment,
native outgoing with unknown author, coordinator-only pause, unknown/retracted
conversation identity, retracted content, no observed history, failed/partial
coverage with unknown operation outcome, and bounded/truncated history.

The screen shows one existing inbox work item beside its exact linked timeline
and a small observational coordination panel. It does not group or resolve
inbox work. Desktop uses two columns; CSS stacks them below 760px. Interface
text in this standalone demo is primarily Traditional Chinese; deployed language settings
are unchanged. Fonts are system fonts and there are no scripts or external
assets. Attachment URLs are omitted, even if stored metadata claims availability.

## Export a standalone review artifact

```sh
python -m tests.conversation_preview.run --export /tmp/brightbean-preview-pages --export-only
```

Choose a **new** output directory. The exporter logs into the synthetic DB with
Django's test client and saves the same authenticated HTTP template rendering.
It exports all scenarios and every bounded page, including `index.html`.
Scenario and paging links become relative HTML links. Necessary CSS is inline;
cookies, credentials, signed cursors and localhost routes are not exported.
Zip the whole directory for review. The export is static and contains no login,
send or pause action. Do not treat a static export as browser authentication QA.

## Scope and read guarantees

`apps/inbox/conversation_read.py` is a new adapter unused by production routes.
It uses real human workspace permissions rather than a fabricated MCP context
or an API key. Existing human authorization is workspace-wide: no invented
per-human account grant policy is added. It requires an active user, a current
membership, a nonarchived workspace and effective `use_inbox`, intersected with
the selected account's capture/read enrollment.

Each protected query embeds lazy membership and account subqueries, pinning
membership ID, role, custom-role permissions, workspace/role organization
consistency, account workspace/platform and
native account identity. Conversation queries additionally pin identity fields;
all linked targets, state and operations are checked independently. A final
check discards a result if the scope/bridge changed during assembly. Unknown
conversation identity displays only the exact observation linked to that work;
it never guesses by names, peers or nearby timestamps. No raw draft bodies,
claim tokens, idempotency keys, provider payloads or remote assets are exposed.

GETs do not call the old detail view, RBAC workspace-switching route or send
services. The preview URL has no `workspace_id` kwarg; sliding session saves
are disabled in isolated settings. Authenticated responses are private/no-store.
Login itself necessarily writes the synthetic session and login timestamp.

Signed cursors expire after one hour and bind the real user/grant, exact account
identity, work target and conversation identity. Each page resolves current
permissions and enrollment again. Paging is by `(first_seen_at, id)` descending,
not assumed platform send order. A timestamp boundary excludes new observations
from subsequent pages; existing edits are not frozen. These are observational
reads, not a transactionally atomic snapshot or a send authorization.

Each page has at most 8 observations; each body is at most 1,200 characters and
each observation shows at most 3 attachment descriptions of 160 characters.
Truncation and more-history boundaries are explicit. Django escapes hostile
text, attachment types/statuses/sources are bounded whitelists, and no stored URL
is turned into a browser resource or action.

“Paused” refers only to this local coordinator. It cannot pause legacy send
tools, external event consumers or native apps. Native outgoing does not prove
human authorship, a particular question answered, delivery, or resolved work.
An unknown send outcome is not a reason to retry. Account-level sync is not
proof of complete or current conversation coverage.

## Verification

Focused regression tests:

```sh
pytest apps/inbox/tests/test_member_timeline.py tests/test_conversation_preview_launcher.py
ruff check apps/inbox/conversation_read.py apps/inbox/tests/test_member_timeline.py tests/conversation_preview tests/test_conversation_preview_launcher.py
ruff format --check apps/inbox/conversation_read.py apps/inbox/tests/test_member_timeline.py tests/conversation_preview tests/test_conversation_preview_launcher.py
mypy apps/inbox/conversation_read.py apps/inbox/tests/test_member_timeline.py tests/conversation_preview tests/test_conversation_preview_launcher.py --ignore-missing-imports
```

Tests cover real password login, built-in/custom-role permissions, revocation
between reads, inactive users, archived workspaces, cross-scope corrupt FKs,
membership replacement, native account/conversation identity changes, cursor
tampering/expiry/timestamp ties, no-side-effect reads, hidden retracted content,
unverified outgoing status, empty/unknown history, hostile text, truncation,
semantic navigation and self-contained assets. SQLite checks do not prove PostgreSQL locking
behavior. This adapter adds no locks or dispatch concurrency claims.

Browser layout and mobile viewport checks remain a separate acceptance gate.
In the development environment the cloud browser rejected loopback navigation
and local file navigation, so authenticated browser E2E and screenshot/visual
mobile QA were **not verified**. Django Client checks and static artifact checks
must not be described as a browser pass. Live Instagram/Facebook observations,
real enrollment, any send/pause controls and provider behavior are out of scope.
