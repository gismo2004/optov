"""The saved settings in the controller's file: what goes in, and that it reads back."""

import types

import custom_entities as rules
import yaml

SETTINGS = {
    "controller": "TC100",
    "software": "v02",
    "saved": "2026-11-10T21:00:00+01:00",
    "values": {"room": 22.0, "mode": 2, "lock": 0, "holiday": "2026-12-24"},
    "programmes": {
        "heating": {
            "mon": [["06:00", "22:00", 2]],
            "tue": [["06:00", "08:00", 2], ["16:00", "24:00", 1]],
            "wed": [],
        }
    },
}
NOTES = {"room": "Room temperature", "mode": "Mode: Automatic"}


def _profile(**platforms):
    lists = {"numbers": [], "selects": [], "switches": [], "dates": [], **platforms}
    return types.SimpleNamespace(**lists)


ROOM = {"id": "room"}
NAMELESS = {"id": "nameless", "unlisted": True}
LOCK = {"id": "lock", "entity_category": "config"}
PARTY = {"id": "party", "entity_category": None}
HOLIDAY = {"id": "holiday"}


def test_everything_writable_is_saved():
    profile = _profile(
        numbers=[ROOM, NAMELESS],
        selects=[{"id": "mode"}],
        switches=[LOCK, PARTY],
        dates=[HOLIDAY],
    )

    found = rules.settings_items(profile)

    assert list(found) == ["room", "nameless", "mode", "lock", "party", "holiday"]
    assert found["party"] == ("switches", PARTY)


def test_only_settings_are_ticked_for_setting_back():
    def ticked(platform, item):
        return rules.restored_by_default(platform, item)

    assert ticked("numbers", ROOM)
    assert ticked("selects", {"id": "mode"})
    assert ticked("switches", LOCK)
    # Party mode is a state of the moment, a date a holiday, and nothing says what a value
    # no menu lists is for: saved and shown, but left for the user to tick.
    assert not ticked("switches", PARTY)
    assert not ticked("dates", HOLIDAY)
    assert not ticked("numbers", NAMELESS)


def test_the_section_reads_back_as_it_was_written():
    text = rules.render_file("Test (node_port)", [], settings=SETTINGS, notes=NOTES)

    assert rules.parse_settings(yaml.safe_load(text)) == SETTINGS
    # A switching time stays text: unquoted, YAML would read 06:00 as a number.
    assert '["06:00", "22:00", 2]' in text
    assert "    mode: 2  # Mode: Automatic" in text
    # A date stays text as well, not a YAML date.
    assert '    holiday: "2026-12-24"' in text


def test_the_settings_do_not_disturb_the_custom_entities():
    text = rules.render_file("Test (node_port)", [], settings=SETTINGS, notes=NOTES)

    sections, problems, readable = rules.parse_file(yaml.safe_load(text))

    assert (problems, readable) == ([], True)
    assert sections == {"onoff": [], "climate": [], "water_heater": []}


def test_a_file_without_the_section_has_no_saved_settings():
    assert rules.parse_settings(yaml.safe_load(rules.render_file("T", []))) is None
    assert rules.parse_settings(None) is None
    assert rules.parse_settings({"settings": {"values": "nonsense"}}) is None


def test_saving_again_replaces_the_section_and_nothing_else():
    by_hand = (
        "# my own note\n"
        "onoff:\n"
        "  - value: sensor.something_this_controller_lacks\n"
        "    on_value: 1\n"
        "\n"
        "settings:\n"
        '  controller: "OLD"\n'
        "  values:\n"
        "    room: 18.0\n"
        "  programmes: {}\n"
        "climate: []\n"
    )

    text = rules.replace_settings(by_hand, rules.render_settings(SETTINGS, NOTES))

    assert rules.parse_settings(yaml.safe_load(text)) == SETTINGS
    assert text.count("settings:") == 1
    # The rest is the owner's, an entry the integration cannot use among it.
    for line in (
        "# my own note",
        "sensor.something_this_controller_lacks",
        "climate: []",
    ):
        assert line in text


def test_only_values_that_really_differ_are_listed():
    saved = {"a": 20, "b": 2, "c": 7.5, "gone": 1}
    now = {"a": 20.0, "b": 1, "c": 7.0, "new": 3}

    assert rules.changed_settings(saved, now) == [("b", 2, 1), ("c", 7.5, 7.0)]
