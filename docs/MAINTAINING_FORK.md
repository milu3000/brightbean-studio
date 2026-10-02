# Maintaining the invite-only / MCP Events fork

## Branches and baseline

- Upstream: `https://github.com/brightbeanxyz/brightbean-studio`
- Fork: `https://github.com/milu3000/brightbean-studio`
- Customization branch: `milu/invite-only-mcp-events`
- Last verified upstream baseline: `96ccc1e88fefa171c4e5ca981dc9f289bdf60d39` (2026-10-02 UTC)
- Keep `main` as the upstream tracking branch. Keep these customizations on their own branch until a deployment decision is explicitly approved.

## Weekly upstream review

The safe weekly outcome is a reviewed, tested update branch and a draft pull request in **this fork**, not an automatic production deployment.

1. Fetch upstream without changing a deployed worktree:
   ```sh
   git fetch upstream main
   git log --oneline HEAD..upstream/main
   git diff --stat HEAD...upstream/main
   ```
2. Start a temporary update branch from the latest accepted customization branch:
   ```sh
   git switch milu/invite-only-mcp-events
   git switch -c maintenance/upstream-YYYY-MM-DD
   git merge --no-commit --no-ff upstream/main
   ```
3. Review conflicts. Preserve the authentication controls, invite validation, Google **application login** gate, and MCP Events outbox/scoping controls. Do not choose an entire side of a conflict blindly. Google Business Profile and YouTube account connections must remain available independently of Google application login.
4. Inspect upstream migrations, dependencies, OAuth changes and new account-creation entry points. Never overwrite an applied migration. Add a new migration when a model changes.
5. Run the gates below, and summarize upstream commits, conflicts, changed behavior and test results in a draft PR targeted at the fork's customization branch. Avoid posting security-sensitive reports to the upstream public issue tracker.
6. Do not reset, force-push, delete customization commits, merge the PR, or deploy automatically. A failing or unverified gate remains a blocker.

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
