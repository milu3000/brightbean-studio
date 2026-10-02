# MCP 2.0 and automated inbox safety update (2026-10-02)

This section supersedes the historical initial-build report below for the Events
wire contract and programmatic inbox sending behavior. Production activation is
not implied by a source or test result.

## Scope and verification

- Dual-era MCP transport: modern per-request version/capability metadata, mirrored
  HTTP headers (including encoded names), typed results, private cache hints,
  origin checks, and preserved legacy fallback
- Event lifecycle parameter validation and a mocked complete subscribe → inbound
  ingestion → outbox delivery → authoritative fetch → reply → no echo → cancel flow
- Dated official MCP core schemas are vendored with upstream licensing notices;
  locally authored Events tests are not represented as official certification
- Programmatic MCP and REST Meta DM replies reject unknown/future/expired original
  timestamps, never claim HUMAN_AGENT, and reject disconnected accounts
- Inactive/archived principals are rejected even after API-key cache warming
- DM poll timestamps are immutable; same-account outbound IDs and PostgreSQL row
  locks suppress unmarked echoes racing a successful send response
- Unsupported automated sends remain failures, not locally fabricated success

All provider and callback traffic in tests is mocked. No live credentials,
callback grants, subscriptions, customer messages or external replies are created.
A genuine user-approved inbound event → assistant wake → authorized response
cycle, plugin capability refresh, and callback access approval remain live gates.
See [MCP_EVENTS.md](MCP_EVENTS.md) for rollout, rollback and uncertainty handling.

## Historical initial build

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

The full implementation is published to the `milu/invite-only-mcp-events` branch of `milu3000/brightbean-studio`. Draft PR: https://github.com/milu3000/brightbean-studio/pull/1 . The upstream-tracking `main` branch is unchanged; nothing is merged or deployed.

The authenticated publishing identity was `milu3000`. The original browser commits are preserved without force push. Implementation commit `4d4dcc0edc4c9113ca08c39b72e70e58e272c777` was read back from GitHub and its tree equals the tested local tree `add5fdc4295bdf5dffaddfaf187b797965a8b392`. Later changes to this report are documentation-only. The requested `mics8128` collaborator has accepted write access; administrative access was not granted.

The source package includes a baseline-applicable patch, a Git bundle and the full tracked source, without credentials or private data. The patch is verified by applying it to the baseline and comparing the resulting Git tree. GitHub workflow state is reported on the PR separately; passing local tests is not a claim of deployed or live-provider verification.
