from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from common.views import health

urlpatterns = [
    path("health/", health, name="health"),
    path("api/v1/webhooks/", include("apps.whatsapp.urls")),
    path(settings.ADMIN_URL, admin.site.urls),
]
