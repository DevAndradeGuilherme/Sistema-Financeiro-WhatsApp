"""Settings for the test suite.

Safety rules (see docs/TESTING.md):
- Tests use only TEST_DB_* variables, never DB_*; defaults point to the local test container
  (``docker compose --profile test up -d test-db``).
- Refuses to run when DJANGO_ENV=production.
- Refuses any Supabase-hosted database or the same server/database configured in DB_*.
"""

import os

from django.core.exceptions import ImproperlyConfigured

# Test-only key, used solely when no key is provided. Never used by runtime settings.
os.environ.setdefault("DJANGO_SECRET_KEY", "insecure-test-only-secret-key")

from .base import *  # noqa: E402, F403
from .base import IS_PRODUCTION, env  # noqa: E402
from .database import postgres_from_env  # noqa: E402

if IS_PRODUCTION:
    raise ImproperlyConfigured("Refusing to run tests with DJANGO_ENV=production.")

DEBUG = False

_test_db = postgres_from_env(
    "TEST_DB_",
    defaults={
        "NAME": "controle_gastos",
        "USER": "postgres",
        "PASSWORD": "postgres",
        "HOST": "localhost",
        "PORT": "5433",
        "SSLMODE": "disable",
    },
)

_host = _test_db["HOST"].lower()
if "supabase" in _host:
    raise ImproperlyConfigured("Refusing to run tests against a Supabase-hosted database.")
if (_host, str(_test_db["PORT"])) == ((env("DB_HOST") or "").lower(), env("DB_PORT") or "5432"):
    raise ImproperlyConfigured("TEST_DB_* must point to a different server than DB_*.")

DATABASES = {"default": _test_db}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
TRANSACTION_MAX_AMOUNT = Decimal("1000000.00")  # noqa: F405

# Fixed fake Evolution configuration: tests never depend on .env nor reach the real API.
EVOLUTION_API_BASE_URL = "http://evolution.test"
EVOLUTION_API_KEY = "test-evolution-api-key"
EVOLUTION_INSTANCE_NAME = "test-instance"
EVOLUTION_WEBHOOK_SECRET = "test-webhook-secret"
EVOLUTION_TIMEOUT_SECONDS = "5"

# Fixed fake OpenAI configuration: the client is always mocked; network is blocked.
OPENAI_API_KEY = "test-openai-api-key"
OPENAI_MODEL = "test-model"
OPENAI_TIMEOUT_SECONDS = "5"
OPENAI_REASONING_EFFORT = None
OPENAI_TRANSCRIPTION_MODEL = "test-transcribe"
OPENAI_TRANSCRIPTION_TIMEOUT_SECONDS = "5"
AUDIO_MAX_BYTES = 1024 * 1024
AUDIO_MAX_SECONDS = 300
