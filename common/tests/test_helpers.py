from datetime import UTC, date, datetime
from decimal import Decimal
from unittest import mock

import pytest

from common.dates import today_local
from common.money import InvalidAmount, has_at_most_two_places, to_decimal
from common.phone import InvalidPhoneNumber, normalize_phone


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("+55 31 99999-8888", "+5531999998888"),
        ("(31) 99999-8888", "+5531999998888"),
        ("31999998888", "+5531999998888"),
        ("5531999998888", "+5531999998888"),
        ("005531999998888", "+5531999998888"),
        ("031999998888", "+5531999998888"),
        ("553199998888", "+5531999998888"),  # WhatsApp JID without the ninth digit
        ("3133334444", "+553133334444"),  # landline keeps 8 digits
        ("+1 415 555 2671", "+14155552671"),
    ],
)
def test_normalize_phone_returns_e164(raw, expected):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["", "abc", "123", "+55 00 99999-8888", "+5531899998888", "+55+31"])
def test_normalize_phone_rejects_invalid(raw):
    with pytest.raises(InvalidPhoneNumber):
        normalize_phone(raw)


def test_to_decimal_rejects_float_and_bool():
    with pytest.raises(InvalidAmount):
        to_decimal(10.5)
    with pytest.raises(InvalidAmount):
        to_decimal(True)


@pytest.mark.parametrize("raw", ["abc", "NaN", "Infinity", "10,50", "1e3", "1E-2"])
def test_to_decimal_rejects_invalid_strings(raw):
    with pytest.raises(InvalidAmount):
        to_decimal(raw)


def test_to_decimal_preserves_cents():
    assert to_decimal("0.10") + to_decimal("0.20") == Decimal("0.30")


def test_has_at_most_two_places():
    assert has_at_most_two_places(Decimal("10"))
    assert has_at_most_two_places(Decimal("10.500"))
    assert not has_at_most_two_places(Decimal("10.505"))


def test_today_local_uses_business_timezone():
    # 01:30 UTC on Oct 7 is still Oct 6 in America/Sao_Paulo (UTC-3).
    with mock.patch(
        "django.utils.timezone.now", return_value=datetime(2026, 10, 7, 1, 30, tzinfo=UTC)
    ):
        assert today_local() == date(2026, 10, 6)
