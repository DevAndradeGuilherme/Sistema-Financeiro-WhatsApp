from django.urls import path

from .views import evolution_webhook

urlpatterns = [
    path("evolution/", evolution_webhook, name="evolution-webhook"),
]
