from django.conf import settings
from django.contrib import admin
from django.urls import path

from common.views import health

urlpatterns = [
    path("health/", health, name="health"),
    path(settings.ADMIN_URL, admin.site.urls),
]
