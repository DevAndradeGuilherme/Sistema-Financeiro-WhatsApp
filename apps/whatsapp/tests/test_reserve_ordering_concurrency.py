"""Final audit P1#3: a reserved message is part of the sender's conversation order at once.

Real PostgreSQL, separate threads/connections. M1 is reserved and then paused *between*
``_reserve`` and ``_classify`` (before the old code attributed it to the sender); M2, reserved
after it (greater pk), runs to the end first. Ordering is the persisted pk: M2 must see M1 in
between and never confirm an old question nor correct an old registration.
"""

import threading
from decimal import Decimal
from unittest import mock

import pytest
from django.db import connections
from django.test import Client

from apps.transactions.models import Transaction, TransactionChange
from apps.whatsapp import services
from apps.whatsapp.models import InboundMessage, PendingAction

from .test_mutation_concurrency import ask_all, incomes, scripted_ai  # noqa: F401 (fixture)
from .test_mutation_targets import create, update
from .test_pipeline import deliver, interp

pytestmark = pytest.mark.django_db(transaction=True)
Action = TransactionChange.Action


@pytest.fixture
def evolution():
    with mock.patch("apps.whatsapp.pipeline.EvolutionClient") as cls:
        yield cls.return_value.send_text


class PausedAfterReserve:
    """Delivers ``message_id`` in a thread and holds it right after its reservation."""

    def __init__(self, text: str, message_id: str):
        self.text, self.message_id = text, message_id
        self.reserved, self.release = threading.Event(), threading.Event()
        self.errors: list[Exception] = []
        real = services._classify

        def classify(record, message):
            if message.provider_message_id == self.message_id:
                self.reserved.set()
                if not self.release.wait(timeout=30):  # pragma: no cover - surfaced below
                    raise TimeoutError("barrier not released")
            return real(record, message)

        self._patch = mock.patch.object(services, "_classify", side_effect=classify)
        self._thread = threading.Thread(target=self._run)

    def _run(self):
        try:
            deliver(Client(), self.text, self.message_id)
        except Exception as exc:  # pragma: no cover - surfaced by finish()
            self.errors.append(exc)
        finally:
            connections.close_all()

    def __enter__(self):
        self._patch.start()
        self._thread.start()
        assert self.reserved.wait(timeout=30), "M1 was never reserved"
        return self

    def finish(self):
        self.release.set()
        self._thread.join(timeout=30)
        assert not self._thread.is_alive()
        assert self.errors == []

    def __exit__(self, *exc):
        self.release.set()
        self._thread.join(timeout=30)
        self._patch.stop()


def test_reserved_message_is_attributed_to_the_sender_before_classification(alice, evolution):
    with scripted_ai({"ajuda": interp(intent="HELP")}), PausedAfterReserve("ajuda", "M1") as m1:
        reserved = InboundMessage.objects.get(provider_message_id="M1")
        assert reserved.status == InboundMessage.Status.RECEIVED
        assert reserved.user_id == alice.pk  # visible to the ordering checks already
        m1.finish()


def test_slow_intermediate_message_blocks_old_confirmation(alice, incomes, evolution):  # noqa: F811
    """A) pending question → M1 reserved (slow) → "sim" reserved after it finishes first:
    it is not the next message after the question → nothing is deleted."""
    ask_all(evolution)
    with scripted_ai({"ajuda": interp(intent="HELP")}), PausedAfterReserve("ajuda", "M1") as m1:
        answer = deliver(Client(), "sim", "C1")
        answer.refresh_from_db()
        assert answer.pk > InboundMessage.objects.get(provider_message_id="M1").pk
        assert answer.error_code == "NO_PENDING_ACTION"
        assert not Transaction.objects.deleted().exists()
        m1.finish()

    assert not TransactionChange.objects.filter(action=Action.DELETE).exists()
    assert not Transaction.objects.deleted().exists()
    assert not PendingAction.objects.exists()


@pytest.mark.parametrize(
    "proposal, code",
    [
        (update(correction=True, amount="80"), "CORRECTION_TARGET_UNCLEAR"),
        (None, "CORRECTION_NOT_CREATED"),  # model proposes a CREATE of 80
    ],
    ids=["update", "create"],
)
def test_slow_intermediate_message_blocks_old_correction(alice, catalog, evolution, proposal, code):
    """B) correctable CREATE → M1 reserved (slow) → "80" reserved after it finishes first:
    the correction link is broken → no edit of the old registration and no new one."""
    proposals = {
        "Gastei 100 no mercado": create("EXPENSE", "100", catalog["alimentacao"], "mercado"),
        "ajuda": interp(intent="HELP"),
        "80": proposal or create("EXPENSE", "80", catalog["outros_gastos"]),
    }
    with scripted_ai(proposals):
        deliver(Client(), "Gastei 100 no mercado", "M0")
        with PausedAfterReserve("ajuda", "M1") as m1:
            correction = deliver(Client(), "80", "M2")
            correction.refresh_from_db()
            assert correction.error_code == code
            m1.finish()

    txn = Transaction.objects.get()
    assert txn.amount == Decimal("100.00")
    assert not TransactionChange.objects.filter(action=Action.UPDATE).exists()


def test_unknown_sender_gains_nothing_from_reservation(alice, evolution):
    """Ordering identity is not authorization: an unknown number stays unattributed/IGNORED."""
    record = deliver(Client(), "ajuda", "X1", remote_jid="5531911112222@s.whatsapp.net")
    assert record.user_id is None
    assert record.status == InboundMessage.Status.IGNORED
    assert record.ignore_reason == InboundMessage.IgnoreReason.UNKNOWN_SENDER


def test_own_echo_does_not_join_the_conversation_order(alice, incomes, evolution):  # noqa: F811
    """Our own reply echoed by the webhook (fromMe, same chat) never breaks "sim"."""
    ask_all(evolution)
    echo = deliver(Client(), "Confirma?", "E1", from_me=True)
    assert echo.user_id is None
    answer = deliver(Client(), "sim", "C1")
    answer.refresh_from_db()
    assert answer.error_code == ""
    assert Transaction.objects.deleted().count() == 2
