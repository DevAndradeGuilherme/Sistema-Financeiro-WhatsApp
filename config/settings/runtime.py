"""Settings for running the application (local server, Docker, production)."""

from .base import *  # noqa: F403
from .database import postgres_from_env

DATABASES = {"default": postgres_from_env("DB_")}
