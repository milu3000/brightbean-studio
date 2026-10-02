from django.conf import settings

from .policy import pending_invitation


def auth_policy(request):
    return {
        "auth_google_login_enabled": settings.AUTH_GOOGLE_LOGIN_ENABLED,
        "auth_public_signup_enabled": not settings.AUTH_INVITE_ONLY,
        "auth_invited_signup_enabled": bool(request.path_info.startswith("/accounts/") and pending_invitation(request)),
    }
