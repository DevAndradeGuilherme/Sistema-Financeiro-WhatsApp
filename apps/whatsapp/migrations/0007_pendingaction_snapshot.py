"""PendingAction: tie the confirmation to the asking message and snapshot the candidates.

A pending confirmation is transient (10 minutes) and an old row has neither the asking
message nor a fingerprint, so it could never be validated safely: existing rows are dropped
before the new NOT NULL columns are added (the sender just asks again). Reversible: the
reverse removes the columns; dropped rows are not restored (they would be expired anyway).
"""

import django.db.models.deletion
from django.db import migrations, models


def drop_unverifiable_pending_actions(apps, schema_editor):
    apps.get_model("whatsapp", "PendingAction").objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("whatsapp", "0006_pendingaction"),
    ]

    operations = [
        migrations.RunPython(drop_unverifiable_pending_actions, migrations.RunPython.noop),
        migrations.AddField(
            model_name="pendingaction",
            name="inbound_message",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="whatsapp.inboundmessage",
                verbose_name="mensagem do pedido",
            ),
        ),
        migrations.AlterField(
            model_name="pendingaction",
            name="inbound_message",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="+",
                to="whatsapp.inboundmessage",
                verbose_name="mensagem do pedido",
            ),
        ),
        migrations.AddField(
            model_name="pendingaction",
            name="criteria",
            field=models.JSONField(default=dict, verbose_name="critérios aprovados"),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="pendingaction",
            name="fingerprint",
            field=models.CharField(
                default="", max_length=64, verbose_name="impressão dos candidatos"
            ),
            preserve_default=False,
        ),
        migrations.AddConstraint(
            model_name="pendingaction",
            constraint=models.CheckConstraint(
                condition=models.Q(("fingerprint__regex", "^[0-9a-f]{64}$")),
                name="pending_action_fingerprint_sha256",
            ),
        ),
    ]
