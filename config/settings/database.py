"""Builds the DATABASES entry from environment variables.

There is intentionally no SQLite fallback: missing configuration raises ImproperlyConfigured.
"""

from django.core.exceptions import ImproperlyConfigured

from .base import env, env_bool


def postgres_from_env(prefix: str, defaults: dict[str, str] | None = None) -> dict:
    defaults = defaults or {}

    def read(key: str) -> str | None:
        return env(f"{prefix}{key}", defaults.get(key))

    missing = [f"{prefix}{key}" for key in ("NAME", "USER", "HOST") if not read(key)]
    if missing:
        raise ImproperlyConfigured(
            "Database is not configured. Missing environment variables: " + ", ".join(missing)
        )

    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": read("NAME"),
        "USER": read("USER"),
        "PASSWORD": read("PASSWORD") or "",
        "HOST": read("HOST"),
        "PORT": read("PORT") or "5432",
        "CONN_MAX_AGE": int(read("CONN_MAX_AGE") or "0"),
        "CONN_HEALTH_CHECKS": True,
        "DISABLE_SERVER_SIDE_CURSORS": env_bool(f"{prefix}DISABLE_SERVER_SIDE_CURSORS", False),
        "OPTIONS": {
            "sslmode": read("SSLMODE") or "prefer",
            "connect_timeout": int(read("CONNECT_TIMEOUT") or "10"),
        },
    }
