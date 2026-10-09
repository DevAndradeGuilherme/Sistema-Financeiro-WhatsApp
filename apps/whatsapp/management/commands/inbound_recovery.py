"""Operational diagnosis and manual recovery of inbound messages (docs/OPERATIONS.md).

Dry-run by default: nothing changes without ``--execute``. Never reprocesses messages, never
touches the ledger and never prints message text, transcripts, amounts or full phone numbers.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError

from apps.whatsapp import recovery
from apps.whatsapp.models import InboundMessage

LIST, INSPECT, RESEND, MARK_FAILED = "list", "inspect", "resend-reply", "mark-failed"
MAX_LIST_LIMIT = 1000


class Command(BaseCommand):
    help = (
        "Diagnostica mensagens interrompidas e executa recuperações comprovadamente seguras "
        "(dry-run por padrão; mutações exigem --execute)."
    )

    def add_arguments(self, parser):
        parser.add_argument("operation", choices=[LIST, INSPECT, RESEND, MARK_FAILED])
        parser.add_argument("message_id", nargs="?", type=int, help="ID interno da mensagem.")
        parser.add_argument(
            "--execute", action="store_true", help="Aplica a ação (sem isso: dry-run)."
        )
        parser.add_argument(
            "--min-age-minutes",
            type=int,
            default=int(recovery.STUCK_MIN_AGE.total_seconds() // 60),
            help="Idade mínima sem atividade para considerar RECEIVED/READY travada.",
        )
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument(
            "--no-reply",
            action="store_true",
            help="mark-failed: não envia a resposta de erro técnico ao usuário.",
        )

    def handle(self, *args, operation, message_id, execute, min_age_minutes, **options):
        min_age = timedelta(minutes=min_age_minutes)
        if min_age < recovery.MIN_ALLOWED_AGE:
            raise CommandError(
                f"--min-age-minutes deve ser >= {recovery.MIN_ALLOWED_AGE.seconds // 60}."
            )
        if not 1 <= options["limit"] <= MAX_LIST_LIMIT:
            raise CommandError(f"--limit deve estar entre 1 e {MAX_LIST_LIMIT}.")
        if options["no_reply"] and operation != MARK_FAILED:
            raise CommandError("--no-reply só se aplica a mark-failed.")
        if operation == LIST:
            if execute:
                raise CommandError("list é somente leitura; --execute não se aplica.")
            return self._list(limit=options["limit"], min_age=min_age)
        if message_id is None:
            raise CommandError(f"{operation} exige o ID interno da mensagem.")

        try:
            if operation == INSPECT:
                if execute:
                    raise CommandError("inspect é somente leitura; --execute não se aplica.")
                self._show(recovery.inspect(message_id, min_age=min_age))
                return
            if operation == RESEND:
                result = recovery.resend_reply(message_id, execute=execute)
            else:
                result = recovery.mark_stuck_failed(
                    message_id, execute=execute, min_age=min_age, notify=not options["no_reply"]
                )
        except InboundMessage.DoesNotExist:
            raise CommandError(f"Mensagem {message_id} não encontrada.") from None
        except recovery.RecoveryRefused as exc:
            self._show(exc.diagnosis)
            raise CommandError(
                f"RECUSADO: {operation} não é seguro para esta mensagem "
                f"(action={exc.diagnosis.action} reason={exc.diagnosis.reason})."
            ) from None

        self._show(result.diagnosis)
        if not execute:
            self.stdout.write(
                self.style.WARNING(
                    f"DRY-RUN: nada foi alterado. {operation} é seguro; repita com --execute."
                )
            )
            return
        if not result.executed:
            self.stdout.write(self.style.WARNING("Nada alterado: estado mudou durante a execução."))
            return
        reply = {None: "não enviada", True: "enviada", False: "falhou"}[result.reply_sent]
        self.stdout.write(self.style.SUCCESS(f"EXECUTADO: {operation}; resposta {reply}."))

    def _list(self, *, limit: int, min_age: timedelta) -> None:
        rows = recovery.needs_attention(limit=limit, min_age=min_age)
        self.stdout.write(
            "id | provider_ref | type | status | error_code | intent | updated_at | "
            "financial_effect | action | reason"
        )
        for d in rows:
            self.stdout.write(
                " | ".join(
                    [
                        str(d.message_id),
                        d.provider_ref,
                        d.message_type,
                        d.status,
                        d.error_code or "-",
                        d.intent or "-",
                        d.updated_at.isoformat(timespec="seconds"),
                        _yes(d.has_financial_effect),
                        d.action,
                        d.reason,
                    ]
                )
            )
        self.stdout.write(f"{len(rows)} mensagem(ns) listada(s) (limite {limit}).")

    def _show(self, d: recovery.Diagnosis) -> None:
        fields = [
            ("id", d.message_id),
            ("provider_ref", d.provider_ref),
            ("type", d.message_type),
            ("status", d.status),
            ("error_code", d.error_code or "-"),
            ("ignore_reason", d.ignore_reason or "-"),
            ("intent", d.intent or "-"),
            ("received_at", d.received_at.isoformat(timespec="seconds")),
            ("updated_at", d.updated_at.isoformat(timespec="seconds")),
            (
                "processed_at",
                d.processed_at.isoformat(timespec="seconds") if d.processed_at else "-",
            ),
            ("user_id", d.user_id or "-"),
            ("phone", d.phone),
            ("transaction_created", _yes(d.created_transaction)),
            ("ledger_changes", _yes(d.ledger_changes)),
            ("receipt_status", d.receipt_status or "-"),
            ("safe_action", d.action),
            ("reason", d.reason),
        ]
        for name, value in fields:
            self.stdout.write(f"{name}: {value}")


def _yes(value: bool) -> str:
    return "sim" if value else "não"
