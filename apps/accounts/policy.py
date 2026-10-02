"""Authentication policy shared by adapters, forms and presentation."""

from django.conf import settings
from django.utils import timezone

from apps.members.models import Invitation

PENDING_INVITE_SESSION_KEY = "pending_invite_token"


def normalize_invitation_email(email):
    """Case/whitespace only: never collapse dots or plus-address aliases."""
    return email.strip().lower()


def invitation_required(request):
    # A supplied invitation must remain valid even on an open-registration
    # installation. Never silently turn a failed invitation into public signup.
    return settings.AUTH_INVITE_ONLY or bool(request.session.get(PENDING_INVITE_SESSION_KEY))


def pending_invitation(request, *, for_update=False):
    token = request.session.get(PENDING_INVITE_SESSION_KEY)
    if not isinstance(token, str) or not token:
        return None
    invitations = Invitation.objects.all()
    if for_update:
        invitations = invitations.select_for_update()
    return invitations.filter(token=token, accepted_at__isnull=True, expires_at__gt=timezone.now()).first()
