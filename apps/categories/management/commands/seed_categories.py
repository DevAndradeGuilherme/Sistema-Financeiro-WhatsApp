from django.core.management.base import BaseCommand

from apps.categories.seed import seed_categories


class Command(BaseCommand):
    help = "Cria o catálogo inicial de categorias (idempotente; não sobrescreve alterações)."

    def handle(self, *args, **options):
        created = seed_categories()
        self.stdout.write(
            self.style.SUCCESS(
                f"Categorias criadas: {created['categories']}; "
                f"subcategorias criadas: {created['subcategories']}."
            )
        )
