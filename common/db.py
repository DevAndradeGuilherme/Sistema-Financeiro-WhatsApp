"""Database infrastructure helpers (no business rules)."""

import logging

from django.conf import settings
from django.db import connections

logger = logging.getLogger(__name__)


def enable_row_level_security(using: str = "default", **kwargs) -> None:
    """Enable RLS (without policies) on every table of the current schema.

    The table owner (the role Django connects with) bypasses RLS, so the application is not
    affected. Other roles, such as Supabase's anon/authenticated roles used by its Data API,
    get no access. Plain PostgreSQL feature; see docs/DATABASE.md.
    """
    connection = connections[using]
    if not settings.DB_ENABLE_RLS or connection.vendor != "postgresql":
        return

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = current_schema()
              AND c.relkind IN ('r', 'p')
              AND NOT c.relrowsecurity
            """
        )
        tables = [row[0] for row in cursor.fetchall()]
        for table in tables:
            quoted = connection.ops.quote_name(table)
            cursor.execute(f"ALTER TABLE {quoted} ENABLE ROW LEVEL SECURITY")

    if tables:
        logger.info("Row level security enabled on %d table(s).", len(tables))
