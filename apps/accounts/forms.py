from allauth.account.adapter import get_adapter
from allauth.account.forms import SignupForm
from django.core.exceptions import ValidationError
from django.db import transaction

from .policy import invitation_required, normalize_invitation_email, pending_invitation


class InvitationSignupForm(SignupForm):
    @transaction.atomic
    def save(self, request):
        # Include allauth's EmailAddress creation and custom signup hook in the
        # same transaction as the adapter's user + membership + invite writes.
        return super().save(request)

    def clean_email(self):
        email = super().clean_email()
        request = get_adapter().request
        if invitation_required(request):
            invitation = pending_invitation(request)
            if invitation is None:
                raise ValidationError("A valid organization invitation is required.")
            if normalize_invitation_email(email) != normalize_invitation_email(invitation.email):
                raise ValidationError("Use the email address this invitation was sent to.")
        return normalize_invitation_email(email)
