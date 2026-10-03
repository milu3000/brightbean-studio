# Maintaining the BrightBean deployment fork

## Own-project branch policy

This repository is maintained as an independent BrightBean deployment fork.
Upstream remains a source of selected fixes, not an automatic release train.

- Production/integration branch: `milu/stable`. It starts at the verified deployed
  customization commit `a02661642c4a98cdbc805387018ac3b55989283e`, preserving the
  login, invitation, Events, attachment and cancelled-UI history
- Short-lived feature branches: `milu/<topic>-YYYYMMDD`, created from current
  `milu/stable`, with draft PRs targeting `milu/stable`
- Upstream source: `https://github.com/brightbeanxyz/brightbean-studio`; the
  `upstream` remote is read-only for maintenance. No work is pushed upstream
- `main` remains an upstream-reference baseline and is **not deployable** for this
  installation. Its last verified baseline is
  `96ccc1e88fefa171c4e5ca981dc9f289bdf60d39`. Never reset stable or production to it
- The proposed default branch is `milu/stable` so new PRs naturally target the
  own-project line. The verified GitHub default is currently `main`; this
  document does not change it. Confirm any settings change separately and record
  its readback. Default branch selection does not grant deployment rights
- Release records identify exact commit SHA, CI run, web and worker deployment
  IDs, migration result, feature flags, acceptance evidence and rollback target

Keep stable history append-only. Do not force-push, rewrite accepted commits or
squash old cumulative PRs independently. Use a reviewed merge commit to preserve
provenance. Keep rollback source refs; deleting branches or database backups is
not part of routine PR cleanup. Branch protection, credentials and permissions
are separate security decisions, not silently changed by this policy.

## Selective upstream review

Review upstream periodically and when a security/provider compatibility fix is
relevant. Reviewing a change does not authorize merging or deploying it.

1. Fetch upstream without changing deployed worktrees. Inspect commits and diffs
   against the last reviewed upstream SHA; record what was accepted, deferred or
   rejected and why
2. Create `maintenance/upstream-YYYY-MM-DD` from current `milu/stable`. Select only
   justified commits and their necessary dependencies, using `git cherry-pick -x`
   or a documented equivalent patch with the original source reference
3. Do not bulk-merge upstream by default. Preserve invite/password login policy,
   Google application-login restrictions, provider connection routes, workspace
   and account isolation, Events contracts and the explicit grouped-UI revert
4. Review migrations, dependencies, authentication and OAuth scope changes.
   Never edit an applied migration. Any new persistent access, credential or
   security policy still needs its required approval
5. Keep original copyright notices and the AGPL-3.0 license. Maintain source
   provenance and make the corresponding fork source available as the license
   requires; selecting patches does not remove upstream obligations
6. Run the full gates below. Open a draft PR in this fork targeting `milu/stable`
   with upstream references, conflicts, behavior changes and real test evidence.
   Merge/deploy only under a specific release approval; never infer approval from
   the review cadence or a green CI result

## GitHub CI and PR hygiene

CI runs on PRs targeting exactly `main` or `milu/stable`, and on pushes to those
same two branches. Feature-branch pushes alone do not deploy or run this workflow;
opening their draft PR is the normal validation path. The stable push reruns CI
for the exact merged commit before deployment.

The workflow keeps read-only repository permissions, pinned actions, isolated
PostgreSQL 16 tests, Ruff, mypy, gitleaks and Docker build with `push: false`.
There is no deployment job, wildcard branch trigger, `pull_request_target` or new
manual dispatch permission. External service integrations must be checked
separately; the workflow is not proof of their behavior.

For cumulative PRs already included in the deployed baseline, verify each head is
an ancestor of stable and leave a cross-reference before closing it as superseded.
Do not claim it was newly merged if it was only adopted through existing history.
Keep cancelled work closed. Open work lacking ancestry or with unique changes
requires a separate review, not automatic closure. No branch deletion is implied.

## Conversation V2 release boundaries

Phase 1 retains authorized bidirectional DM observations and exposes bounded
read-only context. Phase 2 provides local-only coordination contracts, safety
holds and a read-only state projection. Neither introduces an external dispatcher
or silently changes the legacy per-message event/send contracts.

Both flags default off. Deploy additive migrations first and verify both web and
worker. Activate history only after readback and separate provider acceptance;
keep coordination shadow/local-only until its documented gates are satisfied.
Capture and read enrollments default empty and pin workspace, account and
platform. Never enable global capture to obtain an unapproved test sample, and
do not treat V2-off as a pause of existing received-event responders. Follow the
[scoped acceptance sequence](CONVERSATION_V2_SCOPED_ROLLOUT.md).
Never treat a stored debounce deadline as an automatic reply or use an unknown
outcome to justify a retry. See [phase 1](CONVERSATION_V2_PHASE1.md) and
[phase 2](CONVERSATION_V2_PHASE2.md) for exact limitations.

## Regression gates

Use Python 3.12 and an isolated PostgreSQL database, matching the repository CI. Never point tests at production, copied private production data, or real provider credentials.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export DJANGO_SETTINGS_MODULE=config.settings.test
export SECRET_KEY=test-secret-key-not-for-production
export ENCRYPTION_KEY_SALT=test-salt-not-for-production
# DB_HOST/DB_PORT/DB_USER/DB_PASSWORD must identify the disposable local test server.
python manage.py makemigrations --check --dry-run
ruff check .
ruff format --check .
mypy apps/ config/ providers/ tests/ --ignore-missing-imports
pytest --cov=apps --cov-report=term-missing
```

Targeted regression paths (a focused pass does not replace the full suite):

```sh
pytest apps/accounts/tests/ apps/members/tests/ apps/mcp/tests/ apps/inbox/tests/ apps/api/tests/ apps/oauth_server/tests/
```

Required behavioral checks:

- Anonymous GET/POST account registration cannot bypass invitation policy, including direct allauth/social endpoints
- Invalid, revoked, expired, exhausted and email-mismatched invitations cannot create users or independent organizations
- Concurrent consumption cannot overuse an invitation; existing user joins remain functional
- Existing password login and password reset work; Google-only accounts have a reviewed migration path before rollout
- Google application login is disabled while Google Business/YouTube provider connection routes remain intact
- DM webhook retries deduplicate; outbound echoes never become incoming messages; original timestamps survive ingestion
- Historical or timestamp-invalid messages do not open a fresh automated reply window
- Modern event discovery/subscription and legacy MCP clients both work as documented
- Subscription ownership, workspace/account constraints and revocation are enforced on subscription **and delivery**
- Callback challenge/signature, strict HTTPS allowlist, public-IP connection pinning and redirect rejection are tested with mocks
- Inbox row plus durable event enqueue are transactional; delivery retry uses the same event ID; unsubscribe prevents further deliveries
- Worker restart/retry and delivery expiry are observable; no callback URL/signing secret is logged or stored in plaintext

## Rollout and recovery

Before deployment, review the authentication migration guide and MCP Events guide alongside this document. Take a verified backup, test migrations against a disposable staging database, confirm an administrator can use password login, and run the Google-only account preflight. Existing OAuth-only users must set their own passwords through a verified account flow before disabling that login method. Do not set passwords for users or silently change permissions.

MCP Events are disabled by default and the callback host allowlist is empty. Enable only after the platform-supplied callback and signature contract has been verified in staging, with appropriate authorization for any new persistent access. Do not generate callback/signing credentials by hand or commit them. Live ChatGPT registration/delivery is a separate acceptance test; mock tests cannot establish that it works in a deployed connector.

Deploy web and worker together, then verify migrations, login, invitation flow and a non-sensitive staging event. If a release needs rollback, restore the prior code/configuration using the tested migration rollback plan and inspect pending outbox state before restarting delivery; do not replay arbitrary historical messages. No production deploy is authorized merely by a passing test run or an open PR.

The pre-existing generic notification webhook channel remains separate: MCP Events do not populate a legacy per-notification `webhook_url`. Use the dedicated event subscription lifecycle for event-triggered clients.
