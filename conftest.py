import http.client
import socket
from datetime import date

import pytest

from apps.accounts.services import create_user
from apps.categories.models import Category, CategoryKind, Subcategory
from apps.transactions.models import TransactionSource, TransactionType
from apps.transactions.services import create_transaction


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """No real HTTP during tests (Evolution/OpenAI must be mocked). DB uses libpq, unaffected."""

    def blocked(*args, **kwargs):
        raise NetworkBlockedError("Network access is disabled in tests.")

    monkeypatch.setattr(socket, "create_connection", blocked)
    real_getaddrinfo = socket.getaddrinfo

    def local_only_getaddrinfo(host, *args, **kwargs):
        # psycopg resolves the (local) test database host through Python's resolver.
        if host in {"localhost", "127.0.0.1", "::1"}:
            return real_getaddrinfo(host, *args, **kwargs)
        raise NetworkBlockedError("Network access is disabled in tests.")

    monkeypatch.setattr(socket, "getaddrinfo", local_only_getaddrinfo)
    monkeypatch.setattr(http.client.HTTPConnection, "connect", blocked)
    monkeypatch.setattr(http.client.HTTPSConnection, "connect", blocked)


@pytest.fixture
def make_user(db):
    counter = {"n": 0}

    def factory(**kwargs):
        counter["n"] += 1
        n = counter["n"]
        kwargs.setdefault("username", f"user{n}")
        kwargs.setdefault("phone_number", f"+55319999{n:05d}")
        return create_user(**kwargs)

    return factory


@pytest.fixture
def alice(make_user):
    return make_user(username="alice", phone_number="+5531999990001")


@pytest.fixture
def bob(make_user):
    return make_user(username="bob", phone_number="+5531999990002")


@pytest.fixture
def transport(db):
    return Category.objects.create(name="Transporte", slug="transporte", kind=CategoryKind.EXPENSE)


@pytest.fixture
def fuel(transport):
    return Subcategory.objects.create(category=transport, name="Combustível", slug="combustivel")


@pytest.fixture
def food(db):
    return Category.objects.create(
        name="Alimentação", slug="alimentacao", kind=CategoryKind.EXPENSE
    )


@pytest.fixture
def salary(db):
    return Category.objects.create(name="Salário", slug="salario", kind=CategoryKind.INCOME)


@pytest.fixture
def make_transaction():
    def factory(*, user, category, amount="10.00", type=TransactionType.EXPENSE, **kwargs):
        kwargs.setdefault("transaction_date", date(2026, 10, 1))
        kwargs.setdefault("source", TransactionSource.MANUAL)
        return create_transaction(user=user, type=type, amount=amount, category=category, **kwargs)

    return factory
