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


def test_a_sixteen_byte_number_is_neither_a_number_nor_writable():
    placed = _placed(
        {
            "entity_kind": catalog_db.KIND_WRITABLE,
            "fc_write": "Virtual_WRITE",
            "parameter_type": "Byte",
            "byte_length": 16,
            "block_length": 16,
        }
    )
    assert list(placed) == ["sensors"]
    assert "state_class" not in placed["sensors"][0]


def test_a_datapoint_no_menu_lists_is_offered_disabled_and_filed_apart():
    placed = _placed({"tier": None})
    entry = placed["sensors"][0]
    assert entry["unlisted"] and not entry["enabled_by_default"]


def test_a_placeholder_without_an_address_is_not_offered():
    assert _placed({"tier": None, "address": "0x0000"}) == {}


def test_a_menu_that_is_not_a_known_page_is_still_left_out():
    assert _placed({"tier": "Functionscontroll"}) == {}


def test_the_heat_pump_programming_pages_are_settings():
    placed = _placed(
        {
            "tier": "ProgrammingWP",
            "entity_kind": catalog_db.KIND_WRITABLE,
            "fc_write": "Virtual_WRITE",
        }
    )
    assert placed["numbers"][0]["entity_category"] == "config"


def test_an_unlisted_namesake_does_not_rename_a_controller_entity():
    listed = {"name": "Pumpe", "address": "0x0100", "_group_labels": [], "base": True}
    unlisted = {
        "name": "Pumpe",
        "address": "0x0200",
        "_group_labels": [],
        "unlisted": True,
    }
    profile = {"sensors": [listed, unlisted]}
    catalog_db._disambiguate_names(profile)
    assert listed["name"] == "Pumpe"
    assert unlisted["name"] == "Pumpe · 0x0200"


def test_an_unlisted_copy_of_a_shown_value_is_dropped():
    shown = {"id": "a", "address": "0x600D", "bytes": 2, "block": 2}
    copy = {"id": "b", "address": "0x600D", "bytes": 2, "block": 2, "unlisted": True}
    other = {"id": "c", "address": "0x600E", "bytes": 2, "block": 2, "unlisted": True}
    profile = {"numbers": [shown, copy, other]}
    catalog_db._drop_unlisted_repeats(profile)
    assert [e["id"] for e in profile["numbers"]] == ["a", "c"]


def test_an_unlisted_value_inside_a_shown_one_is_dropped():
    # The older two-byte definition of a counter the controller entity reads with four bytes.
    shown = {"id": "a", "address": "0x1640", "bytes": 4, "block": 4}
    part = {"id": "b", "address": "0x1640", "bytes": 2, "block": 2, "unlisted": True}
    # Another field of the same block, beside the shown one: a value of its own.
    beside = {
        "id": "c",
        "address": "0x1640",
        "bytes": 1,
        "block": 8,
        "byte_position": 5,
        "unlisted": True,
    }
    profile = {"sensors": [shown, part, beside]}
    catalog_db._drop_unlisted_repeats(profile)
    assert [e["id"] for e in profile["sensors"]] == ["a", "c"]


def test_uncategorized_readings_are_sensors_and_settings_configuration():
    reading = _placed({"tier": None})["sensors"][0]
    setting = _placed(
        {
            "tier": None,
            "entity_kind": catalog_db.KIND_WRITABLE,
            "fc_write": "Virtual_WRITE",
        }
    )["numbers"][0]
    assert "entity_category" not in reading
    assert setting["entity_category"] == "config"
