from django.apps import AppConfig
from django.db.models.signals import post_migrate


class CommonConfig(AppConfig):
    name = "common"

    def ready(self) -> None:
        from common.db import enable_row_level_security

        # Runs once per migrate (sender=self), after all apps have been migrated.
        post_migrate.connect(enable_row_level_security, sender=self, dispatch_uid="common_rls")
