from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    """Liveness check. Does not touch the database or expose versions."""
    return JsonResponse({"status": "ok"})
