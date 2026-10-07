from urllib.parse import urlparse

from django.core.management.base import BaseCommand, CommandError

from apps.whatsapp import conf
from apps.whatsapp.client import EvolutionClient, EvolutionError


class Command(BaseCommand):
    help = (
        "Configura (idempotente) o webhook da instância: somente MESSAGES_UPSERT, "
        f"header {conf.WEBHOOK_SECRET_HEADER} com EVOLUTION_WEBHOOK_SECRET."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--url",
            required=True,
            help="URL completa do endpoint, ex.: https://<tunnel>/api/v1/webhooks/evolution/",
        )

    def handle(self, *args, url, **options):
        if urlparse(url).scheme not in {"http", "https"}:
            raise CommandError("--url deve começar com http:// ou https://")
        client = EvolutionClient()
        try:
            client.set_webhook(url=url, secret=conf.webhook_secret())
            current = client.find_webhook()
        except EvolutionError as exc:
            raise CommandError(f"Falha ao configurar webhook: {exc}") from None
        self.stdout.write(
            self.style.SUCCESS(
                f"Webhook configurado: url={current.get('url')} "
                f"events={', '.join(current.get('events') or [])} enabled={current.get('enabled')}"
            )
        )
