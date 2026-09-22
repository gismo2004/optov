"""The catalog's own explanation of a datapoint, on its way to a state attribute.

A coding parameter's name says what it is called. Only the description says what changing
it will do, and the catalog has carried that text all along -- it was read by the datapoint
query and then dropped. These tests pin the tidying, because the text arrives with the
vendor's layout markers in it and those mean nothing outside its service tool.
"""

import catalog_db


def test_the_vendor_layout_markers_are_removed():
    dp = {"description": "Erst wenn der Anstieg##ecnnewline##unter dem Wert liegt"}
    assert catalog_db._description(dp) == "Erst wenn der Anstieg unter dem Wert liegt"


def test_tabs_are_removed_too():
    dp = {"description": "kein Manager##ecntab##Geraet ist kein Fehlermanager"}
    assert catalog_db._description(dp) == "kein Manager Geraet ist kein Fehlermanager"


def test_runs_of_whitespace_collapse():
    # The catalog wraps its text, so a marker often sits next to the spaces of the wrap.
    dp = {
        "description": "Maximale  Vorlauftemperatur,   welche\n\tzur Verfuegung steht"
    }
    assert (
        catalog_db._description(dp)
        == "Maximale Vorlauftemperatur, welche zur Verfuegung steht"
    )


def test_a_datapoint_without_a_description_gets_none():
    # Roughly one datapoint in twenty has no text; the attribute is then left off entirely
    # rather than shown empty.
    assert catalog_db._description({}) is None
    assert catalog_db._description({"description": None}) is None
    assert catalog_db._description({"description": ""}) is None


def test_text_that_is_only_markers_counts_as_absent():
    assert (
        catalog_db._description({"description": "##ecnnewline##  ##ecntab##"}) is None
    )


def test_ordinary_text_is_passed_through_unchanged():
    dp = {"description": "Bivalenztemperatur Elektro-Heizung"}
    assert catalog_db._description(dp) == "Bivalenztemperatur Elektro-Heizung"
