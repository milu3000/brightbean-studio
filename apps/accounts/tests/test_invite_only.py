"""Regression coverage for invitation-only signup and independently gated Google login."""

import re
from datetime import timedelta
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import pytest
from allauth.account.models import EmailAddress
from allauth.account.views import SignupView
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.models import SocialAccount, SocialLogin
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import RequestFactory, override_settings
from django.urls import include, path, reverse
from django.utils import timezone

from apps.accounts.adapters import AccountAdapter, SocialAccountAdapter
from apps.accounts.middleware import GoogleLoginPolicyMiddleware
from apps.accounts.models import OAuthConnection, User
from apps.members.models import Invitation, OrgMembership, WorkspaceMembership
from apps.members.services import accept_invitation, resend_invitation, revoke_invitation
from apps.organizations.models import Organization
from apps.workspaces.models import Workspace

urlpatterns = [
    path("alternate-signup/", SignupView.as_view()),
    path("", include("config.urls")),
]

PASSWORD = "A-strong-Test-password-984!"


@pytest.fixture(autouse=True)
def auth_settings(settings):
    settings.AUTH_INVITE_ONLY = True
    settings.AUTH_GOOGLE_LOGIN_ENABLED = False
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def invitation(db):
    org = Organization.objects.create(name="Inviting Organization")
    ws = Workspace.objects.create(organization=org, name="Invited Workspace")
    return Invitation.objects.create(
        organization=org,
        email="invitee@example.com",
        expires_at=timezone.now() + timedelta(days=1),
        workspace_assignments=[{"workspace_id": str(ws.pk), "role": "editor"}],
    )


def open_invite(client, invitation):
    return client.get(reverse("members:accept_invite", kwargs={"token": invitation.token}))


def signup(client, email="invitee@example.com", **kwargs):
    return client.post("/accounts/signup/", {"email": email, "password1": PASSWORD, **kwargs})


@pytest.mark.django_db
class TestInviteOnlySignup:
    @pytest.mark.parametrize("method", ["get", "post"])
    def test_public_signup_closed(self, client, method):
        response = getattr(client, method)(
            "/accounts/signup/", {"email": "outsider@example.com", "password1": PASSWORD}
        )
        assert response.status_code == 403
        assert b"Invitation required" in response.content
        assert not User.objects.exists()

    def test_login_ui_hides_signup_and_google(self, client):
        response = client.get("/accounts/login/")
        assert response.status_code == 200
        assert b"Continue with Google" not in response.content
        assert b">Sign up</a>" not in response.content
        assert b"Accounts are by invitation" in response.content
        assert b"Forgot your password?" in response.content

    def test_valid_invitation_prefills_email_and_creates_only_invited_membership(self, client, invitation):
        assert open_invite(client, invitation).status_code == 200
        response = client.get("/accounts/signup/")
        assert response.status_code == 200
        assert b"invitee@example.com" in response.content
        assert b"readonly" in response.content
        assert b"Continue with Google" not in response.content
        response = signup(client, email="  INVITEE@EXAMPLE.COM  ")
        assert response.status_code == 302
        user = User.objects.get(email="invitee@example.com")
        assert user.check_password(PASSWORD)
        assert user.tos_accepted_at is not None
        assert list(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True)) == [
            invitation.organization_id
        ]
        assert Organization.objects.count() == 1
        assert WorkspaceMembership.objects.get(user=user).workspace_role == "editor"
        invitation.refresh_from_db()
        assert invitation.accepted_at is not None
        assert "pending_invite_token" not in client.session

    def test_invitee_browser_cannot_change_existing_org_timezone(self, client, invitation):
        client.cookies["browser_timezone"] = "Asia/Taipei"
        open_invite(client, invitation)
        assert signup(client).status_code == 302
        invitation.organization.refresh_from_db()
        assert invitation.organization.default_timezone == "UTC"

    @pytest.mark.parametrize("email", ["other@example.com", "invitee+alias@example.com", "in.vitee@example.com"])
    def test_post_cannot_change_locked_email(self, client, invitation, email):
        open_invite(client, invitation)
        response = signup(client, email=email)
        assert response.status_code == 200
        assert b"Use the email address this invitation was sent to" in response.content
        assert not User.objects.exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None

    @pytest.mark.parametrize("state", ["expired", "accepted", "revoked", "rotated", "deleted"])
    def test_invite_invalidated_after_open_cannot_create_user(self, client, invitation, state):
        open_invite(client, invitation)
        if state == "expired":
            Invitation.objects.filter(pk=invitation.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        elif state == "accepted":
            Invitation.objects.filter(pk=invitation.pk).update(accepted_at=timezone.now())
        elif state == "revoked":
            revoke_invitation(invitation)
        elif state == "rotated":
            Invitation.objects.filter(pk=invitation.pk).update(token="replacement-token")
        else:
            invitation.delete()
        assert signup(client).status_code == 403
        assert not User.objects.exists()

    @pytest.mark.parametrize("token", ["missing-token", "", 123, ["invalid"]])
    def test_invalid_session_token(self, client, token):
        session = client.session
        session["pending_invite_token"] = token
        session.save()
        assert signup(client).status_code == 403
        assert not User.objects.exists()

    def test_invite_cannot_be_reused_in_another_session(self, client, invitation):
        from django.test import Client

        second = Client()
        open_invite(client, invitation)
        open_invite(second, invitation)
        assert signup(client).status_code == 302
        assert signup(second).status_code == 403
        assert User.objects.count() == 1

    def test_atomic_rollback_on_membership_failure(self, client, invitation):
        open_invite(client, invitation)
        with patch(
            "apps.members.services.WorkspaceMembership.objects.get_or_create",
            side_effect=ValueError("Unavailable workspace"),
        ):
            assert signup(client).status_code == 403
        assert not User.objects.exists()
        assert not OrgMembership.objects.exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None
        assert Organization.objects.count() == 1
        assert client.session["pending_invite_token"] == invitation.token

    def test_allauth_email_setup_failure_rolls_back_signup_and_invitation(self, client, invitation):
        open_invite(client, invitation)
        with (
            patch("allauth.account.forms.setup_user_email", side_effect=RuntimeError("Email setup failed")),
            pytest.raises(RuntimeError, match="Email setup failed"),
        ):
            signup(client)
        assert not User.objects.exists()
        assert not OrgMembership.objects.exists()
        assert not EmailAddress.objects.exists()
        invitation.refresh_from_db()
        assert invitation.accepted_at is None
        assert client.session["pending_invite_token"] == invitation.token

    def test_adapter_rechecks_after_validation_race(self, client, invitation):
        open_invite(client, invitation)
        original = AccountAdapter.save_user

        def revoke_before_save(adapter, request, user, form, commit=True):
            Invitation.objects.filter(pk=invitation.pk).update(expires_at=timezone.now())
            return original(adapter, request, user, form, commit=commit)

        with patch.object(AccountAdapter, "save_user", revoke_before_save):
            assert signup(client).status_code == 403
        assert not User.objects.exists()

    def test_adapter_direct_save_rejects_mismatch_and_missing_invite(self, invitation):
        request = RequestFactory().post("/accounts/signup/")
        request.session = {"pending_invite_token": invitation.token}
        form = SimpleNamespace(cleaned_data={"email": "wrong@example.com", "password1": PASSWORD})
        with pytest.raises(ImmediateHttpResponse):
            AccountAdapter().save_user(request, User(), form)
        request.session = {}
        form.cleaned_data["email"] = invitation.email
        with pytest.raises(ImmediateHttpResponse):
            AccountAdapter().save_user(request, User(), form)
        assert not User.objects.exists()

    @override_settings(ROOT_URLCONF=__name__)
    def test_default_allauth_signup_view_cannot_bypass_adapter(self, client):
        response = client.post("/alternate-signup/", {"email": "bypass@example.com", "password1": PASSWORD})
        assert response.status_code in (200, 403)
        assert b"Invitation required" in response.content
        assert not User.objects.exists()

    def test_explicit_public_signup_flag_restores_registration(self, client, settings):
        settings.AUTH_INVITE_ONLY = False
        assert b">Sign up</a>" in client.get("/accounts/login/").content
        assert signup(client, "public@example.com").status_code == 302
        assert User.objects.filter(email="public@example.com").exists()

    def test_invalid_invitation_never_falls_back_to_public_signup(self, client, settings):
        settings.AUTH_INVITE_ONLY = False
        session = client.session
        session["pending_invite_token"] = "revoked-token"
        session.save()
        assert signup(client).status_code == 403
        assert not User.objects.exists()

    def test_csrf_required(self, invitation):
        from django.test import Client

        client = Client(enforce_csrf_checks=True)
        open_invite(client, invitation)
        assert signup(client).status_code == 403
        assert not User.objects.exists()


@pytest.mark.django_db
class TestExistingUsers:
    def test_password_login_works(self, client):
        user = User.objects.create_user(email="existing@example.com", password=PASSWORD, tos_accepted_at=timezone.now())
        response = client.post("/accounts/login/", {"login": user.email, "password": PASSWORD})
        assert response.status_code == 302
        assert client.session["_auth_user_id"] == str(user.pk)

    def test_password_reset_works_without_invite(self, client, mailoutbox):
        user = User.objects.create_user(email="existing@example.com", password=PASSWORD)
        response = client.post("/accounts/password/reset/", {"email": user.email})
        assert response.status_code == 302
        assert len(mailoutbox) == 1
        assert mailoutbox[0].to == [user.email]
        assert "/accounts/password/reset/key/" in mailoutbox[0].body

    def test_google_only_user_can_request_password_reset(self, client, mailoutbox):
        user = User.objects.create_user(email="google-only@example.com")
        user.set_unusable_password()
        user.save()
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
        SocialAccount.objects.create(user=user, provider="google", uid="google-only")
        assert client.post("/accounts/password/reset/", {"email": user.email}).status_code == 302
        assert len(mailoutbox) == 1
        assert "/accounts/password/reset/key/" in mailoutbox[0].body

    def test_google_only_user_completes_password_reset_and_logs_in(self, client, mailoutbox):
        user = User.objects.create_user(email="google-reset@example.com", tos_accepted_at=timezone.now())
        user.set_unusable_password()
        user.save()
        EmailAddress.objects.create(user=user, email=user.email, verified=True, primary=True)
        SocialAccount.objects.create(user=user, provider="google", uid="reset-complete")
        assert client.post("/accounts/password/reset/", {"email": user.email}).status_code == 302
        match = re.search(r"https?://[^\s]+/accounts/password/reset/key/[^\s]+", mailoutbox[0].body)
        assert match
        response = client.get(urlsplit(match.group()).path)
        assert response.status_code == 302
        assert "set-password" in response.url
        reset_path = response.url
        assert client.post(reset_path, {"password1": PASSWORD, "password2": PASSWORD}).status_code == 302
        user.refresh_from_db()
        assert user.check_password(PASSWORD)
        assert client.post("/accounts/login/", {"login": user.email, "password": PASSWORD}).status_code == 302
        assert client.session["_auth_user_id"] == str(user.pk)

    def test_existing_user_joins_additional_org(self, client, invitation):
        user = User.objects.create_user(email=invitation.email, password=PASSWORD, tos_accepted_at=timezone.now())
        original_org_ids = set(OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True))
        client.force_login(user)
        response = client.post(reverse("members:accept_invite", kwargs={"token": invitation.token}))
        assert response.status_code == 302
        assert set(
            OrgMembership.objects.filter(user=user).values_list("organization_id", flat=True)
        ) == original_org_ids | {invitation.organization_id}
        invitation.refresh_from_db()
        assert invitation.is_accepted

    def test_existing_user_wrong_email_does_not_join(self, client, invitation):
        user = User.objects.create_user(email="other@example.com", password=PASSWORD, tos_accepted_at=timezone.now())
        client.force_login(user)
        response = client.post(reverse("members:accept_invite", kwargs={"token": invitation.token}))
        assert response.status_code == 200
        assert b"different email address" in response.content
        assert not OrgMembership.objects.filter(user=user, organization=invitation.organization).exists()
        invitation.refresh_from_db()
        assert not invitation.is_accepted

    @pytest.mark.parametrize("mutation", ["revoke", "accept", "rotate"])
    def test_stale_service_invitation_rejected(self, invitation, mutation):
        user = User.objects.create_user(email=invitation.email, password=PASSWORD)
        if mutation == "revoke":
            revoke_invitation(invitation)
        elif mutation == "accept":
            Invitation.objects.filter(pk=invitation.pk).update(accepted_at=timezone.now())
        else:
            Invitation.objects.filter(pk=invitation.pk).update(token="rotated")
        with pytest.raises(ValueError):
            accept_invitation(invitation, user)
        assert not OrgMembership.objects.filter(user=user, organization=invitation.organization).exists()

    def test_foreign_workspace_assignment_fails_without_partial_membership(self, invitation):
        user = User.objects.create_user(email=invitation.email, password=PASSWORD)
        foreign = Workspace.objects.exclude(organization=invitation.organization).first()
        invitation.workspace_assignments.append({"workspace_id": str(foreign.pk), "role": "owner"})
        invitation.save()
        with pytest.raises(ValueError):
            accept_invitation(invitation, user)
        assert not OrgMembership.objects.filter(user=user, organization=invitation.organization).exists()
        invitation.refresh_from_db()
        assert not invitation.is_accepted

    def test_resend_cannot_revive_consumed_invitation_from_stale_instance(self, invitation):
        Invitation.objects.filter(pk=invitation.pk).update(accepted_at=timezone.now())
        with pytest.raises(ValueError, match="already been accepted"):
            resend_invitation(invitation)
        with pytest.raises(ValueError, match="already accepted"):
            revoke_invitation(invitation)


@pytest.mark.django_db
class TestGooglePolicy:
    @pytest.mark.parametrize(
        "url", ["/accounts/google/login/", "/accounts/google/login/callback/", "/accounts/google/login/token/"]
    )
    @pytest.mark.parametrize("method", ["get", "post"])
    def test_all_google_auth_routes_blocked(self, client, url, method):
        assert getattr(client, method)(url).status_code == 403
        assert not User.objects.exists()

    def test_callback_adapter_is_defense_in_depth(self):
        request = RequestFactory().get("/")
        sociallogin = SocialLogin(user=User(), account=SocialAccount(provider="google", uid="new"))
        with pytest.raises(ImmediateHttpResponse):
            SocialAccountAdapter().pre_social_login(request, sociallogin)

    def test_google_enabled_for_migration_does_not_allow_social_signup(self, invitation, settings):
        settings.AUTH_GOOGLE_LOGIN_ENABLED = True
        request = RequestFactory().post("/")
        request.session = {"pending_invite_token": invitation.token}
        sociallogin = SocialLogin(
            user=User(email=invitation.email), account=SocialAccount(provider="google", uid="new")
        )
        adapter = SocialAccountAdapter()
        assert not adapter.is_open_for_signup(request, sociallogin)
        with pytest.raises(ImmediateHttpResponse):
            adapter.save_user(request, sociallogin)
        assert not User.objects.exists()

    def test_staged_google_login_existing_user_still_works(self, settings):
        settings.AUTH_GOOGLE_LOGIN_ENABLED = True
        user = User.objects.create_user(email="google@example.com", password=PASSWORD)
        sociallogin = SocialLogin(user=user, account=SocialAccount(provider="google", uid="existing"))
        SocialAccountAdapter().pre_social_login(RequestFactory().get("/"), sociallogin)
        assert OAuthConnection.objects.filter(user=user, provider="google").exists()

    def test_google_flag_restores_login_ui_but_invite_signup_stays_password_only(self, client, invitation, settings):
        settings.AUTH_GOOGLE_LOGIN_ENABLED = True
        assert b"Continue with Google" in client.get("/accounts/login/").content
        open_invite(client, invitation)
        assert b"Continue with Google" not in client.get("/accounts/signup/").content

    @pytest.mark.parametrize("platform", ["youtube", "google_business"])
    def test_publishing_oauth_routes_are_not_blocked(self, platform):
        from django.urls import resolve

        request = RequestFactory().get(f"/social-accounts/callback/{platform}/")
        request.resolver_match = resolve(request.path)
        middleware = GoogleLoginPolicyMiddleware(lambda r: None)
        assert request.resolver_match.namespace == "social_accounts"
        assert middleware.process_view(request, request.resolver_match.func, (), {"platform": platform}) is None

    def test_google_social_signup_stale_session_cannot_bypass_flag(self, client, invitation):
        sociallogin = SocialLogin(
            user=User(email=invitation.email), account=SocialAccount(provider="google", uid="stale")
        )
        sociallogin.provider = SocialAccountAdapter().get_provider(RequestFactory().get("/"), "google")
        session = client.session
        session["socialaccount_sociallogin"] = sociallogin.serialize()
        session["pending_invite_token"] = invitation.token
        session.save()
        response = client.post(reverse("socialaccount_signup"), {"email": invitation.email})
        assert response.status_code in (200, 403)
        assert b"Invitation required" in response.content
        assert not User.objects.exists()


@pytest.mark.django_db
class TestAuthPreflight:
    def test_no_accounts_passes_without_changes(self):
        output = StringIO()
        call_command("auth_preflight", stdout=output)
        assert "No changes made" in output.getvalue()
        assert not User.objects.exists()

    @pytest.mark.parametrize("password", [None, "", "!unusable"])
    def test_google_only_account_fails_without_changing_password(self, password):
        user = User.objects.create_user(email="google@example.com")
        if password is None:
            user.set_unusable_password()
        else:
            user.password = password
        user.save()
        SocialAccount.objects.create(user=user, provider="google", uid="preflight")
        before = user.password
        output = StringIO()
        with pytest.raises(CommandError, match="1 active Google-linked"):
            call_command("auth_preflight", stdout=output)
        assert user.email not in output.getvalue()
        user.refresh_from_db()
        assert user.password == before

    def test_password_set_account_passes_and_legacy_connection_detected(self):
        user = User.objects.create_user(email="google@example.com", password=PASSWORD)
        OAuthConnection.objects.create(user=user, provider="google", provider_user_id="legacy")
        call_command("auth_preflight", stdout=StringIO())
        user.set_unusable_password()
        user.save()
        with pytest.raises(CommandError):
            call_command("auth_preflight", stdout=StringIO())
