"""Structured contract between the application and the model (docs/AI_CONTRACT.md).

Plain dataclasses, no domain imports. The model output is an untrusted *proposal*: this module
only checks its shape; Django (apps.whatsapp.actions) validates meaning and authorizes.
There are no user ids, phones or permission fields. ``scope``/``target_member_name`` only
describe whose money the message talks about; Django authorizes (apps.transactions.scopes).
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Any

CONTRACT_VERSION = "v1.2"

INTENTS = (
    "CREATE_TRANSACTION",
    "QUERY_TRANSACTIONS",
    "UPDATE_TRANSACTION",
    "UNDO_TRANSACTION",
    "HELP",
    "UNKNOWN",
)
TRANSACTION_TYPES = ("EXPENSE", "INCOME")
QUERY_KINDS = ("TOTAL", "BALANCE", "TOP", "BREAKDOWN", "LIST", "COMPARE", "RANKING")
# Semantic hint only ("whose money the message talks about"). Never an authorization.
SCOPES = ("SELF", "FAMILY", "FAMILY_MEMBER")


def _nullable(json_type: str, **extra: Any) -> dict:
    return {"type": [json_type, "null"], **extra}


def _object(properties: dict, nullable: bool = True) -> dict:
    return {
        "type": ["object", "null"] if nullable else "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_TYPE = _nullable("string", enum=[*TRANSACTION_TYPES, None])

REFERENCE_SCHEMA = _object(
    {
        "last": {"type": "boolean"},
        "transaction_type": _TYPE,
        "category_id": _nullable("integer"),
        "subcategory_id": _nullable("integer"),
        "description": _nullable("string"),
        "amount": _nullable("string"),
        "transaction_date": _nullable("string"),
    }
)

UPDATES_SCHEMA = _object(
    {
        "amount": _nullable("string"),
        "description": _nullable("string"),
        "category_id": _nullable("integer"),
        "subcategory_id": _nullable("integer"),
        "transaction_date": _nullable("string"),
    }
)

OUTPUT_SCHEMA: dict = _object(
    {
        "intent": {"type": "string", "enum": list(INTENTS)},
        "follow_up": {"type": "boolean"},
        "scope": _nullable("string", enum=[*SCOPES, None]),
        "target_member_name": _nullable("string"),
        "transaction_type": _TYPE,
        "amount": _nullable("string"),
        "description": _nullable("string"),
        "category_id": _nullable("integer"),
        "subcategory_id": _nullable("integer"),
        "transaction_date": _nullable("string"),
        "query_kind": _nullable("string", enum=[*QUERY_KINDS, None]),
        "start_date": _nullable("string"),
        "end_date": _nullable("string"),
        "compare_start_date": _nullable("string"),
        "compare_end_date": _nullable("string"),
        "limit": _nullable("integer"),
        "reference": REFERENCE_SCHEMA,
        "updates": UPDATES_SCHEMA,
    },
    nullable=False,
)


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
    """Everything the model receives besides the message text. No user data.

    ``previous_query``: the sender's last query parameters (ids/dates only), when recent,
    so follow-ups like "e mês passado?" can be resolved.
    """

    reference_date: date
    timezone: str
    catalog: tuple[CatalogCategory, ...] = field(default_factory=tuple)
    previous_query: dict | None = None


@dataclass(frozen=True)
class TransactionReference:
    """How the user identifies an existing transaction. Resolved by Django in the database."""

    last: bool = False
    transaction_type: str | None = None
    category_id: int | None = None
    subcategory_id: int | None = None
    description: str | None = None
    amount: str | None = None
    transaction_date: str | None = None


@dataclass(frozen=True)
class TransactionUpdates:
    amount: str | None = None
    description: str | None = None
    category_id: int | None = None
    subcategory_id: int | None = None
    transaction_date: str | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in self.__dataclass_fields__)


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
    follow_up: bool = False
    compare_start_date: str | None = None
    compare_end_date: str | None = None
    limit: int | None = None
    reference: TransactionReference | None = None
    updates: TransactionUpdates | None = None
    scope: str | None = None
    target_member_name: str | None = None

    def __repr__(self) -> str:  # keep amounts/descriptions out of logs and tracebacks
        return f"Interpretation(intent={self.intent!r}, query_kind={self.query_kind!r})"


class _Reader:
    def __init__(self, data: Any, schema: dict, where: str):
        if not isinstance(data, dict):
            raise InvalidOutput(f"{where} must be an object")
        if set(data) != set(schema["required"]):
            raise InvalidOutput(f"{where}: unexpected or missing fields")
        self.data = data
        self.where = where

    def text(self, name: str, allowed: tuple[str, ...] | None = None) -> str | None:
        value = self.data[name]
        if value is None:
            return None
        if not isinstance(value, str):
            raise InvalidOutput(f"{self.where}.{name} must be a string")
        if allowed is not None and value not in allowed:
            raise InvalidOutput(f"{self.where}.{name} has an invalid value")
        return value

    def integer(self, name: str) -> int | None:
        value = self.data[name]
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidOutput(f"{self.where}.{name} must be an integer")
        return value

    def boolean(self, name: str) -> bool:
        value = self.data[name]
        if not isinstance(value, bool):
            raise InvalidOutput(f"{self.where}.{name} must be a boolean")
        return value


def _reference(data: Any) -> TransactionReference | None:
    if data is None:
        return None
    r = _Reader(data, REFERENCE_SCHEMA, "reference")
    return TransactionReference(
        last=r.boolean("last"),
        transaction_type=r.text("transaction_type", TRANSACTION_TYPES),
        category_id=r.integer("category_id"),
        subcategory_id=r.integer("subcategory_id"),
        description=r.text("description"),
        amount=r.text("amount"),
        transaction_date=r.text("transaction_date"),
    )


def _updates(data: Any) -> TransactionUpdates | None:
    if data is None:
        return None
    r = _Reader(data, UPDATES_SCHEMA, "updates")
    return TransactionUpdates(
        amount=r.text("amount"),
        description=r.text("description"),
        category_id=r.integer("category_id"),
        subcategory_id=r.integer("subcategory_id"),
        transaction_date=r.text("transaction_date"),
    )


def parse_output(data: Any) -> Interpretation:
    r = _Reader(data, OUTPUT_SCHEMA, "output")
    intent = r.text("intent", INTENTS)
    if intent is None:
        raise InvalidOutput("intent is required")
    return Interpretation(
        intent=intent,
        follow_up=r.boolean("follow_up"),
        scope=r.text("scope", SCOPES),
        target_member_name=r.text("target_member_name"),
        transaction_type=r.text("transaction_type", TRANSACTION_TYPES),
        amount=r.text("amount"),
        description=r.text("description"),
        category_id=r.integer("category_id"),
        subcategory_id=r.integer("subcategory_id"),
        transaction_date=r.text("transaction_date"),
        query_kind=r.text("query_kind", QUERY_KINDS),
        start_date=r.text("start_date"),
        end_date=r.text("end_date"),
        compare_start_date=r.text("compare_start_date"),
        compare_end_date=r.text("compare_end_date"),
        limit=r.integer("limit"),
        reference=_reference(data["reference"]),
        updates=_updates(data["updates"]),
    )
