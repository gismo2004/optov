"""Custom entities: which values qualify for an on/off entity, the preselections, the file."""

from types import SimpleNamespace

import yaml
from custom_entities import (
    check_device_class,
    default_on_value,
    find_item,
    is_on,
    off_value,
    parse_file,
    render_file,
    two_state_items,
)


def _profile():
    return SimpleNamespace(
        sensors=[
            {"id": "relay", "options": {0: "Aus", 1: "Ein"}},
            {"id": "mode", "options": {0: "a", 1: "b", 2: "c"}},
            {"id": "temp"},
        ],
        selects=[
            {"id": "party", "options": {0: "OFF", 1: "ON"}},
            {"id": "inverted", "options": {0: "aktiv", 1: "inaktiv"}},
            {"id": "odd", "options": {32767: "Fehler", 32768: "Nicht vorhanden"}},
        ],
    )


def test_only_two_state_sensors_and_selects_qualify():
    found = [(p, i["id"]) for p, i in two_state_items(_profile())]
    assert found == [
        ("sensors", "relay"),
        ("selects", "party"),
        ("selects", "inverted"),
        ("selects", "odd"),
    ]
    platform, item = find_item(_profile(), "party")
    assert platform == "selects" and item["id"] == "party"
    assert find_item(_profile(), "mode") is None


def test_preselections():
    profile = _profile()
    assert default_on_value(find_item(profile, "relay")[1]) == 1
    assert default_on_value(find_item(profile, "odd")[1]) == 32768
    assert check_device_class("sensors", "running") == "running"
    assert check_device_class("sensors", "outlet") == "none"
    assert check_device_class("selects", None) == "switch"


def test_state_mapping_follows_the_chosen_value():
    inverted = find_item(_profile(), "inverted")[1]
    # The user chose 0 ("aktiv") as "on": the pair puts the active state first.
    assert is_on("aktiv", inverted, 0) is True
    assert is_on("inaktiv", inverted, 0) is False
    assert is_on(None, inverted, 0) is None
    assert off_value(find_item(_profile(), "odd")[1], 32768) == 32767


def test_parse_file_keeps_valid_entries_and_names_the_rest():
    raw = {
        "onoff": [
            {
                "value": "sensor.a",
                "on_value": 1,
                "device_class": "running",
                "name": " P ",
            },
            {"value": "sensor.b", "on_value": "1"},
            {"on_value": 1},
            "text",
            {"value": "sensor.a", "on_value": 0},
        ],
        "heating": [],
    }
    sections, problems, readable = parse_file(raw)
    assert readable
    assert sections["onoff"] == [
        {"value": "sensor.a", "on_value": 1, "device_class": "running", "name": "P"}
    ]
    assert problems == [
        "onoff entry 2 (sensor.b): 'on_value' must be a whole number",
        "onoff entry 3: 'value' is missing",
        "onoff entry 4: not a mapping",
        "onoff entry 5 (sensor.a): listed twice, the first one is used",
        "unknown section 'heating', ignored",
    ]


def test_an_unreadable_file_is_not_an_empty_one():
    assert parse_file(None) == (
        {"onoff": [], "climate": [], "water_heater": []},
        [],
        True,
    )
    sections, problems, readable = parse_file([{"value": "x", "on_value": 1}])
    assert (
        not readable
        and sections == {"onoff": [], "climate": [], "water_heater": []}
        and problems
    )
    _, problems, readable = parse_file({"onoff": "x"})
    assert readable and problems == ["onoff: must be a list of entries"]


def test_rendered_file_reads_back_as_the_same_entries():
    text = render_file(
        "Test (node_port)",
        [
            {
                "value": "sensor.pump",
                "on_value": 1,
                "device_class": "running",
                "name": 'Pumpe "A" läuft',
                "on_label": "Ein",
                "other": (0, "Aus"),
            }
        ],
    )
    assert "# Ein (other: 0 = Aus)" in text
    sections, problems, readable = parse_file(yaml.safe_load(text))
    assert readable and problems == []
    assert sections["onoff"] == [
        {
            "value": "sensor.pump",
            "on_value": 1,
            "device_class": "running",
            "name": 'Pumpe "A" läuft',
        }
    ]
    assert parse_file(yaml.safe_load(render_file("Empty", []))) == (
        {"onoff": [], "climate": [], "water_heater": []},
        [],
        True,
    )


def test_climate_and_water_heater_entries():
    from custom_entities import is_custom_unique_id, shared_modes, write_values

    raw = yaml.safe_load(
        """
climate:
  - name: Heizkreis 1
    target: number.setpoint
    mode: select.mode
    modes: {0: off, 1: off, 2: auto, 5: heat}
    write: {off: 1}
    current: sensor.room
    heating: binary_sensor.compressor
  - target: number.other
    modes: {0: heat}
  - target: number.third
    mode: select.mode
    modes: {0: boil}
water_heater:
  - target: number.dhw
    current: sensor.tank
"""
    )
    sections, problems, readable = parse_file(raw)
    assert readable
    assert problems == [
        "climate entry 2 (number.other): a thermostat needs 'mode'",
        "climate entry 3 (number.third): mode 'boil' is not one of "
        "off, heat, cool, heat_cool, auto",
    ]
    (climate,) = sections["climate"]
    # An unquoted off is YAML's false; in a mode position it can only mean "off".
    assert climate["modes"] == {0: "off", 1: "off", 2: "auto", 5: "heat"}
    assert climate["write"] == {"off": 1, "auto": 2, "heat": 5}
    assert climate["heating"] == "binary_sensor.compressor"
    (water,) = sections["water_heater"]
    assert (
        water["mode"] is None
        and water["modes"] == {}
        and water["current"] == "sensor.tank"
    )
    assert shared_modes(climate["modes"]) == {"off": [0, 1]}
    assert write_values({0: "off", 1: "off"}, {}) == {"off": 0}
    assert is_custom_unique_id("node_port", "node_port_climate_number_setpoint")
    assert is_custom_unique_id("node_port", "node_port_item_onoff")
    assert not is_custom_unique_id("node_port", "node_port_item")
    assert not is_custom_unique_id("other", "node_port_item_onoff")


def test_write_must_name_a_mapped_value():
    _, problems, _ = parse_file(
        {
            "climate": [
                {
                    "target": "number.t",
                    "mode": "select.m",
                    "modes": {0: "off"},
                    "write": {"heat": 0},
                }
            ]
        }
    )
    assert problems == [
        "climate entry 1 (number.t): 'write' heat: 0 is not mapped to heat"
    ]


def test_all_sections_render_and_read_back():
    climate = {
        "name": "Heizkreis 1",
        "target": "number.setpoint",
        "current": "sensor.room",
        "heating": None,
        "mode": "select.mode",
        "modes": {0: "off", 1: "off", 2: "auto"},
        "write": {"off": 1, "auto": 2},
        "labels": {0: "Abschaltbetrieb", 1: "Nur WW", 2: "Heizen"},
    }
    water = {
        "name": "WW",
        "target": "number.dhw",
        "current": "sensor.tank",
        "mode": None,
        "modes": {},
        "write": {},
    }
    text = render_file("Test", [], [climate], [water])
    assert "# Nur WW" in text
    sections, problems, _ = parse_file(yaml.safe_load(text))
    assert problems == []
    assert sections["climate"][0]["modes"] == {0: "off", 1: "off", 2: "auto"}
    assert sections["climate"][0]["write"] == {"off": 1, "auto": 2}
    assert sections["water_heater"][0]["target"] == "number.dhw"


def test_presets_are_checked_and_read_back():
    base = {"target": "number.t", "mode": "select.m", "modes": {0: "heat"}}
    sections, problems, _ = parse_file(
        {
            "climate": [
                {**base, "presets": {"boost": "select.party", "eco": "switch.eco"}},
                {**base, "target": "number.u", "presets": {"turbo": "switch.x"}},
                {**base, "target": "number.v", "presets": {"eco": "sensor.x"}},
            ]
        }
    )
    assert problems == [
        "climate entry 2 (number.u): preset 'turbo' is not one of "
        "eco, comfort, away, boost, home, sleep, activity",
        "climate entry 3 (number.v): preset eco: must be a switch or a select",
    ]
    (climate,) = sections["climate"]
    assert climate["presets"] == {"boost": "select.party", "eco": "switch.eco"}
    text = render_file("Test", [], [climate])
    # Written in Home Assistant's preset order, whatever order they were given in.
    assert text.index("eco: switch.eco") < text.index("boost: select.party")
    again, problems, _ = parse_file(yaml.safe_load(text))
    assert problems == [] and again["climate"][0]["presets"] == climate["presets"]
