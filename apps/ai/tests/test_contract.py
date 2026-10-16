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
    # v1.5.1
    "for_another_person": False,
    # v1.5.2
    "correction": False,
    # v1.5 fields
    "months": None,
    "trend_metric": None,
    "trend_direction": None,
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
        {"for_another_person": None},
        {"for_another_person": "true"},
    ],
)
def test_invalid_values_rejected(change):
    with pytest.raises(InvalidOutput):
        parse_output({**VALID, **change})


def test_for_another_person_is_a_plain_flag():
    """v1.5.1: a boolean hint Django can only use to refuse, never an owner/user field."""
    assert OUTPUT_SCHEMA["properties"]["for_another_person"] == {"type": "boolean"}
    assert parse_output(VALID).for_another_person is False
    assert parse_output({**VALID, "for_another_person": True}).for_another_person is True


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
    "start_date": None,  # v1.5.2
    "end_date": None,  # v1.5.2
    "all": False,  # v1.5.2
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


def test_v152_reference_period_all_and_correction():
    """v1.5.2: UNDO may carry a reference (period, "all"); correction is a plain flag."""
    from apps.ai.contract import REFERENCE_SCHEMA

    assert REFERENCE_SCHEMA["properties"]["all"] == {"type": "boolean"}
    assert OUTPUT_SCHEMA["properties"]["correction"] == {"type": "boolean"}
    reference = {**REFERENCE, "all": True, "start_date": "2026-10-01", "end_date": "2026-10-31"}
    result = parse_output(
        {**VALID, "intent": "UNDO_TRANSACTION", "reference": reference, "correction": True}
    )
    assert result.reference.all is True
    assert (result.reference.start_date, result.reference.end_date) == ("2026-10-01", "2026-10-31")
    assert result.correction is True
    for bad in ({**REFERENCE, "all": None}, {**REFERENCE, "all": "true"}):
        with pytest.raises(InvalidOutput):
            parse_output({**VALID, "reference": bad})
    with pytest.raises(InvalidOutput):
        parse_output({**VALID, "correction": None})
