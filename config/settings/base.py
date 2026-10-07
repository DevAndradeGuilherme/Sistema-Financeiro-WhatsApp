"""Settings shared by every environment.

Does not define DATABASES: see ``runtime.py`` (app/server) and ``test.py`` (test suite).
All environment-dependent values come from environment variables (see ``.env.example``).
"""

import os
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Existing environment variables always take precedence over the .env file.
load_dotenv(BASE_DIR / ".env", override=False)


def env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def env_required(name: str) -> str:
    value = env(name)
    if value is None:
        raise ImproperlyConfigured(f"Environment variable {name} is required.")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = env(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ImproperlyConfigured(f"Environment variable {name} must be a boolean, got {value!r}.")


def env_list(name: str) -> list[str]:
    value = env(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]


# --- Environment -------------------------------------------------------------

APP_ENV = env("DJANGO_ENV", "development")
if APP_ENV not in {"development", "production"}:
    raise ImproperlyConfigured("DJANGO_ENV must be 'development' or 'production'.")
IS_PRODUCTION = APP_ENV == "production"

SECRET_KEY = env_required("DJANGO_SECRET_KEY")
DEBUG = env_bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

if IS_PRODUCTION:
    if DEBUG:
        raise ImproperlyConfigured("DJANGO_DEBUG must be False when DJANGO_ENV=production.")
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("DJANGO_ALLOWED_HOSTS is required when DJANGO_ENV=production.")
    if len(SECRET_KEY) < 50 or SECRET_KEY.startswith("change-me"):
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is too weak for production.")

# --- Applications ------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "common",
    "apps.accounts",
    "apps.families",
    "apps.categories",
    "apps.transactions",
    "apps.ai",
    "apps.whatsapp",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ADMIN_URL = env("DJANGO_ADMIN_URL", "admin/")

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Internationalization / time --------------------------------------------

LANGUAGE_CODE = "pt-br"
TIME_ZONE = env("TIME_ZONE", "America/Sao_Paulo")
USE_I18N = True
USE_TZ = True

# --- Static files ------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# --- Database helpers --------------------------------------------------------

# Enables PostgreSQL row level security (no policies) on every table after migrate.
# Defense against the Supabase Data API exposing the public schema. See docs/DATABASE.md.
DB_ENABLE_RLS = env_bool("DB_ENABLE_RLS", default=True)

# --- Domain ------------------------------------------------------------------

TRANSACTION_MAX_AMOUNT = Decimal(env("TRANSACTION_MAX_AMOUNT", "1000000.00"))

# --- WhatsApp / Evolution API --------------------------------------------------
# Read here, validated where used (apps.whatsapp.conf): missing values fail clearly only
# when an operation needs them. See docs/WHATSAPP.md.

EVOLUTION_API_BASE_URL = env("EVOLUTION_API_BASE_URL")
EVOLUTION_API_KEY = env("EVOLUTION_API_KEY")
EVOLUTION_INSTANCE_NAME = env("EVOLUTION_INSTANCE_NAME")
EVOLUTION_WEBHOOK_SECRET = env("EVOLUTION_WEBHOOK_SECRET")
EVOLUTION_TIMEOUT_SECONDS = env("EVOLUTION_TIMEOUT_SECONDS")

# --- OpenAI ------------------------------------------------------------------
# Validated where used (apps.ai.conf). The model always comes from configuration.

OPENAI_API_KEY = env("OPENAI_API_KEY")
OPENAI_MODEL = env("OPENAI_MODEL")
OPENAI_TIMEOUT_SECONDS = env("OPENAI_TIMEOUT_SECONDS")
# Optional; only sent when set (not every model accepts it). See docs/AI_CONTRACT.md.
OPENAI_REASONING_EFFORT = env("OPENAI_REASONING_EFFORT")

# Audio (V1.3): one transcription call per audio message, then the textual pipeline.
OPENAI_TRANSCRIPTION_MODEL = env("OPENAI_TRANSCRIPTION_MODEL")
OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS = env("OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS")
AUDIO_MAX_BYTES = int(env("AUDIO_MAX_BYTES", str(10 * 1024 * 1024)))  # API limit is 25 MB
AUDIO_MAX_SECONDS = int(env("AUDIO_MAX_SECONDS", "300"))  # financial messages, not meetings

# Follow-up queries ("e mês passado?") reuse the last query for this long.
CONVERSATION_CONTEXT_TTL_MINUTES = int(env("CONVERSATION_CONTEXT_TTL_MINUTES", "20"))

# --- Security ----------------------------------------------------------------

SESSION_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

if IS_PRODUCTION:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = env_bool("DJANGO_SECURE_SSL_REDIRECT", default=True)
    SECURE_REDIRECT_EXEMPT = [r"^health/$"]
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("DJANGO_SECURE_HSTS_SECONDS", "3600"))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
        "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False
    )

# --- Logging -----------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "default"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
