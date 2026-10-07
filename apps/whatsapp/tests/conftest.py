"""Fixtures shared by the WhatsApp pipeline tests."""

from unittest import mock

import pytest

from apps.ai.service import InterpretationResult
from apps.categories.models import Category, Subcategory
from apps.categories.seed import seed_categories


@pytest.fixture
def catalog(db):
    seed_categories()
    return {
        "transporte": Category.objects.get(slug="transporte"),
        "combustivel": Subcategory.objects.get(slug="combustivel"),
        "uber": Subcategory.objects.get(slug="uber"),
        "alimentacao": Category.objects.get(slug="alimentacao"),
        "salario": Category.objects.get(slug="salario"),
        "outros_gastos": Category.objects.get(slug="outros-gastos"),
        "outras_receitas": Category.objects.get(slug="outras-receitas"),
    }


@pytest.fixture
def alice(make_user):
    return make_user(username="alice", phone_number="+5531999990001")


@pytest.fixture
def maria(make_user):
    return make_user(username="maria", phone_number="+5521988887777", first_name="Maria")


@pytest.fixture
def ai():
    with mock.patch("apps.whatsapp.pipeline.interpret_message") as interpret:
        interpret.set = lambda interpretation: setattr(
            interpret,
            "return_value",
            InterpretationResult(interpretation, model="test-model", duration_ms=5),
        )
        yield interpret


@pytest.fixture
def evolution():
    with mock.patch("apps.whatsapp.pipeline.EvolutionClient") as cls:
        yield cls.return_value.send_text
