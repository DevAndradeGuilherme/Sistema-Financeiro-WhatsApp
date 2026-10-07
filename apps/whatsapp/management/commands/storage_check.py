import uuid

from django.core.management.base import BaseCommand, CommandError

from common.storage import StorageClient, StorageError


class Command(BaseCommand):
    help = (
        "Verifica o bucket de comprovantes: existe e é PRIVADO. Com --write-test, envia um objeto "
        "sintético, confere e remove. Nunca altera políticas nem torna o bucket público."
    )

    def add_arguments(self, parser):
        parser.add_argument("--write-test", action="store_true")

    def handle(self, *args, write_test, **options):
        client = StorageClient()
        try:
            info = client.bucket_info()
        except StorageError as exc:
            raise CommandError(f"Bucket inacessível: {exc}") from None
        public = info.get("public")
        self.stdout.write(f"Bucket: {client.config.bucket} | public={public}")
        if public is not False:
            raise CommandError("O bucket NÃO está privado. Corrija no Supabase (não feito aqui).")
        if not write_test:
            self.stdout.write(self.style.SUCCESS("Bucket existe e é privado."))
            return

        path = f"receipts/smoke/{uuid.uuid4().hex}.txt"
        try:
            client.upload(path, b"storage smoke test (no personal data)", "text/plain")
            info = client.object_info(path)
            self.stdout.write(f"Upload ok; objeto encontrado (size={info.get('size', '?')}).")
            try:
                client.upload(path, b"overwrite attempt", "text/plain")
                raise CommandError("Sobrescrita permitida: x-upsert=false não foi respeitado.")
            except StorageError as exc:
                self.stdout.write(f"Sobrescrita recusada como esperado ({exc.code}).")
        except StorageError as exc:
            raise CommandError(f"Falha no teste de escrita: {exc}") from None
        finally:
            try:
                client.delete(path)
                self.stdout.write("Objeto de teste removido.")
            except StorageError as exc:
                self.stderr.write(f"Não consegui remover o objeto de teste: {exc}")
        self.stdout.write(self.style.SUCCESS("Storage OK (privado, upload/consulta/remoção)."))
