"""Money helpers. Money is always Decimal; float is rejected."""

from decimal import Decimal, InvalidOperation

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
        try:
            result = Decimal(value.strip())
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
