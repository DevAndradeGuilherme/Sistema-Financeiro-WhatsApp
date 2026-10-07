import threading

import pytest
from django.db import IntegrityError, connection, connections

from apps.whatsapp.models import InboundMessage, MessageType
from apps.whatsapp.parsers import parse_evolution_event
from apps.whatsapp.services import receive_message

from . import payloads


@pytest.mark.django_db
def test_unique_constraint_blocks_duplicate_provider_id():
    fields = {
        "provider": "EVOLUTION",
        "instance": "test-instance",
        "provider_message_id": "ABC",
        "message_type": MessageType.TEXT,
    }
    InboundMessage.objects.create(**fields)
    with pytest.raises(IntegrityError):
        InboundMessage.objects.create(**fields)


@pytest.mark.django_db
def test_empty_provider_id_rejected_by_database():
    with pytest.raises(IntegrityError):
        InboundMessage.objects.create(
            instance="test-instance", provider_message_id="", message_type=MessageType.TEXT
        )


@pytest.mark.django_db
def test_service_returns_duplicate_on_second_call(make_user):
    make_user(phone_number="+5531999990001")
    message = parse_evolution_event(payloads.text()).message
    first = receive_message(message)
    second = receive_message(message)
    assert first.duplicate is False
    assert first.is_ready
    assert second.duplicate is True
    assert second.is_ready is False
    assert second.record.pk == first.record.pk


@pytest.mark.django_db(transaction=True)
def test_concurrent_deliveries_reserve_only_once(make_user):
    """Two threads (separate DB connections) deliver the same message at the same time."""
    make_user(phone_number="+5531999990001")
    message = parse_evolution_event(payloads.text()).message
    barrier = threading.Barrier(2)
    results, errors = [], []

    def deliver():
        try:
            barrier.wait(timeout=5)
            results.append(receive_message(message))
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=deliver) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert errors == []
    assert sorted(r.duplicate for r in results) == [False, True]
    assert InboundMessage.objects.count() == 1
    assert connection.vendor == "postgresql"
