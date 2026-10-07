from datetime import date
from decimal import Decimal

import pytest

from common.formatting import describe_day, describe_period, format_brl, format_date

TODAY = date(2026, 10, 6)


@pytest.mark.parametrize(
    "value, expected",
    [
        (Decimal("0"), "R$ 0,00"),
        (Decimal("10"), "R$ 10,00"),
        (Decimal("10.5"), "R$ 10,50"),
        (Decimal("0.05"), "R$ 0,05"),
        (Decimal("1234.56"), "R$ 1.234,56"),
        (Decimal("2185.00"), "R$ 2.185,00"),
        (Decimal("1000000.00"), "R$ 1.000.000,00"),
        (Decimal("-50.00"), "-R$ 50,00"),
    ],
)
def test_format_brl(value, expected):
    assert format_brl(value) == expected


def test_format_brl_rejects_float():
    with pytest.raises(TypeError):
        format_brl(10.5)


def test_dates():
    assert format_date(date(2026, 10, 2)) == "02/10/2026"
    assert describe_day(TODAY, TODAY) == "hoje"
    assert describe_day(date(2026, 10, 5), TODAY) == "ontem"
    assert describe_day(date(2026, 10, 2), TODAY) == "em 02/10/2026"


@pytest.mark.parametrize(
    "start, end, expected",
    [
        (TODAY, TODAY, "hoje"),
        (date(2026, 10, 5), date(2026, 10, 5), "ontem"),
        (date(2026, 10, 1), TODAY, "em outubro"),
        (date(2026, 9, 1), date(2026, 9, 30), "em setembro"),
        (date(2025, 3, 1), date(2025, 3, 31), "em março de 2025"),
        (date(2026, 10, 2), date(2026, 10, 4), "de 02/10/2026 a 04/10/2026"),
    ],
)
def test_describe_period(start, end, expected):
    assert describe_period(start, end, TODAY) == expected
