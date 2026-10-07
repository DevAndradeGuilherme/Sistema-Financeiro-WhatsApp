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
    # v1.1 fields
    "follow_up": False,
    "compare_start_date": None,
    "compare_end_date": None,
    "limit": None,
    "reference": None,
    "updates": None,
    # v1.2 fields
    "scope": None,
    "target_member_name": None,
}


def test_schema_is_strict_compatible():
    assert OUTPUT_SCHEMA["additionalProperties"] is False
    assert set(OUTPUT_SCHEMA["required"]) == set(OUTPUT_SCHEMA["properties"])
    # No user/authorization fields. (V1.2 adds only the semantic ``scope`` enum and a free-text
    # ``target_member_name``, both resolved and authorized by Django.)
    forbidden = {
        "target_user",
        "role",
        "user_id",
        "target_user_id",
        "target_phone",
        "permission",
        "authorized",
        "is_admin",
    }
    assert not forbidden & set(OUTPUT_SCHEMA["properties"])
    assert OUTPUT_SCHEMA["properties"]["scope"]["enum"] == ["SELF", "FAMILY", "FAMILY_MEMBER", None]
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


REFERENCE = {
    "last": False,
    "transaction_type": "EXPENSE",
    "category_id": None,
    "subcategory_id": 1,
    "description": "gasolina",
    "amount": None,
    "transaction_date": None,
}
UPDATES = {
    "amount": "15.00",
    "description": None,
    "category_id": None,
    "subcategory_id": None,
    "transaction_date": None,
}


def test_nested_objects_are_strict():
    from apps.ai.contract import REFERENCE_SCHEMA, UPDATES_SCHEMA

    for schema in (REFERENCE_SCHEMA, UPDATES_SCHEMA):
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "transaction_id" not in schema["properties"]
        assert schema["type"] == ["object", "null"]


def test_parse_update_proposal():
    result = parse_output(
        {**VALID, "intent": "UPDATE_TRANSACTION", "reference": REFERENCE, "updates": UPDATES}
    )
    assert result.reference.subcategory_id == 1
    assert result.reference.last is False
    assert result.updates.amount == "15.00"
    assert not result.updates.is_empty()


@pytest.mark.parametrize(
    "reference, updates",
    [
        ({**REFERENCE, "last": "yes"}, None),
        ({**REFERENCE, "user_id": 7}, None),
        ({**REFERENCE, "category_id": "1"}, None),
        (None, {**UPDATES, "amount": 15.0}),
        (None, {**UPDATES, "transaction_type": "INCOME"}),  # type is never editable
        (None, {**UPDATES, "owner": "maria"}),
    ],
)
def test_invalid_nested_objects_rejected(reference, updates):
    with pytest.raises(InvalidOutput):
        parse_output({**VALID, "reference": reference, "updates": updates})


@pytest.mark.parametrize("value", [True, "x", 1])
def test_follow_up_must_be_boolean(value):
    if value is True:
        assert parse_output({**VALID, "follow_up": value}).follow_up is True
    else:
        with pytest.raises(InvalidOutput):
            parse_output({**VALID, "follow_up": value})
