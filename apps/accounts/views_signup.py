from allauth.account.views import SignupView

from .policy import pending_invitation


class InvitePrefillSignupView(SignupView):
    """Show the invited email; authorization is enforced by forms and adapters."""

    def _invited_email(self):
        invitation = pending_invitation(self.request)
        return invitation.email if invitation else None

    def get_initial(self):
        initial = super().get_initial()
        email = self._invited_email()
        if email:
            initial["email"] = email
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["invited_email_locked"] = bool(self._invited_email())
        return ctx

    def closed(self):
        response = super().closed()
        response.status_code = 403
        return response
