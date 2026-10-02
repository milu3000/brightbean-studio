# Validation report — 2026-10-02 UTC

## Scope

This change set implements invitation-only email/password signup, disables Google application login by default while preserving social-publishing OAuth, and adds opt-in MCP Events for inbound direct messages. No production site, account, credential, permission, callback subscription, or deployment was changed.

Upstream baseline: `96ccc1e88fefa171c4e5ca981dc9f289bdf60d39`.

Fork: `milu3000/brightbean-studio`, branch `milu/invite-only-mcp-events`.

## Final automated checks

- Python 3.12.14 / Django 5.1.15 / PostgreSQL 17.10
- Full suite: **2683 passed, 1 skipped**, in 101.81 seconds
- Coverage: **79%** across applications (whole project, not only this patch)
- PostgreSQL row-lock regressions passed, including simultaneous invitation use, invitation revocation during signup, concurrent outbox delivery, unsubscribe during an in-flight delivery, and concurrent event fanout
- Ruff 0.15.9 lint: passed
- Ruff format: 456 files already formatted
- mypy 1.19.1, full repository command: **572 source files passed**
- Django migration drift check: no changes detected
- Django system checks and PostgreSQL forward → reverse → restore of the new MCP migration: passed on the disposable test database
- `git diff --check`: passed
- Gitleaks 8.21.2 on 896 exported source files: **no leaks found**

The one skipped check targets Redis-backed caching; this isolated test deployment uses the repository's local-memory cache. PostgreSQL concurrency checks were executed, not skipped. The suite emits upstream/test static-directory warnings; no failed test remains.

## Review and defects fixed during validation

Independent review covered invitation atomicity and email matching, allauth entry points, Google login separation, event workspace/account isolation, key/token revocation, encrypted subscription/outbox storage, HTTPS/DNS/SSRF controls, challenge signatures, unsubscribe and retry deduplication. A reproduced OAuth cancellation issue after changing the active workspace was fixed and independently retested. Quiet expired subscriptions and expired rotation keys are cleaned by the worker sweep.

The existing inbox migration-test fixture originally restored only the inbox app after rewinding a migration, leaving dependent MCP tables unapplied. It now restores the original full migration graph. The complete PostgreSQL suite passes with that correction.

An intermediate concurrent mypy invocation crashed within django-stubs. A subsequent complete final invocation passed; no unrelated typing code was changed to mask the crash.

## Limits before deployment

- All provider/callback delivery tests used mock payloads and mocked network transport. No live ChatGPT Events subscription or callback delivery was attempted
- OAuth event renewal and `get_inbox_message` still follow the selected dashboard workspace. Cross-workspace cancellation is fixed; use a deliberately authorized fixed-workspace API key for stable background automation, or keep the OAuth workspace stable. See `MCP_EVENTS.md`
- Google-only existing users must complete their own password setup and pass `auth_preflight` before Google application login is turned off in a live installation
- Container image build, a deployed browser smoke test, production database backup/restore, and live platform acceptance remain deployment gates
- MCP Events are disabled by default; callback allowlist is empty and fails closed
- Generic legacy notification webhook configuration was not changed; the dedicated MCP Events lifecycle is separate

## Publication state

The fork and remote customization branch were created and verified. At report time, the remote branch still points to the upstream baseline: the completed implementation is in the accompanying local commit, patch and source package. The cloud GitHub browser upload route timed out repeatedly and its next upload waited for approval for approximately 21 minutes. No partial application-code change was committed remotely, no PR was opened, and no deployment occurred. Do not treat the fork branch URL alone as the completed implementation until its tree/commit is verified after upload.

Apply the delivered patch to the exact baseline or fetch the accompanying Git bundle, then run the documented gates and open a draft PR in this fork. Do not merge or deploy without the separate approved rollout.
