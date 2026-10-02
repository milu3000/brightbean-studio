# Invitation-only password authentication

This fork defaults to `AUTH_INVITE_ONLY=true` and `AUTH_GOOGLE_LOGIN_ENABLED=false`.
These settings govern **application sign-in only**, not Google Business / YouTube
publishing connections under `/social-accounts/`.

- Public signup links are hidden; GET/POST `/accounts/signup/` require a valid
  organization invitation stored in the session by opening its acceptance link.
- Account adapters enforce the same restriction for allauth entry points. Social
  signup remains closed in invite-only mode, even when existing Google login is
  temporarily enabled for migration.
- The submitted address must exactly match the invited address after stripping
  whitespace and lowercasing. Dots and plus aliases are not collapsed. The email
  input's read-only attribute is convenience, not authorization.
- The database transaction locks and rechecks the invite before creating the
  account. User creation, organization/workspace membership and one-time token
  consumption are atomic. Expired, accepted, revoked and superseded tokens fail
  closed. No fallback organization is created on failure.
- Existing users can still sign in with passwords, reset their passwords, and
  accept invitations into an additional organization using the matching address.
- Trusted management/admin user creation remains available for bootstrapping;
  it is not a public registration endpoint. A fresh operator can run Django's
  interactive `createsuperuser`, then sign in and invite team members.

## Existing deployments: prevent a Google-only account lockout

Do **not** change a live installation to the new defaults without this preflight.
This code change alone does not deploy or modify any existing environment.

1. Keep `AUTH_GOOGLE_LOGIN_ENABLED=true` during migration. Setting
   `AUTH_INVITE_ONLY=true` immediately closes new public/social signups while
   allowing existing Google users to sign in.
2. Run `python manage.py auth_preflight` using the deployment's normal settings
   and database. It only reads users and exits nonzero if active Google-linked
   users have no usable password. `--show-emails` optionally lists the affected
   addresses in the operator's console; keep that output private.
3. Verify transactional email delivery and each user's current email address.
   Have the affected users set their own passwords using `/accounts/password/set/`
   while signed in, or `/accounts/password/reset/`. Do not generate, assign or
   transmit passwords on their behalf. The reset flow is available independently
   of signup policy; users must control their current mailbox.
4. Confirm affected users can actually sign in with email/password. A stored hash
   alone cannot prove that the user knows the password or receives reset emails.
5. Rerun preflight. Only after it passes and users have verified access, set
   `AUTH_GOOGLE_LOGIN_ENABLED=false` and restart the app. Check password login,
   password reset, invitation signup and both Google publishing connectors.

Rollback for login issues: restore `AUTH_GOOGLE_LOGIN_ENABLED=true` and restart.
Keep invite-only mode enabled. This restores Google sign-in for existing users
without reopening social signup. No Google accounts, publishing credentials,
passwords or invitations are deleted by the feature or preflight command.

Setting `AUTH_INVITE_ONLY=false` deliberately restores public password signup.
If Google login is also enabled, public social signup is restored too. An invalid
pending invitation still fails closed rather than silently creating a personal
organization; use a fresh session for intentional public signup.
