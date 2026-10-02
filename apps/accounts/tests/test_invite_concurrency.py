"""Exercise the invitation authorization lock on the production database engine."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event
from types import SimpleNamespace

import pytest
from allauth.core.exceptions import ImmediateHttpResponse
from django.db import close_old_connections, connection, connections, transaction
from django.test import RequestFactory
from django.utils import timezone

from apps.accounts.adapters import AccountAdapter
from apps.accounts.models import User
from apps.members.models import Invitation, OrgMembership
from apps.organizations.models import Organization


@pytest.fixture
def invitation(transactional_db, settings):
    if connection.vendor != "postgresql":
        pytest.skip("Row-lock race regression requires PostgreSQL")
    settings.AUTH_INVITE_ONLY = True
    return Invitation.objects.create(
        organization=Organization.objects.create(name="Concurrency org"),
        email="race@example.com",
        expires_at=timezone.now() + timedelta(days=1),
    )


def attempt_signup(token, *, barrier=None, started=None):
    close_old_connections()
    try:
        request = RequestFactory().post("/accounts/signup/")
        request.session = {"pending_invite_token": token}
        form = SimpleNamespace(cleaned_data={"email": "race@example.com", "password1": "Concurrency-password-912!"})
        if barrier:
            barrier.wait(timeout=10)
        if started:
            started.set()
        try:
            AccountAdapter().save_user(request, User(), form)
            return "created"
        except ImmediateHttpResponse as exc:
            assert exc.response.status_code == 403
            return "rejected"
    finally:
        connections.close_all()


@pytest.mark.django_db(transaction=True)
def test_simultaneous_signup_consumes_invitation_once(invitation):
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = [pool.submit(attempt_signup, invitation.token, barrier=barrier) for _ in range(2)]
        outcomes = [attempt.result(timeout=15) for attempt in attempts]
    assert sorted(outcomes) == ["created", "rejected"]
    assert User.objects.filter(email=invitation.email).count() == 1
    assert OrgMembership.objects.filter(organization=invitation.organization).count() == 1
    assert Organization.objects.count() == 1
    invitation.refresh_from_db()
    assert invitation.is_accepted


@pytest.mark.django_db(transaction=True)
def test_signup_waiting_on_revocation_cannot_use_old_token(invitation):
    started = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with transaction.atomic():
            locked = Invitation.objects.select_for_update().get(pk=invitation.pk)
            locked.expires_at = timezone.now()
            locked.save(update_fields=["expires_at"])
            attempt = pool.submit(attempt_signup, invitation.token, started=started)
            assert started.wait(timeout=10)
        assert attempt.result(timeout=15) == "rejected"
    assert not User.objects.exists()
    assert not OrgMembership.objects.exists()
    invitation.refresh_from_db()
    assert invitation.accepted_at is None
