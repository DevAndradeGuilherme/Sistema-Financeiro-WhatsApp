import json
from unittest import mock

import pytest
from django.db import OperationalError
from django.test import Client, override_settings

from apps.accounts.models import User
from apps.transactions.models import Transaction
from apps.whatsapp.models import InboundMessage, MessageType

from . import payloads

pytestmark = pytest.mark.django_db

URL = "/api/v1/webhooks/evolution/"
SECRET = "test-webhook-secret"
Status = InboundMessage.Status
Reason = InboundMessage.IgnoreReason


def post(client, payload, secret=SECRET, raw=None):
    headers = {} if secret is None else {"X-Webhook-Secret": secret}
    body = raw if raw is not None else json.dumps(payload)
    return client.post(URL, data=body, content_type="application/json", headers=headers)


@pytest.fixture
def known_user(make_user):
    return make_user(username="alice", phone_number="+5531999990001")


@pytest.fixture(autouse=True)
def pipeline():
    """The AI pipeline is covered in test_pipeline.py; here it is stubbed."""
    with mock.patch("apps.whatsapp.views.handle_ready_message") as handler:
        yield handler


# --- Authentication / HTTP --------------------------------------------------------------


def test_authenticated_valid_text_event_is_accepted(client, known_user, pipeline):
    response = post(client, payloads.text())
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}

    record = InboundMessage.objects.get()
    assert record.status == Status.READY
    assert record.user == known_user
    assert record.sender_phone == "+5531999990001"
    assert record.text == "10 reais de gasolina"
    assert record.message_type == MessageType.TEXT
    assert record.provider_message_id == "3EB0A1B2C3D4E5F6"
    assert record.instance == "test-instance"
    pipeline.assert_called_once_with(record)


@pytest.mark.parametrize("secret", ["wrong-secret", "", "test-webhook-secret "])
def test_wrong_secret_is_rejected(client, known_user, pipeline, secret):
    response = post(client, payloads.text(), secret=secret)
    assert response.status_code == 401
    assert not InboundMessage.objects.exists()
    pipeline.assert_not_called()


def test_missing_secret_is_rejected(client, known_user, pipeline):
    response = post(client, payloads.text(), secret=None)
    assert response.status_code == 401
    assert not InboundMessage.objects.exists()
    pipeline.assert_not_called()


def test_secret_is_checked_before_parsing(client):
    assert post(client, None, secret="nope", raw="not json").status_code == 401


@override_settings(EVOLUTION_WEBHOOK_SECRET="")
def test_unconfigured_secret_fails_clearly(client):
    client.raise_request_exception = True
    with pytest.raises(Exception, match="EVOLUTION_WEBHOOK_SECRET"):
        post(client, payloads.text())


def test_invalid_json_returns_400(client):
    response = post(client, None, raw="{not json")
    assert response.status_code == 400
    assert not InboundMessage.objects.exists()


@pytest.mark.parametrize("raw", ["[]", '"x"', '{"event": "messages.upsert"}', "{}"])
def test_malformed_payload_returns_400(client, raw):
    assert post(client, None, raw=raw).status_code == 400
    assert not InboundMessage.objects.exists()


def test_upsert_without_key_returns_400(client):
    payload = payloads.text()
    del payload["data"]["key"]
    assert post(client, payload).status_code == 400


def test_get_is_not_allowed(client):
    assert client.get(URL, headers={"X-Webhook-Secret": SECRET}).status_code == 405


def test_csrf_exempt_only_for_webhook(known_user, pipeline):
    csrf_client = Client(enforce_csrf_checks=True)
    assert post(csrf_client, payloads.text()).status_code == 200
    # CSRF stays enforced elsewhere (e.g. admin login).
    assert csrf_client.post("/admin/login/", {"username": "x"}).status_code == 403


def test_other_instance_is_rejected(client, known_user, pipeline):
    response = post(client, payloads.text(instance="other-instance"))
    assert response.status_code == 403
    assert not InboundMessage.objects.exists()


def test_unknown_event_is_ignored(client, pipeline):
    response = post(client, payloads.other_event("connection.update"))
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert not InboundMessage.objects.exists()
    pipeline.assert_not_called()


# --- Filters ------------------------------------------------------------------------------


def _assert_ignored(response, reason):
    assert response.status_code == 200
    record = InboundMessage.objects.get()
    assert record.status == Status.IGNORED
    assert record.ignore_reason == reason
    assert record.text == ""
    return record


def test_from_me_is_ignored(client, known_user, pipeline):
    _assert_ignored(post(client, payloads.text(from_me=True)), Reason.FROM_ME)
    pipeline.assert_not_called()


def test_group_is_ignored(client, known_user, pipeline):
    users_before = User.objects.count()
    payload = payloads.text(remote_jid="120363000000000000@g.us")
    record = _assert_ignored(post(client, payload), Reason.GROUP)
    assert record.user is None
    assert User.objects.count() == users_before
    assert not Transaction.objects.exists()
    pipeline.assert_not_called()


def test_broadcast_is_ignored(client, pipeline):
    _assert_ignored(post(client, payloads.text(remote_jid="status@broadcast")), Reason.BROADCAST)


def test_unresolved_lid_sender_is_ignored(client, pipeline):
    payload = payloads.text(remote_jid="123456789012345@lid")
    _assert_ignored(post(client, payload), Reason.SENDER_UNRESOLVED)


def test_audio_is_routed_to_transcription(client, known_user, pipeline):
    """V1.3: audio from a known user is reserved (RECEIVED) and handed to the audio handler
    (download + transcription, covered in test_audio.py); the text pipeline is not called here."""
    with mock.patch("apps.whatsapp.views.handle_audio_message") as handler:
        response = post(client, payloads.audio())
    assert response.status_code == 200
    record = InboundMessage.objects.get()
    assert (record.message_type, record.status, record.user) == (
        MessageType.AUDIO,
        Status.RECEIVED,
        known_user,
    )
    handler.assert_called_once()
    assert handler.call_args.args[0] == record
    pipeline.assert_not_called()


def test_image_is_recognized_but_not_processed(client, known_user, pipeline):
    record = _assert_ignored(post(client, payloads.image()), Reason.MEDIA_NOT_SUPPORTED)
    assert record.message_type == MessageType.IMAGE
    pipeline.assert_not_called()


def test_unsupported_type_is_ignored(client, known_user, pipeline):
    record = _assert_ignored(post(client, payloads.sticker()), Reason.UNSUPPORTED_TYPE)
    assert record.message_type == MessageType.UNSUPPORTED
    pipeline.assert_not_called()


def test_empty_text_is_ignored(client, known_user, pipeline):
    _assert_ignored(post(client, payloads.text("   ")), Reason.EMPTY_TEXT)


def test_too_long_text_is_ignored(client, known_user, pipeline):
    _assert_ignored(post(client, payloads.text("a" * 5000)), Reason.TEXT_TOO_LONG)


def test_unknown_sender_is_not_created(client, pipeline):
    response = post(client, payloads.text(remote_jid="5521988887777@s.whatsapp.net"))
    record = _assert_ignored(response, Reason.UNKNOWN_SENDER)
    assert record.sender_phone == "+5521988887777"
    assert record.user is None
    assert not User.objects.exists()
    pipeline.assert_not_called()


def test_inactive_user_is_ignored(client, known_user, pipeline):
    known_user.is_active = False
    known_user.save()
    _assert_ignored(post(client, payloads.text()), Reason.INACTIVE_USER)
    pipeline.assert_not_called()


def test_sender_identified_through_phone_normalizer(client, known_user, pipeline):
    # JID without the ninth digit maps to the canonical phone stored for the user.
    post(client, payloads.text(remote_jid="553199990001@s.whatsapp.net"))
    assert InboundMessage.objects.get().user == known_user


def test_payload_apikey_is_never_stored(client, known_user, pipeline):
    post(client, payloads.text())
    record = InboundMessage.objects.values().get()
    assert "instance-token-that-must-never-be-stored" not in json.dumps(record, default=str)


def test_webhook_never_creates_transactions(client, known_user, pipeline):
    post(client, payloads.text("10 reais de gasolina"))
    assert not Transaction.objects.exists()


# --- Idempotency --------------------------------------------------------------------------


def test_duplicate_delivery_does_not_process_again(client, known_user, pipeline):
    first = post(client, payloads.text())
    second = post(client, payloads.text())
    assert first.json() == {"status": "ready"}
    assert second.status_code == 200
    assert second.json() == {"status": "duplicate"}
    assert InboundMessage.objects.count() == 1
    pipeline.assert_called_once()


def test_same_id_on_other_instance_is_independent(client, known_user, pipeline):
    post(client, payloads.text())
    with override_settings(EVOLUTION_INSTANCE_NAME="second-instance"):
        post(client, payloads.text(instance="second-instance"))
    assert InboundMessage.objects.count() == 2


def test_duplicate_of_ignored_message_stays_ignored(client, pipeline):
    post(client, payloads.text(from_me=True))
    assert post(client, payloads.text(from_me=True)).json() == {"status": "duplicate"}
    assert InboundMessage.objects.count() == 1


# --- Failures -----------------------------------------------------------------------------


def test_database_error_before_reservation_returns_503(client, known_user):
    with mock.patch("apps.whatsapp.views.receive_message", side_effect=OperationalError("db down")):
        response = post(client, payloads.text())
    assert response.status_code == 503


def test_pipeline_error_marks_failed_and_returns_200(client, known_user):
    with mock.patch("apps.whatsapp.views.handle_ready_message", side_effect=RuntimeError("boom")):
        response = post(client, payloads.text())
    assert response.status_code == 200
    record = InboundMessage.objects.get()
    assert record.status == Status.FAILED
    assert record.error_code == "PIPELINE_ERROR"
    # A redelivery does not run the pipeline again.
    with mock.patch("apps.whatsapp.views.handle_ready_message") as handler:
        assert post(client, payloads.text()).json() == {"status": "duplicate"}
    handler.assert_not_called()
