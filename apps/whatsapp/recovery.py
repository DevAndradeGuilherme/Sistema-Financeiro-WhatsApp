"""Manual recovery of interrupted inbound messages (runbook: docs/OPERATIONS.md).

Diagnosis is read-only. The only mutations are explicit, per message, and provably safe:

- ``RESEND_REPLY``: the effect is committed (PROCESSED) and only the reply failed. The card is
  rebuilt from the persisted transaction by the same function the pipeline uses, and only for
  a CREATE whose transaction is unchanged since creation. Nothing financial runs again.
- ``MARK_FAILED``: a RECEIVED/READY message older than the in-flight window, with no effect in
  the ledger. It becomes FAILED (fencing any late worker, which re-checks the status under the
  same row lock) and the user gets the pipeline's existing technical-error reply.

The interpretation, transcription, vision and financial services are **never** re-executed:
the message may be old ("ontem" would resolve to another day) and the user may already have
sent it again, so reprocessing could duplicate a financial effect that idempotency cannot see.
Anything ambiguous is refused for manual investigation; nothing here edits the ledger.

Every check runs again under ``SELECT ... FOR UPDATE`` of the message (the same lock the
effect commit takes), so concurrent recoveries or late workers cannot double an action.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.transactions.models import Transaction, TransactionChange
from common.phone import mask_phone

from . import actions, messages
from .models import InboundMessage, ReceiptAttachment
from .pipeline import REPLY_SEND_FAILED, idempotency_key, send_reply
from .services import mark_failed

logger = logging.getLogger(__name__)

Status = InboundMessage.Status

# Longer than the worst synchronous request (Gunicorn timeout, 120 s by default) with margin:
# a younger RECEIVED/READY message may still be in flight.
STUCK_MIN_AGE = timedelta(minutes=15)
MIN_ALLOWED_AGE = timedelta(minutes=5)

# error_code values written by recovery (free text column, no schema change).
REPLY_RESEND_PENDING = "REPLY_RESEND_PENDING"  # claimed; if it persists, delivery is unknown
REPLY_RESENT = "REPLY_RESENT"

CREATE_INTENT = "CREATE_TRANSACTION"
ORIGIN_MAX_LENGTH = TransactionChange._meta.get_field("origin").max_length


class Action(StrEnum):
    NONE = "NONE"  # terminal and consistent: nothing to do
    WAIT = "WAIT"  # may still be in flight: look again later
    RESEND_REPLY = "RESEND_REPLY"
    MARK_FAILED = "MARK_FAILED"
    REFUSE = "REFUSE"  # ambiguous/anomalous or not provable: manual investigation only


@dataclass(frozen=True)
class Diagnosis:
    """Safe operational view of one message: no text, transcript, amount or full phone."""

    message_id: int
    provider_ref: str
    message_type: str
    status: str
    error_code: str
    ignore_reason: str
    intent: str
    received_at: datetime
    updated_at: datetime
    processed_at: datetime | None
    user_id: int | None
    phone: str
    created_transaction: bool  # Transaction with this message's idempotency key (any state)
    ledger_changes: bool  # TransactionChange whose origin is this message
    receipt_status: str
    action: Action
    reason: str

    @property
    def has_financial_effect(self) -> bool:
        return self.created_transaction or self.ledger_changes


@dataclass(frozen=True)
class Result:
    diagnosis: Diagnosis
    executed: bool = False
    reply_sent: bool | None = None


class RecoveryRefused(Exception):
    def __init__(self, diagnosis: Diagnosis):
        super().__init__(diagnosis.reason)
        self.diagnosis = diagnosis


def diagnose(
    record: InboundMessage, *, now: datetime | None = None, min_age: timedelta = STUCK_MIN_AGE
) -> Diagnosis:
    key = idempotency_key(record)
    created = Transaction.objects.filter(idempotency_key=key).exists()
    # TransactionChange stores origins in a bounded audit field. Match the exact value written
    # by transactions.services._record so long, but valid, provider identifiers cannot hide an
    # UPDATE/UNDO effect from recovery.
    changes = TransactionChange.objects.filter(origin=key[:ORIGIN_MAX_LENGTH]).exists()
    receipt = (
        ReceiptAttachment.objects.filter(inbound_message_id=record.pk)
        .values_list("status", flat=True)
        .first()
    )
    action, reason = _decide(
        record,
        effect=created or changes,
        receipt=receipt,
        age=(now or timezone.now()) - record.updated_at,
        min_age=min_age,
    )
    return Diagnosis(
        message_id=record.pk,
        provider_ref=mask_provider_id(record.provider_message_id),
        message_type=record.message_type,
        status=record.status,
        error_code=record.error_code,
        ignore_reason=record.ignore_reason,
        intent=record.intent,
        received_at=record.received_at,
        updated_at=record.updated_at,
        processed_at=record.processed_at,
        user_id=record.user_id,
        phone=mask_phone(record.sender_phone),
        created_transaction=created,
        ledger_changes=changes,
        receipt_status=receipt or "",
        action=action,
        reason=reason,
    )


def _decide(
    record: InboundMessage, *, effect: bool, receipt: str | None, age: timedelta, min_age
) -> tuple[Action, str]:
    status = record.status
    if status in (Status.RECEIVED, Status.READY):
        # Effect and PROCESSED commit together, so an effect here contradicts the invariant.
        if effect or receipt:
            return Action.REFUSE, "EFFECT_WITHOUT_TERMINAL_STATUS"
        if age < min_age:
            return Action.WAIT, "MAY_BE_IN_FLIGHT"
        return Action.MARK_FAILED, "STUCK_WITHOUT_EFFECT"
    if status in (Status.FAILED, Status.IGNORED):
        if effect:
            return Action.REFUSE, "EFFECT_ON_NON_PROCESSED"
        return Action.NONE, f"{status}_WITHOUT_EFFECT"

    # PROCESSED: whatever effect it had is committed and final.
    if record.error_code == REPLY_RESEND_PENDING:
        return Action.REFUSE, "RESEND_OUTCOME_UNKNOWN"
    if record.error_code != REPLY_SEND_FAILED:
        return Action.NONE, "PROCESSED"
    if record.intent != CREATE_INTENT:
        # Query answers would be recomputed with today's data; update/undo texts depend on
        # the state at that time. Not provably the reply that failed.
        return Action.REFUSE, "REPLY_NOT_DERIVABLE"
    txn = record.transaction
    if txn is None or txn.idempotency_key != idempotency_key(record):
        return Action.REFUSE, "EFFECT_NOT_CONFIRMED"
    if txn.deleted_at is not None or list(txn.changes.values_list("action", flat=True)) != [
        TransactionChange.Action.CREATE
    ]:
        return Action.REFUSE, "TRANSACTION_CHANGED_SINCE"
    if not record.sender_phone or record.user is None or not record.user.is_active:
        return Action.REFUSE, "RECIPIENT_UNAVAILABLE"
    return Action.RESEND_REPLY, "REPLY_DERIVABLE_FROM_LEDGER"


def needs_attention(*, limit: int = 50, min_age: timedelta = STUCK_MIN_AGE) -> list[Diagnosis]:
    """Messages that are not (or may not be) cleanly terminal, most recent first."""
    records = (
        InboundMessage.objects.filter(
            Q(status__in=[Status.RECEIVED, Status.READY, Status.FAILED])
            | Q(error_code__in=[REPLY_SEND_FAILED, REPLY_RESEND_PENDING])
        )
        .select_related("user", "transaction")
        .order_by("-received_at")[:limit]
    )
    now = timezone.now()
    return [diagnose(record, now=now, min_age=min_age) for record in records]


def inspect(message_id: int, *, min_age: timedelta = STUCK_MIN_AGE) -> Diagnosis:
    record = InboundMessage.objects.select_related("user", "transaction").get(pk=message_id)
    return diagnose(record, min_age=min_age)


def resend_reply(message_id: int, *, execute: bool) -> Result:
    """Resend the registration card of a committed CREATE whose reply failed (at most once)."""
    with transaction.atomic():
        record = _locked(message_id)
        diagnosis = diagnose(record)
        if diagnosis.action != Action.RESEND_REPLY:
            raise RecoveryRefused(diagnosis)
        if not execute:
            return Result(diagnosis)
        reply = actions.created_reply(record.transaction)
        _set_error_code(record, expected=REPLY_SEND_FAILED, new=REPLY_RESEND_PENDING)

    # HTTP outside the transaction. If the process dies here the claim stays PENDING and the
    # message is refused from then on (delivery unknown), never resent blindly.
    sent = send_reply(record, reply)
    _set_error_code(
        record, expected=REPLY_RESEND_PENDING, new=REPLY_RESENT if sent else REPLY_SEND_FAILED
    )
    _log("recovery.reply_resent" if sent else "recovery.reply_resend_failed", record)
    return Result(diagnosis, executed=True, reply_sent=sent)


def mark_stuck_failed(
    message_id: int, *, execute: bool, min_age: timedelta = STUCK_MIN_AGE, notify: bool = True
) -> Result:
    """Close a stuck RECEIVED/READY message without effect as FAILED (no reprocessing)."""
    if min_age < MIN_ALLOWED_AGE:
        raise ValueError("min_age is shorter than the in-flight window.")
    with transaction.atomic():
        record = _locked(message_id)
        diagnosis = diagnose(record, min_age=min_age)
        if diagnosis.action != Action.MARK_FAILED:
            raise RecoveryRefused(diagnosis)
        if not execute:
            return Result(diagnosis)
        # Same conditional transition as the pipeline (only from RECEIVED/READY).
        changed = mark_failed(record, f"STUCK_{diagnosis.status}")
    _log("recovery.marked_failed", record)

    sent = None
    if changed and notify and record.sender_phone and record.user and record.user.is_active:
        sent = send_reply(record, messages.TECHNICAL_ERROR)
    return Result(diagnosis, executed=changed, reply_sent=sent)


def mask_provider_id(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}…{value[-4:]}"


def _locked(message_id: int) -> InboundMessage:
    return (
        InboundMessage.objects.select_for_update(of=("self",))
        .select_related("user", "transaction")
        .get(pk=message_id)
    )


def _set_error_code(record: InboundMessage, *, expected: str, new: str) -> None:
    InboundMessage.objects.filter(
        pk=record.pk, status=Status.PROCESSED, error_code=expected
    ).update(error_code=new, updated_at=timezone.now())


def _log(event: str, record: InboundMessage) -> None:
    logger.info(
        "%s message_id=%s provider_message_id=%s message_type=%s",
        event,
        record.pk,
        record.provider_message_id,
        record.message_type,
    )
