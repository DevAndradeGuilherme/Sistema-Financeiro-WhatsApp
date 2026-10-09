import logging

from django.db import connection, transaction
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)

# Upper bound for the readiness query itself; the connection attempt is bounded separately by
# DB_CONNECT_TIMEOUT (config/settings/database.py).
READINESS_STATEMENT_TIMEOUT_MS = 2000
READINESS_QUERY = "SELECT 1"


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    """Liveness check. Does not touch the database or expose versions."""
    return JsonResponse({"status": "ok"})


@require_GET
def ready(request: HttpRequest) -> JsonResponse:
    """Readiness: the database answers a trivial read-only query within a short timeout.

    No external provider (OpenAI, Evolution) is called and nothing is written. Failures answer
    503 with a fixed body; the exception detail (host, user, message) is never returned and
    only its type is logged.
    """
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                [str(READINESS_STATEMENT_TIMEOUT_MS)],
            )
            cursor.execute(READINESS_QUERY)
            cursor.fetchone()
    except Exception as exc:  # any failure means "not ready"; details stay out of the response
        logger.warning("readiness.failed check=database error=%s", type(exc).__name__)
        return JsonResponse({"status": "unavailable"}, status=503)
    return JsonResponse({"status": "ready"})
