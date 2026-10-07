"""Phone number normalization to E.164.

Central place for the canonical representation (``+5531999998888``). Intentionally simple:
improve here (not at call sites) when the WhatsApp integration needs more rules.
"""

import re

BRAZIL_CODE = "55"
E164_PATTERN = r"^\+[1-9][0-9]{7,14}$"
_E164_RE = re.compile(E164_PATTERN)


class InvalidPhoneNumber(ValueError):
    pass


def normalize_phone(raw: str) -> str:
    """Normalize an untrusted phone string to E.164.

    - Accepts separators (spaces, dots, dashes, parentheses) and the ``00`` prefix.
    - Numbers without country code with 10 or 11 digits are treated as Brazilian (DDD + number).
    - Brazilian mobile numbers missing the ninth digit (8-digit subscriber starting with 6-9)
      get it inserted, so ``553199998888`` and ``5531999998888`` map to the same canonical value.
    """
    if not isinstance(raw, str):
        raise InvalidPhoneNumber("Telefone deve ser texto.")

    value = raw.strip()
    has_plus = value.startswith("+")
    if re.search(r"[^\d\s().+\-]", value) or "+" in value[1:]:
        raise InvalidPhoneNumber("Telefone contém caracteres inválidos.")

    digits = re.sub(r"\D", "", value)
    if not has_plus:
        if digits.startswith("00"):
            digits = digits[2:]
        elif digits.startswith("0") and len(digits) in (11, 12):
            digits = BRAZIL_CODE + digits[1:]  # national trunk prefix: 0 + DDD + number
        elif len(digits) in (10, 11):
            digits = BRAZIL_CODE + digits

    if digits.startswith(BRAZIL_CODE):
        digits = _normalize_brazil(digits)

    canonical = "+" + digits
    if not _E164_RE.match(canonical):
        raise InvalidPhoneNumber("Telefone inválido.")
    return canonical


def _normalize_brazil(digits: str) -> str:
    national = digits[len(BRAZIL_CODE) :]
    if len(national) not in (10, 11):
        raise InvalidPhoneNumber("Telefone brasileiro deve ter DDD e 8 ou 9 dígitos.")
    if national[0] == "0" or national[1] == "0":
        raise InvalidPhoneNumber("DDD inválido.")
    if len(national) == 10 and national[2] in "6789":
        national = national[:2] + "9" + national[2:]
    if len(national) == 11 and national[2] != "9":
        raise InvalidPhoneNumber("Celular brasileiro com 9 dígitos deve começar com 9.")
    return BRAZIL_CODE + national
