"""Money helpers. Money is always Decimal; float is rejected."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TWO_PLACES = Decimal("0.01")


class InvalidAmount(ValueError):
    pass


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Convert a trusted-type value to Decimal without passing through float.

    Strings must use a dot as decimal separator (e.g. "10.50").
    """
    if isinstance(value, (bool, float)):
        raise InvalidAmount("Valores monetários não podem ser float.")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, str):
        normalized = value.strip()
        if "e" in normalized.lower():
            raise InvalidAmount("Valor monetário inválido.")
        try:
            result = Decimal(normalized)
        except InvalidOperation as exc:
            raise InvalidAmount("Valor monetário inválido.") from exc
    else:
        raise InvalidAmount("Tipo de valor monetário não suportado.")

    if not result.is_finite():
        raise InvalidAmount("Valor monetário inválido.")
    return result


def has_at_most_two_places(value: Decimal) -> bool:
    """True for 10, 10.5, 10.50 and 10.500; False for 10.505."""
    if value.as_tuple().exponent >= -2:
        return True
    try:
        return value == value.quantize(TWO_PLACES)
    except InvalidOperation:
        return False


ONE_DECIMAL = Decimal("0.1")


def percent(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    """``numerator / denominator * 100`` with one decimal place, ROUND_HALF_UP, in Decimal.

    None when the denominator is zero (never divides by zero). Values above 100% or negative
    are returned as they are (e.g. expenses greater than income).
    """
    if denominator == 0:
        return None
    return (Decimal(numerator) / Decimal(denominator) * 100).quantize(
        ONE_DECIMAL, rounding=ROUND_HALF_UP
    )
