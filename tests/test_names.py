"""Datapoints that share a name, and how they are told apart.

The catalog names a relay's state, its switching counter and its running hours all the same,
because in the vendor's tool each sits on its own page. In Home Assistant they meet in one
entity picker, so the menu node that sets each apart is appended, and the kinds Home Assistant
has no device class for get an icon. All of it from catalog structure and catalog text.
"""

import catalog_db
from catalog_db import ICON_COUNTER, ICON_TWO_STATE, _ProfileInputs


def _inputs(groups=(), links=None, datapoints=(), enums=None):
    groups = [dict(g) for g in groups]
    return _ProfileInputs(
        model="M",
        device_name="Controller",
        circuits={},
        hidden_circuits=set(),
        groups=groups,
        group_ids_by_et=links or {},
        group_addrs_by_id={g["id"]: g["address"] for g in groups},
        groups_by_id={g["id"]: g for g in groups},
        datapoints=list(datapoints),
        enums_by_et=enums or {},
        hidden_event_type_ids=set(),
        hidden_group_ids=set(),
        active_tiers=set(),
        unreachable_fc=set(),
    )


GROUPS = [
    {"id": 1, "address": "M~Overview~WP", "name": "Wärmepumpe", "order_index": 1},
    {
        "id": 2,
        "address": "M~Statistic~SchaltzyklenWP",
        "name": "Schaltzyklen WP",
        "order_index": 2,
    },
    {
        "id": 3,
        "address": "M~Statistic~BetriebsstundenWP",
        "name": "Betriebsstd. WP",
        "order_index": 3,
    },
    {"id": 4, "address": "ecnsysEventTypeGroupHC~M", "name": "M", "order_index": 0},
    {
        "id": 5,
        "address": "M~DiagnosisDiagnosis1~Uebersicht",
        "name": "Übersicht",
        "order_index": 4,
    },
]


# -- which menu node names a datapoint ------------------------------------------------


def test_the_nodes_of_its_own_tier_come_first():
    inputs = _inputs(GROUPS)
    assert catalog_db._group_labels([5, 2], "Statistic", inputs) == [
        "Schaltzyklen WP",
        "Übersicht",
    ]


def test_the_catalogs_bookkeeping_nodes_are_not_labels():
    # Named after the controller model; nobody ever sees them as a page.
    inputs = _inputs(GROUPS)
    assert catalog_db._group_labels([4, 5], "DiagnosisDiagnosis2", inputs) == [
        "Übersicht"
    ]


# -- telling namesakes apart ----------------------------------------------------------


def _profile(*entries):
    return {
        "sensors": [dict(e) for e in entries],
        "binary_sensors": [],
        "numbers": [],
        "selects": [],
        "switches": [],
    }


def test_a_relays_state_counter_and_hours_become_three_names():
    profile = _profile(
        {
            "name": "Sekundärpumpe 1",
            "address": "0x0404",
            "base": True,
            "_group_labels": ["Wärmepumpe", "Übersicht"],
        },
        {
            "name": "Sekundärpumpe 1",
            "address": "0x0504",
            "base": False,
            "_group_labels": ["Schaltzyklen WP"],
        },
        {
            "name": "Sekundärpumpe 1",
            "address": "0x0584",
            "base": False,
            "_group_labels": ["Betriebsstd. WP"],
        },
    )
    catalog_db._disambiguate_names(profile)
    assert [e["name"] for e in profile["sensors"]] == [
        "Sekundärpumpe 1",  # the live reading keeps the plain name
        "Sekundärpumpe 1 · Schaltzyklen WP",
        "Sekundärpumpe 1 · Betriebsstd. WP",
    ]


def test_a_shared_node_is_not_used_to_tell_them_apart():
    # Both live readings, both on the overview page; only the hours are also filed elsewhere.
    profile = _profile(
        {
            "name": "E-Heizung Stufe 1",
            "address": "0x0488",
            "base": True,
            "_group_labels": ["Wärmepumpe"],
        },
        {
            "name": "E-Heizung Stufe 1",
            "address": "0x0588",
            "base": True,
            "_group_labels": ["Wärmepumpe", "Betriebsstd. WP"],
        },
    )
    catalog_db._disambiguate_names(profile)
    assert [e["name"] for e in profile["sensors"]] == [
        "E-Heizung Stufe 1",
        "E-Heizung Stufe 1 · Betriebsstd. WP",
    ]


def test_what_the_menu_cannot_separate_falls_back_to_the_address():
    profile = _profile(
        {
            "name": "Ventilstellung",
            "address": "0x0671",
            "base": False,
            "_group_labels": ["EEV"],
        },
        {
            "name": "Ventilstellung",
            "address": "0x0672",
            "base": False,
            "_group_labels": ["EEV"],
        },
    )
    catalog_db._disambiguate_names(profile)
    assert [e["name"] for e in profile["sensors"]] == [
        "Ventilstellung · 0x0671",
        "Ventilstellung · 0x0672",
    ]


def test_unique_names_are_left_alone_and_the_working_key_is_removed():
    profile = _profile(
        {
            "name": "Außentemperatur",
            "address": "0x0101",
            "base": True,
            "_group_labels": ["X"],
        },
    )
    catalog_db._disambiguate_names(profile)
    assert profile["sensors"] == [
        {"name": "Außentemperatur", "address": "0x0101", "base": True}
    ]


# -- icons for the kinds without a device class ----------------------------------------


def _place(dp, *, enums=None, links=None):
    base = {
        "id": 10,
        "name": "WPR_X",
        "pretty_name": "X",
        "address": "0x0404",
        "entity_kind": catalog_db.KIND_READONLY,
        "fc_read": "Virtual_READ",
        "byte_length": 2,
        "bit_length": 0,
        "conversion": "NoConversion",
        "parameter_type": "Integer",
        "tier": "Overview",
    }
    base.update(dp)
    inputs = _inputs(GROUPS, links=links or {10: [1]}, datapoints=[base], enums=enums)
    inputs.by_address = {base["address"]: [base]}
    profile = _profile()
    catalog_db._place_datapoint(base, inputs, profile)
    return profile["sensors"][0]


def test_a_two_state_reading_gets_the_toggle_icon():
    entry = _place({}, enums={10: {"0": "Aus", "1": "Ein"}})
    assert entry["icon"] == ICON_TWO_STATE


def test_a_reading_with_more_states_keeps_the_default():
    entry = _place(
        {}, enums={10: {"0": "OK", "1": "Kurzschluss", "2": "Unterbrechung"}}
    )
    assert "icon" not in entry


def test_a_unitless_count_on_the_statistics_page_gets_the_counter_icon():
    entry = _place({"tier": "Statistic", "address": "0x0504"}, links={10: [2]})
    assert entry["icon"] == ICON_COUNTER


def test_running_hours_are_left_to_their_device_class():
    entry = _place(
        {
            "tier": "Statistic",
            "address": "0x0584",
            "unit": "Stunden",
            "translated_unit": "Stunden",
            "conversion": "Sec2Hour",
        },
        links={10: [3]},
    )
    assert "icon" not in entry
    assert entry["device_class"] == "duration"
