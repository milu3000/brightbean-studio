"""Standalone local settings. Does not read .env or import deployment settings."""

import os
from pathlib import Path

from .guard import preview_root

ROOT = preview_root()
SECRET_KEY = os.environ["BRIGHTBEAN_SYNTHETIC_SECRET"]
ENCRYPTION_KEY_SALT = b"local-synthetic-preview-only"
SYNTHETIC_TIMELINE_PREVIEW = True
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost", "testserver"]
ROOT_URLCONF = "tests.conversation_preview.urls"
AUTH_USER_MODEL = "accounts.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/login/"
TIME_ZONE = "UTC"
USE_TZ = True
LANGUAGE_CODE = "zh-hant"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SITE_ID = 1
STATIC_URL = "/static/"
MEDIA_ROOT = ROOT / "media"
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
SESSION_SAVE_EVERY_REQUEST = False
SESSION_COOKIE_NAME = "synthetic_preview_session"
CSRF_COOKIE_NAME = "synthetic_preview_csrf"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Strict"
APP_URL = "http://127.0.0.1"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(ROOT / "synthetic.sqlite3")}}
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "oauth2_provider",
    "apps.background_task_config.BackgroundTaskConfig",
    "apps.common",
    "apps.accounts",
    "apps.organizations",
    "apps.workspaces",
    "apps.members",
    "apps.settings_manager",
    "apps.credentials",
    "apps.social_accounts",
    "apps.media_library",
    "apps.composer",
    "apps.calendar",
    "apps.publisher",
    "apps.notifications",
    "apps.inbox",
    "apps.approvals",
    "apps.client_portal",
    "apps.onboarding",
    "apps.intelligence",
    "apps.api_keys",
    "apps.api",
    "apps.mcp",
    "apps.oauth_server",
    "apps.analytics",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "tests.conversation_preview.views.PreviewHeadersMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [Path(__file__).parent / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
            ]
        },
    }
]
INBOX_CONVERSATION_V2_ENABLED = True
INBOX_REPLY_COORDINATION_ENABLED = True
# Static invented UUIDs only; model imports happen later, after django.setup().
INBOX_CONVERSATION_V2_CAPTURE_ACCOUNTS: list[dict[str, str]] = []
INBOX_CONVERSATION_V2_READ_ACCOUNTS: list[dict[str, str]] = []
