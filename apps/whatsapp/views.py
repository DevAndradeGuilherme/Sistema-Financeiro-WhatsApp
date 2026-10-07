"""Evolution webhook endpoint. Thin: authenticate → parse → service → HTTP status.

HTTP policy (Evolution v2.3.7 does not retry 400/401/403/404/422; retries other errors):
- 401 missing/invalid secret; 400 malformed JSON/payload; 403 other instance;
- 200 handled, ignored event, ignored message or duplicate;
- 503 database unavailable before the idempotency reservation (safe to retry).
"""

import hmac
import json
import logging

from django.core.exceptions import RequestDataTooBig
from django.db import DatabaseError
from django.http import HttpRequest, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import conf
from .parsers import InvalidPayload, parse_evolution_event
from .pipeline import handle_ready_message
from .services import mark_failed, receive_message

logger = logging.getLogger(__name__)


def _authenticated(request: HttpRequest) -> bool:
    expected = conf.webhook_secret()  # fails clearly (500) if not configured
    provided = request.headers.get(conf.WEBHOOK_SECRET_HEADER, "")
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


@csrf_exempt  # server-to-server; authenticated by the shared secret header instead
@require_POST
def evolution_webhook(request: HttpRequest) -> JsonResponse:
    if not _authenticated(request):
        logger.warning("webhook.unauthorized")
        return JsonResponse({"error": "unauthorized"}, status=401)

    try:
        payload = json.loads(request.body)
    except (RequestDataTooBig, ValueError):
        logger.warning("webhook.invalid_json")
        return JsonResponse({"error": "invalid payload"}, status=400)

    try:
        event = parse_evolution_event(payload)
    except InvalidPayload as exc:
        logger.warning("webhook.invalid_payload detail=%s", exc)
        return JsonResponse({"error": "invalid payload"}, status=400)

    if event.instance != conf.instance_name():
        logger.warning("webhook.unexpected_instance event=%s", event.event)
        return JsonResponse({"error": "unexpected instance"}, status=403)

    if event.message is None:
        logger.info("webhook.event_ignored event=%s instance=%s", event.event, event.instance)
        return JsonResponse({"status": "ignored"})

    try:
        result = receive_message(event.message)
    except DatabaseError:
        logger.exception("webhook.database_error event=%s", event.event)
        return JsonResponse({"error": "temporarily unavailable"}, status=503)

    if result.duplicate:
        return JsonResponse({"status": "duplicate"})

    if result.is_ready:
        try:
            handle_ready_message(result.record)
        except Exception:
            # Reserved already: a retry would be a no-op, so answer 200 and keep it FAILED.
            logger.exception("webhook.pipeline_error event=%s", event.event)
            mark_failed(result.record, "PIPELINE_ERROR")

    return JsonResponse({"status": result.record.status.lower()})
