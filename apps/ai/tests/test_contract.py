import pytest

from apps.ai.contract import OUTPUT_SCHEMA, InvalidOutput, parse_output

VALID = {
    "intent": "CREATE_TRANSACTION",
    "transaction_type": "EXPENSE",
    "amount": "10.00",
    "description": "gasolina",
    "category_id": 2,
    "subcategory_id": 1,
    "transaction_date": None,
    "query_kind": None,
    "start_date": None,
    "end_date": None,
}


def test_schema_is_strict_compatible():
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"])
    # No user/authorization fields in the V1.0 contract.
    assert not {"target_user", "scope", "role", "user_id"} & set(OUTPUT_SCHEMA["properties"])
    # No numeric money: amount is a string.
    assert OUTPUT_SCHEMA["properties"]["amount"]["type"] == ["string", "null"]


def test_parse_valid_output():
    result = parse_output(VALID)
    assert result.intent == "CREATE_TRANSACTION"
    assert result.amount == "10.00"
    assert result.category_id == 2


@pytest.mark.parametrize(
    "change",
    [
        {"intent": "DELETE_EVERYTHING"},
        {"intent": None},
        {"transaction_type": "TRANSFER"},
        {"amount": 10.5},
        {"category_id": "2"},
        {"category_id": True},
        {"subcategory_id": 1.5},
        {"query_kind": "AVERAGE"},
        {"description": 123},
    ],
)
def test_invalid_values_rejected(change):
    with pytest.raises(InvalidOutput):
        parse_output({**VALID, **change})


def test_extra_or_missing_fields_rejected():
    with pytest.raises(InvalidOutput):
        parse_output({**VALID, "target_user": "Maria"})
    missing = dict(VALID)
    del missing["amount"]
    with pytest.raises(InvalidOutput):
        parse_output(missing)
    with pytest.raises(InvalidOutput):
        parse_output(["not", "an", "object"])


def test_repr_hides_financial_content():
    text = repr(parse_output(VALID))
    assert "10.00" not in text
    assert "gasolina" not in text
