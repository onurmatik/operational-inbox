from __future__ import annotations

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def env_list(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "unsafe-development-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1],testserver")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
MCP_ALLOWED_ORIGINS = env_list(
    "MCP_ALLOWED_ORIGINS",
    "https://chatgpt.com,https://chat.openai.com,https://claude.ai,https://claude.com",
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "oauth2_provider",
    "oauth_server.apps.OAuthServerConfig",
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

database_url = os.getenv("DJANGO_DATABASE_URL", "").strip() or "sqlite:///db.sqlite3"
if database_url.startswith("sqlite:///"):
    sqlite_path = database_url.removeprefix("sqlite:///")
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / sqlite_path,
            "OPTIONS": {"timeout": 20, "transaction_mode": "IMMEDIATE"},
        }
    }
elif database_url.startswith(("postgresql://", "postgres://")):
    DATABASES = {
        "default": dj_database_url.parse(
            database_url,
            conn_max_age=60,
            conn_health_checks=True,
        )
    }
else:
    raise ImproperlyConfigured(
        "DJANGO_DATABASE_URL must use the sqlite:/// or postgresql:// scheme."
    )

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
MCP_RESOURCE_URL = os.getenv("MCP_RESOURCE_URL", f"{PUBLIC_BASE_URL}/mcp")
MCP_DOCUMENTATION_URL = os.getenv("MCP_DOCUMENTATION_URL", f"{PUBLIC_BASE_URL}/mcp-docs/")
MCP_REQUIRED_SCOPES = ["read", "write", "manage_domains", "send"]
OPERATIONAL_INBOX_OAUTH_SERVER_ENABLED = env_bool(
    "OPERATIONAL_INBOX_OAUTH_SERVER_ENABLED",
    True,
)
OAUTH_ISSUER = os.getenv("OAUTH_ISSUER", PUBLIC_BASE_URL).rstrip("/")
OAUTH_ACCESS_TOKEN_EXPIRE_SECONDS = env_int("OAUTH_ACCESS_TOKEN_EXPIRE_SECONDS", 900)
OAUTH_AUTHORIZATION_CODE_EXPIRE_SECONDS = env_int(
    "OAUTH_AUTHORIZATION_CODE_EXPIRE_SECONDS",
    60,
)
OAUTH_REFRESH_TOKEN_EXPIRE_SECONDS = env_int(
    "OAUTH_REFRESH_TOKEN_EXPIRE_SECONDS",
    2_592_000,
)
if OPERATIONAL_INBOX_OAUTH_SERVER_ENABLED:
    invalid_lifetimes = [
        name
        for name, value in {
            "OAUTH_ACCESS_TOKEN_EXPIRE_SECONDS": OAUTH_ACCESS_TOKEN_EXPIRE_SECONDS,
            "OAUTH_AUTHORIZATION_CODE_EXPIRE_SECONDS": (OAUTH_AUTHORIZATION_CODE_EXPIRE_SECONDS),
            "OAUTH_REFRESH_TOKEN_EXPIRE_SECONDS": OAUTH_REFRESH_TOKEN_EXPIRE_SECONDS,
        }.items()
        if value < 1
    ]
    if invalid_lifetimes:
        raise ImproperlyConfigured(
            "Operational Inbox OAuth token lifetimes must be positive: "
            + ", ".join(invalid_lifetimes)
        )
OAUTH_DCR_ENABLED = env_bool("OAUTH_DCR_ENABLED", True)
OAUTH_DCR_PER_IP_HOURLY_LIMIT = env_int("OAUTH_DCR_PER_IP_HOURLY_LIMIT", 20)
OAUTH_DCR_GLOBAL_HOURLY_LIMIT = env_int("OAUTH_DCR_GLOBAL_HOURLY_LIMIT", 200)
OAUTH_DCR_MAX_REDIRECT_URIS = env_int("OAUTH_DCR_MAX_REDIRECT_URIS", 10)
OAUTH_DCR_CLIENT_RETENTION_DAYS = env_int("OAUTH_DCR_CLIENT_RETENTION_DAYS", 30)
OPENAI_APPS_CHALLENGE_TOKEN = os.getenv("OPENAI_APPS_CHALLENGE_TOKEN", "").strip()

OAUTH2_PROVIDER_APPLICATION_MODEL = "oauth_server.OAuthApplication"
OAUTH2_PROVIDER = {
    "APPLICATION_MODEL": OAUTH2_PROVIDER_APPLICATION_MODEL,
    "OAUTH2_VALIDATOR_CLASS": "oauth_server.validators.OperationalInboxOAuth2Validator",
    "RESOURCE_SERVER_TOKEN_RESOURCE_VALIDATOR": (
        "oauth_server.validators.exact_resource_validator"
    ),
    "SCOPES": {
        "read": "Read authorized inboxes, conversations, drafts, and delivery status.",
        "write": "Create drafts and apply reversible inbox organization changes.",
        "manage_domains": (
            "Inspect public DNS, start domain onboarding, read setup instructions, "
            "and request DNS verification."
        ),
        "send": (
            "Send exact agent-authored reply revisions and explicitly resend failed or "
            "unknown attempts."
        ),
    },
    "DEFAULT_SCOPES": [],
    "AUTHORIZATION_CODE_EXPIRE_SECONDS": OAUTH_AUTHORIZATION_CODE_EXPIRE_SECONDS,
    "ACCESS_TOKEN_EXPIRE_SECONDS": OAUTH_ACCESS_TOKEN_EXPIRE_SECONDS,
    "REFRESH_TOKEN_EXPIRE_SECONDS": OAUTH_REFRESH_TOKEN_EXPIRE_SECONDS,
    "REFRESH_TOKEN_GRACE_PERIOD_SECONDS": 0,
    "REFRESH_TOKEN_REUSE_PROTECTION": True,
    "ROTATE_REFRESH_TOKEN": True,
    "REQUEST_APPROVAL_PROMPT": "force",
    "ALLOWED_REDIRECT_URI_SCHEMES": ["https"],
    "ALLOW_LOCALHOST_LOOPBACK": True,
    "ALLOW_URI_WILDCARDS": False,
    "PKCE_REQUIRED": True,
    "OIDC_ENABLED": False,
    "OIDC_ISS_ENDPOINT": OAUTH_ISSUER,
    "DCR_ENABLED": OAUTH_DCR_ENABLED,
    "DCR_REGISTRATION_PERMISSION_CLASSES": ("oauth_server.permissions.RateLimitedDCRPermission",),
    "CIMD_ENABLED": False,
    "CLEAR_EXPIRED_TOKENS_BATCH_SIZE": 500,
    "OAUTH2_RESPONSE_TYPES_SUPPORTED": ["code"],
    "OAUTH2_TOKEN_ENDPOINT_AUTH_METHODS_SUPPORTED": ["none"],
    "OAUTH2_GRANT_TYPES_SUPPORTED": ["authorization_code", "refresh_token"],
    "OAUTH2_PROTECTED_RESOURCE_IDENTIFIER": MCP_RESOURCE_URL,
    "OAUTH2_PROTECTED_RESOURCE_AUTHORIZATION_SERVERS": [OAUTH_ISSUER],
    "OAUTH2_PROTECTED_RESOURCE_BEARER_METHODS_SUPPORTED": ["header"],
    "OAUTH2_PROTECTED_RESOURCE_NAME": "Operational Inbox MCP",
    "COMPLIANT_BCP_RFC9700_IMPLICIT_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PASSWORD_GRANT": True,
    "COMPLIANT_BCP_RFC9700_PKCE_METHOD": True,
    "COMPLIANT_BCP_RFC9700_ACCESS_TOKEN_TRANSPORT": True,
    "COMPLIANT_BCP_RFC9700_AUTHZ_RESPONSE_ISS": True,
    "COMPLIANT_BCP_RFC9700_TOKEN_STORAGE": True,
    "COMPLIANT_BCP_RFC9700_REFRESH_TOKEN": True,
    "COMPLIANT_BCP_RFC9700_REDIRECT_URI_SCHEME": True,
}

INBOUND_SERVICE_DOMAIN = os.getenv("INBOUND_SERVICE_DOMAIN", "inbound.operationalinbox.com")
MAX_DOMAINS_PER_USER = int(os.getenv("MAX_DOMAINS_PER_USER", "20"))
DOMAIN_PROVISION_RATE_LIMIT = int(os.getenv("DOMAIN_PROVISION_RATE_LIMIT", "5"))
DOMAIN_PROVISION_RATE_WINDOW_SECONDS = int(
    os.getenv("DOMAIN_PROVISION_RATE_WINDOW_SECONDS", "3600")
)
DOMAIN_CLAIM_TTL_HOURS = int(os.getenv("DOMAIN_CLAIM_TTL_HOURS", "72"))
DOMAIN_DOWNGRADE_GRACE_DAYS = env_int("DOMAIN_DOWNGRADE_GRACE_DAYS", 30)
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
# The existing OUTBOUND_* settings remain the Pro limits for deployment
# compatibility. Free has lower capacity but the same outbound feature set.
FREE_OUTBOUND_RATE_LIMIT_PER_MINUTE = env_int("FREE_OUTBOUND_RATE_LIMIT_PER_MINUTE", 2)
FREE_OUTBOUND_DAILY_ACCOUNT_LIMIT = env_int("FREE_OUTBOUND_DAILY_ACCOUNT_LIMIT", 10)
FREE_OUTBOUND_DAILY_DOMAIN_LIMIT = env_int("FREE_OUTBOUND_DAILY_DOMAIN_LIMIT", 10)
FREE_OUTBOUND_MONTHLY_ACCOUNT_LIMIT = env_int("FREE_OUTBOUND_MONTHLY_ACCOUNT_LIMIT", 30)
OUTBOUND_RATE_LIMIT_PER_MINUTE = env_int("OUTBOUND_RATE_LIMIT_PER_MINUTE", 30)
OUTBOUND_DAILY_ACCOUNT_LIMIT = env_int("OUTBOUND_DAILY_ACCOUNT_LIMIT", 500)
OUTBOUND_DAILY_DOMAIN_LIMIT = env_int("OUTBOUND_DAILY_DOMAIN_LIMIT", 200)
OUTBOUND_MONTHLY_ACCOUNT_LIMIT = env_int("OUTBOUND_MONTHLY_ACCOUNT_LIMIT", 5000)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_TRIAGE_MODEL = os.getenv("OPENAI_TRIAGE_MODEL", "gpt-5.6-luna")
OPENAI_REPORT_MODEL = os.getenv("OPENAI_REPORT_MODEL", "gpt-5.6-terra")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRO_UNIT_AMOUNT = int(os.getenv("STRIPE_PRO_UNIT_AMOUNT", "0"))
STRIPE_PRO_COMPARE_AT_UNIT_AMOUNT = int(os.getenv("STRIPE_PRO_COMPARE_AT_UNIT_AMOUNT", "999"))
STRIPE_PRO_CURRENCY = os.getenv("STRIPE_PRO_CURRENCY", "usd").strip().casefold()

BACKUP_ENCRYPTION_KEY = os.getenv("BACKUP_ENCRYPTION_KEY", "")
BACKUP_DIRECTORY = os.getenv("BACKUP_DIRECTORY", str(BASE_DIR / "backups"))

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", not DEBUG)
SESSION_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
SESSION_COOKIE_AGE = 400 * 24 * 60 * 60
SESSION_EXPIRE_AT_BROWSER_CLOSE = False
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
