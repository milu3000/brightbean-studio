"""Login errors remain visible without revealing whether an account exists."""

import re

import pytest
from allauth.account.models import EmailAddress
from django.core.cache import cache
from django.utils import translation
from django.utils.html import escape

from apps.accounts.models import User


@pytest.fixture(autouse=True)
def login_settings(settings):
    settings.AUTH_INVITE_ONLY = True
    settings.AUTH_GOOGLE_LOGIN_ENABLED = False
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def login_user(db):
    user = User.objects.create_user(email="login-test@example.com", password="Correct-test-password-984!")
    EmailAddress.objects.create(user=user, email=user.email, primary=True, verified=True)
    return user


def assert_field_error_visible(response, field_name):
    form = response.context["form"]
    assert form[field_name].errors
    body = response.content.decode()
    assert str(escape(form[field_name].errors[0])) in body
    assert f'aria-describedby="id_{field_name}_error"' in body
    assert f'id="id_{field_name}_error"' in body
    assert 'aria-invalid="true"' in body


@pytest.mark.django_db
class TestLoginErrors:
    @pytest.mark.parametrize("language", ["en", "zh-hant"])
    def test_wrong_password_shows_generic_error(self, client, login_user, language):
        with translation.override(language):
            response = client.post(
                "/accounts/login/", {"login": login_user.email, "password": "Wrong-test-password-748!"}
            )
            errors = [str(error) for error in response.context["form"].non_field_errors()]
        assert response.status_code == 200
        assert errors
        body = response.content.decode()
        assert str(escape(errors[0])) in body
        assert 'id="login-form-errors" role="alert"' in body
        assert "Forgot your password?" in body
        assert "Accounts are by invitation" not in body
        assert "Social media scheduling" not in body
        assert "trustpilot" not in body.lower()
        assert "_auth_user_id" not in client.session

    def test_unknown_email_and_wrong_password_use_same_error(self, client, login_user):
        errors = []
        for email in (login_user.email, "unknown-login-test@example.invalid"):
            response = client.post("/accounts/login/", {"login": email, "password": "Wrong-test-password-748!"})
            assert response.status_code == 200
            errors.append(list(response.context["form"].non_field_errors()))
            assert str(escape(errors[-1][0])) in response.content.decode()
        assert errors[0] == errors[1]
        assert "_auth_user_id" not in client.session

    def test_invalid_email_error_is_visible(self, client):
        response = client.post("/accounts/login/", {"login": "not-an-email", "password": "Wrong-test-password-748!"})
        assert response.status_code == 200
        assert_field_error_visible(response, "login")

    def test_empty_form_errors_are_visible(self, client):
        response = client.post("/accounts/login/", {})
        assert response.status_code == 200
        assert_field_error_visible(response, "login")
        assert_field_error_visible(response, "password")
        assert b"Forgot your password?" in response.content

    def test_submitted_password_is_never_redisplayed(self, client, login_user):
        password = "Wrong-test-password-748!"
        response = client.post("/accounts/login/", {"login": login_user.email, "password": password})
        assert response.status_code == 200
        assert password.encode() not in response.content
        field = re.search(r'<input[^>]*name="password"[^>]*>', response.content.decode())
        assert field is not None
        assert "value=" not in field.group()
        assert login_user.email.encode() in response.content

    def test_get_has_no_empty_error_alert(self, client):
        response = client.get("/accounts/login/")
        assert response.status_code == 200
        assert b"login-form-errors" not in response.content
        assert b'aria-invalid="true"' not in response.content

    def test_password_reset_page_still_available(self, client):
        assert client.get("/accounts/password/reset/").status_code == 200

    def test_correct_password_still_authenticates(self, client, login_user):
        response = client.post(
            "/accounts/login/", {"login": login_user.email, "password": "Correct-test-password-984!"}
        )
        assert response.status_code == 302
        assert str(client.session["_auth_user_id"]) == str(login_user.pk)
