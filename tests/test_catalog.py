"""The catalog queries against a small catalog built by hand (sample_catalog.py)."""

import catalog_db
import pytest
import sample_catalog as sample
from sample_catalog import DEVICE

P300 = {"Virtual_READ", "Virtual_WRITE"}


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    return sample.build(tmp_path_factory.mktemp("catalog") / "sample.db")


def _profile(db, culture="en", probed=None):
    """The entity set with both circuit flags set unless the test says otherwise."""
    values = {sample.CIRCUIT2: 1, sample.MIXER: 1} if probed is None else probed
    return catalog_db.generate_profile(DEVICE, db, culture, values, reachable_fc=P300)


def _ids(profile):
    return {
        item["id"]
        for platform in catalog_db.ENTITY_DOMAINS
        for item in profile[platform]
    }


def _named(profile, platform, name):
    return next(item for item in profile[platform] if item["name"] == name)


# --- The file -----------------------------------------------------------------------------


def test_the_catalog_says_which_structure_and_languages_it_has(db):
    assert catalog_db.check_catalog(db) == sample.SCHEMA_VERSION
    assert catalog_db.get_available_languages(db) == ["de", "en"]
    info = catalog_db.catalog_info(db)
    assert (info["controllers"], info["languages"]) == (1, ["de", "en"])


def test_a_catalog_newer_than_the_code_is_refused(tmp_path):
    newer = sample.build(tmp_path / "newer.db", catalog_db.CATALOG_SCHEMA_VERSION + 1)

    with pytest.raises(catalog_db.CatalogSchemaError):
        catalog_db.check_catalog(newer)


# --- Identification -----------------------------------------------------------------------


def test_a_controller_is_found_by_its_system_id(db):
    found = catalog_db.get_device_by_system_id(sample.SYSTEM_ID, db, 1, 2)

    assert found == {"id": DEVICE, "model": "TC100"}


def test_a_system_id_the_catalog_lacks_is_an_unknown_controller(db):
    with pytest.raises(catalog_db.UnknownControllerError):
        catalog_db.get_device_by_system_id(0x2099, db, 1, 2)


# --- Rule inputs and the startup probe -----------------------------------------------------


def test_a_foreign_rule_input_is_matched_by_its_label_in_the_language_asked_for(db):
    german = catalog_db.get_condition_aliases(DEVICE, db, "de")
    english = catalog_db.get_condition_aliases(DEVICE, db, "en")

    assert german == {sample.FOREIGN_CIRCUIT2: sample.CIRCUIT2}
    # The mixer flags share their name in English only.
    assert english == {**german, sample.FOREIGN_MIXER: sample.MIXER}


def test_a_sensor_health_input_is_never_matched_to_the_value_it_belongs_to(db):
    # Both carry the label "Flow temperature"; comparing the temperature with a health code
    # would be meaningless.
    for culture in ("de", "en"):
        aliases = catalog_db.get_condition_aliases(DEVICE, db, culture)
        assert sample.FOREIGN_FLOW_HEALTH not in aliases


def test_the_probe_reads_what_the_rules_of_that_language_will_ask_for(db):
    def probed(culture):
        return {t["id"] for t in catalog_db.get_probe_targets(DEVICE, db, culture)}

    assert probed("de") == {sample.CIRCUIT2}
    assert probed("en") == {sample.CIRCUIT2, sample.MIXER}


# --- The rules ----------------------------------------------------------------------------


def test_a_rule_hides_its_target_through_an_own_and_a_matched_input(db):
    absent = catalog_db.evaluate_rules(DEVICE, {sample.CIRCUIT2: 0}, db, "de")
    fitted = catalog_db.evaluate_rules(DEVICE, {sample.CIRCUIT2: 1}, db, "de")

    assert absent == ({sample.ROOM2, sample.SLOPE2}, set())
    assert fitted == (set(), set())


def test_a_rule_whose_input_was_never_read_hides_nothing(db):
    assert catalog_db.evaluate_rules(DEVICE, {}, db, "en") == (set(), set())


def test_the_same_controller_answers_the_same_in_either_language(db):
    values = {sample.CIRCUIT2: 0, sample.MIXER: 0}

    german = catalog_db.evaluate_rules(DEVICE, values, db, "de")
    english = catalog_db.evaluate_rules(DEVICE, values, db, "en")

    # English resolves one input more, and the probe has read it for that language.
    assert german[0] == {sample.ROOM2, sample.SLOPE2}
    assert english[0] == german[0] | {sample.RUNTIME}


# --- The entity set -----------------------------------------------------------------------


def test_each_datapoint_lands_on_the_platform_its_shape_says(db):
    profile = _profile(db)

    assert {i["name"] for i in profile["switches"]} == {"Party mode"}
    assert {i["name"] for i in profile["selects"]} == {"Operating mode"}
    assert {i["name"] for i in profile["numbers"]} == {
        "Room temperature setpoint",
        "Room temperature setpoint 2",
        "Slope 2",
        "Mixer runtime",
    }
    assert {"Outside temperature", "Flow temperature"} <= {
        i["name"] for i in profile["sensors"]
    }


def test_what_the_rules_hide_is_not_in_the_entity_set(db):
    with_circuit = _ids(_profile(db))
    without = _ids(_profile(db, probed={sample.CIRCUIT2: 0, sample.MIXER: 1}))

    assert len(with_circuit - without) == 2
    assert not without - with_circuit


def test_names_units_and_options_come_in_the_language_asked_for(db):
    german, english = _profile(db, "de"), _profile(db, "en")

    assert german["device_name"] == "Testregelung 100"
    assert english["device_name"] == "Test control 100"
    assert german["circuits"]["HC1"] == "Heizkreis 1"
    assert list(_named(german, "selects", "Betriebsart")["options"].values()) == [
        "Aus",
        "Heizen",
        "Automatik",
    ]
    assert list(_named(english, "selects", "Operating mode")["options"].values()) == [
        "Off",
        "Heating",
        "Automatic",
    ]
    # The entity set itself does not depend on the language.
    assert _ids(german) == _ids(english)


def test_a_setting_carries_its_limits_step_description_and_default(db):
    room = _named(_profile(db), "numbers", "Room temperature setpoint")

    assert (room["min"], room["max"], room["step"]) == (10, 30, 0.1)
    assert room["setpoint_step"] == 1
    assert room["description"] == "The wanted room temperature."
    assert room["catalog_default"] == 20
    assert room["circuit"] == "HC1"
    assert _named(_profile(db), "selects", "Operating mode")["catalog_default"] == 2
    assert _named(_profile(db), "switches", "Party mode")["catalog_default"] == 0


def test_what_the_controller_shows_on_its_own_menus_is_enabled_by_default(db):
    profile = _profile(db)

    assert _named(profile, "sensors", "Outside temperature")["enabled_by_default"]
    assert _named(profile, "numbers", "Room temperature setpoint")["enabled_by_default"]
