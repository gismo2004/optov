"""What a datapoint may become when the link cannot, or must not, ask for it."""

import catalog_db
from test_names import GROUPS, _inputs, _profile

P300 = {
    "Virtual_READ",
    "Virtual_WRITE",
    "Remote_Procedure_Call",
    "EEPROM_READ",
    "EEPROM_WRITE",
}


def _placed(dp, reachable=P300):
    base = {
        "id": 10,
        "name": "X",
        "pretty_name": "X",
        "address": "0x0404",
        "entity_kind": catalog_db.KIND_READONLY,
        "fc_read": "Virtual_READ",
        "fc_write": "undefined",
        "byte_length": 2,
        "block_length": 2,
        "bit_length": 0,
        "conversion": "NoConversion",
        "parameter_type": "Integer",
        "tier": "Overview",
    }
    base.update(dp)
    inputs = _inputs(GROUPS, links={10: [1]}, datapoints=[base])
    inputs.reachable_fc = reachable
    inputs.by_address = {base["address"]: [base]}
    profile = _profile()
    catalog_db._place_datapoint(base, inputs, profile)
    return {k: v for k, v in profile.items() if isinstance(v, list) and v}


def test_a_code_the_link_does_not_implement_makes_no_entity():
    assert _placed({"fc_read": "KBUS_VIRTUAL_READ"}) == {}


def test_a_setting_without_a_write_code_is_only_a_reading():
    placed = _placed({"entity_kind": catalog_db.KIND_WRITABLE, "fc_write": "undefined"})
    assert list(placed) == ["sensors"]


def test_a_setting_with_a_write_code_the_link_lacks_is_only_a_reading():
    placed = _placed(
        {"entity_kind": catalog_db.KIND_WRITABLE, "fc_write": "KBUS_VIRTUAL_WRITE"}
    )
    assert list(placed) == ["sensors"]


def test_a_remote_procedure_without_a_parameter_is_never_polled():
    # Clearing the fault history is one of these; reading it would call it.
    procedure = {
        "fc_read": "Remote_Procedure_Call",
        "fc_write": "Remote_Procedure_Call",
        "entity_kind": catalog_db.KIND_WRITABLE,
        "byte_length": 0,
        "block_length": 0,
    }
    assert _placed(procedure) == {}


def test_a_remote_procedure_reading_carries_its_parameter_and_is_not_writable():
    placed = _placed(
        {
            "fc_read": "Remote_Procedure_Call",
            "fc_write": "Remote_Procedure_Call",
            "entity_kind": catalog_db.KIND_WRITABLE,
            "prefix_read": "0A",
            "block_length": 6,
        }
    )
    assert list(placed) == ["sensors"]
    assert placed["sensors"][0]["prefix_read"] == "0A"


def test_an_array_or_an_oversize_value_is_not_one_entity():
    assert _placed({"block_factor": 3, "byte_length": 12, "block_length": 12}) == {}
    assert _placed({"byte_length": 60, "block_length": 60}) == {}


def test_a_conversion_nothing_can_decode_makes_no_entity():
    assert _placed({"conversion": "Estrich", "byte_length": 1, "block_length": 1}) == {}


def test_the_service_counters_are_plain_numbers():
    from decode import decode_value

    placed = _placed({"conversion": "LastBurnerCheck", "byte_length": 4})
    assert list(placed) == ["sensors"]
    assert decode_value(bytes([0x10, 0x27, 0, 0]), "LastCheckInterval") == 10000


def test_a_multiplied_setting_takes_its_limits_in_the_shown_unit():
    placed = _placed(
        {
            "entity_kind": catalog_db.KIND_WRITABLE,
            "fc_write": "Virtual_WRITE",
            "conversion": "Mult5",
            "parameter_type": "Byte",
            "byte_length": 1,
            "block_length": 1,
        }
    )
    number = placed["numbers"][0]
    assert (number["min"], number["max"], number["step"]) == (0, 1275, 5)


def test_an_address_setting_is_shown_not_set():
    placed = _placed(
        {
            "entity_kind": catalog_db.KIND_WRITABLE,
            "fc_write": "Virtual_WRITE",
            "conversion": "IPAddress",
            "byte_length": 4,
            "block_length": 4,
        }
    )
    assert list(placed) == ["sensors"]
