from django.core.management.base import BaseCommand, CommandError

from apps.whatsapp.client import EvolutionClient, EvolutionError

DEFAULT_TEXT = "Mensagem de teste técnico do sistema financeiro."


class Command(BaseCommand):
    help = "Envia uma mensagem de texto de teste pela Evolution API."

    def add_arguments(self, parser):
        parser.add_argument(
            "--phone", required=True, help="Telefone de destino (qualquer formato)."
        )
        parser.add_argument("--text", default=DEFAULT_TEXT)

    def handle(self, *args, phone, text, **options):
        try:
            sent = EvolutionClient().send_text(phone, text)
        except (ValueError, EvolutionError) as exc:
            raise CommandError(f"Falha no envio: {exc}") from None
        self.stdout.write(
            self.style.SUCCESS(f"Enviada: id={sent.provider_message_id} status={sent.status}")
        )
