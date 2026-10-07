import hmac

from django.core.management.base import BaseCommand, CommandError

from apps.whatsapp import conf
from apps.whatsapp.client import EvolutionClient, EvolutionError, EvolutionHTTPError


class Command(BaseCommand):
    help = "Verifica Evolution API: versão, estado da instância e webhook (sem exibir segredos)."

    def handle(self, *args, **options):
        client = EvolutionClient()
        try:
            info = client.get_server_info()
        except EvolutionError as exc:
            raise CommandError(f"Evolution indisponível: {exc}") from None
        self.stdout.write(f"Evolution: versão {info.get('version', '?')}")
        self.stdout.write(f"Instância: {client.config.instance}")

        try:
            self.stdout.write(f"Conexão: {client.get_connection_state() or '?'}")
            webhook = client.find_webhook()
        except EvolutionHTTPError as exc:
            raise CommandError(f"Falha ao consultar a instância: {exc}") from None

        if not webhook:
            self.stdout.write(self.style.WARNING("Webhook: não configurado"))
            return
        headers = webhook.get("headers") if isinstance(webhook.get("headers"), dict) else {}
        configured = str(headers.get(conf.WEBHOOK_SECRET_HEADER, ""))
        secret_ok = hmac.compare_digest(configured.encode(), conf.webhook_secret().encode())
        self.stdout.write(f"Webhook: enabled={webhook.get('enabled')} url={webhook.get('url')}")
        self.stdout.write(f"Eventos: {', '.join(webhook.get('events') or []) or '-'}")
        self.stdout.write(
            f"Header {conf.WEBHOOK_SECRET_HEADER}: "
            + ("confere com EVOLUTION_WEBHOOK_SECRET" if secret_ok else "AUSENTE OU DIFERENTE")
        )
