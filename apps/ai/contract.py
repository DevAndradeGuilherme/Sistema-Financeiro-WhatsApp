"""Structured contract between the application and the model (docs/AI_CONTRACT.md).

Plain dataclasses, no domain imports. The model output is an untrusted *proposal*: this module
only checks its shape; Django (apps.whatsapp.actions) validates meaning and authorizes.
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any

CONTRACT_VERSION = "v1.0"

INTENTS = ("CREATE_TRANSACTION", "QUERY_TRANSACTIONS", "HELP", "UNKNOWN")
TRANSACTION_TYPES = ("EXPENSE", "INCOME")
QUERY_KINDS = ("TOTAL", "BALANCE")


def _nullable(json_type: str, **extra: Any) -> dict:
    return {"type": [json_type, "null"], **extra}


OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "transaction_type": _nullable("string", enum=[*TRANSACTION_TYPES, None]),
        "amount": _nullable("string"),
        "description": _nullable("string"),
        "category_id": _nullable("integer"),
        "subcategory_id": _nullable("integer"),
        "transaction_date": _nullable("string"),
        "query_kind": _nullable("string", enum=[*QUERY_KINDS, None]),
        "start_date": _nullable("string"),
        "end_date": _nullable("string"),
    },
    "required": [
        "intent",
        "transaction_type",
        "amount",
        "description",
        "category_id",
        "subcategory_id",
        "transaction_date",
        "query_kind",
        "start_date",
        "end_date",
    ],
    "additionalProperties": False,
}


class InvalidOutput(ValueError):
    """Model output does not match the contract."""


@dataclass(frozen=True)
class CatalogSubcategory:
    id: int
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogCategory:
    id: int
    name: str
    kind: str  # EXPENSE | INCOME | BOTH
    aliases: tuple[str, ...] = ()
    subcategories: tuple[CatalogSubcategory, ...] = ()


@dataclass(frozen=True)
class InterpretationContext:
    """Everything the model receives besides the message text. No user data."""

    reference_date: date
    timezone: str
    catalog: tuple[CatalogCategory, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Interpretation:
    intent: str
    transaction_type: str | None
    amount: str | None
    description: str | None
    category_id: int | None
    subcategory_id: int | None
    transaction_date: str | None
    query_kind: str | None
    start_date: str | None
    end_date: str | None

    def __repr__(self) -> str:  # keep amounts/descriptions out of logs and tracebacks
        return f"Interpretation(intent={self.intent!r}, query_kind={self.query_kind!r})"


def parse_output(data: Any) -> Interpretation:
    if not isinstance(data, dict):
        raise InvalidOutput("output must be an object")
    expected = set(OUTPUT_SCHEMA["required"])
    if set(data) != expected:
        raise InvalidOutput("unexpected or missing fields")

    def text(name: str, allowed: tuple[str, ...] | None = None) -> str | None:
        value = data[name]
        if value is None:
            return None
        if not isinstance(value, str):
            raise InvalidOutput(f"{name} must be a string")
        if allowed is not None and value not in allowed:
            raise InvalidOutput(f"{name} has an invalid value")
        return value

    def integer(name: str) -> int | None:
        value = data[name]
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidOutput(f"{name} must be an integer")
        return value

    intent = text("intent", INTENTS)
    if intent is None:
        raise InvalidOutput("intent is required")
    return Interpretation(
        intent=intent,
        transaction_type=text("transaction_type", TRANSACTION_TYPES),
        amount=text("amount"),
        description=text("description"),
        category_id=integer("category_id"),
        subcategory_id=integer("subcategory_id"),
        transaction_date=text("transaction_date"),
        query_kind=text("query_kind", QUERY_KINDS),
        start_date=text("start_date"),
        end_date=text("end_date"),
    )
