from __future__ import annotations

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "inbox.apps.InboxConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "inbox.middleware.RequestIDMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "operational_inbox.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "inbox.context_processors.navigation_context",
            ]
        },
    }
]

WSGI_APPLICATION = "operational_inbox.wsgi.application"
ASGI_APPLICATION = "operational_inbox.asgi.application"

database_url = os.getenv("DJANGO_DATABASE_URL", "sqlite:///db.sqlite3")
if not database_url.startswith("sqlite:///"):
    raise ImproperlyConfigured("DJANGO_DATABASE_URL must use the sqlite:/// scheme.")
sqlite_path = database_url.removeprefix("sqlite:///")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / sqlite_path,
        "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedStaticFilesStorage"
            if DEBUG
            else "whitenoise.storage.CompressedManifestStaticFilesStorage"
        )
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "inbox.User"
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "sesame.backends.ModelBackend",
]
LOGIN_URL = "signup"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "home"

SESAME_MAX_AGE = 600
SESAME_ONE_TIME = False
SESAME_TOKENS = ["sesame.tokens_v2"]

email_backend_mode = (
    os.getenv("DJANGO_EMAIL_BACKEND", "console" if DEBUG else "ses").strip().casefold()
)
if email_backend_mode == "console":
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
elif email_backend_mode == "ses":
    EMAIL_BACKEND = "inbox.email_backend.SESEmailBackend"
else:
    raise ImproperlyConfigured("DJANGO_EMAIL_BACKEND must be either 'console' or 'ses'.")
DEFAULT_FROM_EMAIL = os.getenv(
    "DEFAULT_FROM_EMAIL", "Operational Inbox <notifications@operationalinbox.com>"
)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
INBOUND_SERVICE_DOMAIN = os.getenv("INBOUND_SERVICE_DOMAIN", "inbound.operationalinbox.com")
MAX_PROJECTS_PER_ORGANIZATION = int(os.getenv("MAX_PROJECTS_PER_ORGANIZATION", "10"))
MAX_DOMAINS_PER_ORGANIZATION = int(os.getenv("MAX_DOMAINS_PER_ORGANIZATION", "5"))
DOMAIN_PROVISION_RATE_LIMIT = int(os.getenv("DOMAIN_PROVISION_RATE_LIMIT", "5"))
DOMAIN_PROVISION_RATE_WINDOW_SECONDS = int(
    os.getenv("DOMAIN_PROVISION_RATE_WINDOW_SECONDS", "3600")
)
DOMAIN_CLAIM_TTL_HOURS = int(os.getenv("DOMAIN_CLAIM_TTL_HOURS", "72"))
SIGNUP_RATE_LIMIT = int(os.getenv("SIGNUP_RATE_LIMIT", "5"))
SIGNUP_RATE_WINDOW_SECONDS = int(os.getenv("SIGNUP_RATE_WINDOW_SECONDS", "3600"))
VERIFICATION_RESEND_RATE_LIMIT = int(os.getenv("VERIFICATION_RESEND_RATE_LIMIT", "3"))
VERIFICATION_RESEND_RATE_WINDOW_SECONDS = int(
    os.getenv("VERIFICATION_RESEND_RATE_WINDOW_SECONDS", "3600")
)
TRUSTED_PROXY_IPS = set(env_list("TRUSTED_PROXY_IPS", "127.0.0.1,::1"))

AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_INGRESS_BUCKET = os.getenv("AWS_INGRESS_BUCKET", "")
AWS_INGRESS_QUEUE_URL = os.getenv("AWS_INGRESS_QUEUE_URL", "")
AWS_INBOUND_TOPIC_ARN = os.getenv("AWS_INBOUND_TOPIC_ARN", "")
AWS_DELIVERY_TOPIC_ARN = os.getenv("AWS_DELIVERY_TOPIC_ARN", "")
AWS_SES_CONFIGURATION_SET = os.getenv("AWS_SES_CONFIGURATION_SET", "operational-inbox")
AWS_SES_RECEIPT_RULE_SET = os.getenv("AWS_SES_RECEIPT_RULE_SET", "operational-inbox")
AWS_SES_RECEIPT_RULE = os.getenv("AWS_SES_RECEIPT_RULE", "operational-inbox-allowlist")
AWS_SES_SYSTEM_IDENTITY = os.getenv("AWS_SES_SYSTEM_IDENTITY", "operationalinbox.com")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TRIAGE_MODEL = os.getenv("OPENAI_TRIAGE_MODEL", "gpt-5.6-luna")
OPENAI_DRAFT_MODEL = os.getenv("OPENAI_DRAFT_MODEL", "gpt-5.6-terra")
OPENAI_REPORT_MODEL = os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-terra")

BACKUP_ENCRYPTION_KEY = os.getenv("BACKUP_ENCRYPTION_KEY", "")
BACKUP_DIRECTORY = os.getenv("BACKUP_DIRECTORY", str(BASE_DIR / "backups"))

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 0 if DEBUG else 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "jsonish": {
            "format": (
                "time=%(asctime)s level=%(levelname)s logger=%(name)s "
                "request_id=%(request_id)s message=%(message)s"
            )
        }
    },
    "filters": {"request_id": {"()": "inbox.logging.RequestIDFilter"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "jsonish",
            "filters": ["request_id"],
        }
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}
