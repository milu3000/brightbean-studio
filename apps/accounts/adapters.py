from allauth.account.adapter import DefaultAccountAdapter
from allauth.core.exceptions import ImmediateHttpResponse
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter
from django.conf import settings
from django.db import transaction
from django.http import HttpResponseForbidden

from apps.accounts.models import OAuthConnection
from apps.common.mail import transactional
from apps.members.services import accept_invitation

from .policy import (
    PENDING_INVITE_SESSION_KEY,
    invitation_required,
    normalize_invitation_email,
    pending_invitation,
)


class AccountAdapter(DefaultAccountAdapter):
    """Marks allauth's own mail as transactional.

    Password resets, email confirmations and login codes are mail a person is
    sitting in front of waiting for. Without this they would carry the default
    ``notification`` class and be subject to the per-recipient cap in
    ``apps.common.mail`` — so a user who had already received their allowance of
    publish-failure notices that hour could not reset their own password. The
    global daily cap still applies; nothing bypasses that.

    ``render_mail`` is the single seam every allauth email passes through, so
    overriding it here covers all of them without touching a template.
    """

    def is_open_for_signup(self, request):
        return not invitation_required(request) or pending_invitation(request) is not None

    @transaction.atomic
    def save_user(self, request, user, form, commit=True):
        if not invitation_required(request):
            return super().save_user(request, user, form, commit=commit)

        # The form check is UX; the locked recheck is the authorization boundary.
        # User creation, memberships and one-time token consumption commit together.
        invitation = pending_invitation(request, for_update=True)
        email = normalize_invitation_email(form.cleaned_data.get("email", ""))
        if invitation is None or email != normalize_invitation_email(invitation.email) or not commit:
            raise ImmediateHttpResponse(HttpResponseForbidden("A valid matching organization invitation is required."))
        user._skip_default_provisioning = True
        user = super().save_user(request, user, form, commit=True)
        try:
            accept_invitation(invitation, user)
        except ValueError as exc:
            raise ImmediateHttpResponse(HttpResponseForbidden(str(exc))) from exc
        return user

    def render_mail(self, template_prefix, email, context, headers=None):
        return super().render_mail(
            template_prefix,
            email,
            context,
            headers={**(headers or {}), **transactional()},
        )


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    """Custom adapter that syncs Google social logins to OAuthConnection."""

    def is_open_for_signup(self, request, sociallogin):
        # Invitation signup always sets a password, including during a staged
        # migration where existing users still have Google login enabled.
        return (
            settings.AUTH_GOOGLE_LOGIN_ENABLED
            and not settings.AUTH_INVITE_ONLY
            and not request.session.get(PENDING_INVITE_SESSION_KEY)
        )

    def populate_user(self, request, sociallogin, data):
        """Set user.name from Google profile (custom User model has 'name', not first/last)."""
        user = super().populate_user(request, sociallogin, data)
        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip()
        if full_name and not user.name:
            user.name = full_name
        return user

    def save_user(self, request, sociallogin, form=None):
        """Create OAuthConnection after saving a new social signup."""
        if not self.is_open_for_signup(request, sociallogin):
            raise ImmediateHttpResponse(HttpResponseForbidden("Social signup is disabled. Use your invitation link."))
        user = super().save_user(request, sociallogin, form)
        self._sync_oauth_connection(user, sociallogin)
        return user

    def pre_social_login(self, request, sociallogin):
        """Sync OAuthConnection for returning users and auto-connected accounts."""
        if sociallogin.account.provider == "google" and not settings.AUTH_GOOGLE_LOGIN_ENABLED:
            raise ImmediateHttpResponse(
                HttpResponseForbidden("Google sign-in is disabled. Sign in with your password.")
            )
        super().pre_social_login(request, sociallogin)
        if sociallogin.is_existing:
            self._sync_oauth_connection(sociallogin.user, sociallogin)

    def _sync_oauth_connection(self, user, sociallogin):
        account = sociallogin.account
        if account.provider != "google":
            return
        provider_email = ""
        for ea in sociallogin.email_addresses:
            provider_email = ea.email
            break
        OAuthConnection.objects.update_or_create(
            provider=OAuthConnection.Provider.GOOGLE,
            provider_user_id=account.uid,
            defaults={"user": user, "provider_email": provider_email},
        )
