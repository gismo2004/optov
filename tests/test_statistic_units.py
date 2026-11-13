"""Statistics recorded under a unit's old word are relabelled, and only those.

statistic_units.py imports Home Assistant, which the test run does not have, so the recorder is
a stand-in that keeps what it is asked to change.
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

PACKAGE = "optov_statistic_units_under_test"
SOURCE = Path(__file__).resolve().parent.parent / "custom_components" / "optov"

SENSORS = [
    {"id": "run_time", "unit": "s", "unit_text": "Seconds"},
    {"id": "pressure", "unit": "bar", "unit_text": "Bar (absolute)"},
    {"id": "speed", "unit": "U/min"},
    {"id": "not_registered", "unit": "h", "unit_text": "Hours"},
]
# What the recorder holds: one series still under the old word, one already relabelled.
RECORDED = [
    {"statistic_id": "sensor.run_time", "statistics_unit_of_measurement": "Seconds"},
    {"statistic_id": "sensor.pressure", "statistics_unit_of_measurement": "bar"},
]


def _run(recorder_loaded=True):
    saved = dict(sys.modules)
    changed, asked = [], []

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod

    class _Recorder:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    def list_statistic_ids(hass, ids):
        asked.append(ids)
        return [r for r in RECORDED if r["statistic_id"] in ids]

    def update(hass, statistic_id, **change):
        changed.append((statistic_id, change))

    registry = types.SimpleNamespace(
        async_get_entity_id=lambda domain, platform, unique_id: (
            None
            if unique_id.endswith("not_registered")
            else f"sensor.{unique_id.removeprefix('node_')}"
        )
    )
    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.recorder", get_instance=lambda hass: _Recorder())
    module(
        "homeassistant.components.recorder.statistics",
        STATISTIC_UNIT_TO_UNIT_CONVERTER={
            "s": types.SimpleNamespace(UNIT_CLASS="duration")
        },
        async_update_statistics_metadata=update,
        list_statistic_ids=list_statistic_ids,
    )
    module("homeassistant.core", HomeAssistant=object)
    module("homeassistant.helpers")
    module("homeassistant.helpers.entity_registry", async_get=lambda hass: registry)
    sys.modules["homeassistant.helpers"].entity_registry = sys.modules[
        "homeassistant.helpers.entity_registry"
    ]
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(SOURCE)]
    sys.modules[PACKAGE] = package
    sys.modules[f"{PACKAGE}.const"] = types.SimpleNamespace(DOMAIN="optov")
    sys.modules[f"{PACKAGE}.coordinator"] = types.SimpleNamespace(
        OptolinkConfigEntry=object
    )
    try:
        units = importlib.import_module(f"{PACKAGE}.statistic_units")
        coordinator = types.SimpleNamespace(
            stable_id="node", profile=types.SimpleNamespace(sensors=SENSORS)
        )
        entry = types.SimpleNamespace(
            runtime_data=types.SimpleNamespace(coordinator=coordinator)
        )
        components = {"recorder"} if recorder_loaded else set()
        hass = types.SimpleNamespace(
            config=types.SimpleNamespace(components=components)
        )
        asyncio.run(units.async_relabel(hass, entry))
    finally:
        sys.modules.clear()
        sys.modules.update(saved)
    return asked, changed


def test_a_series_under_the_old_word_gets_the_unit_shown_now():
    asked, changed = _run()

    # Only readings whose unit is shown differently are looked up at all.
    assert asked == [{"sensor.run_time", "sensor.pressure"}]
    # The one still under its old word is relabelled, with the unit's class; the one that
    # already has the unit is left alone.
    assert changed == [
        (
            "sensor.run_time",
            {"new_unit_class": "duration", "new_unit_of_measurement": "s"},
        )
    ]


def test_without_a_recorder_nothing_is_asked():
    assert _run(recorder_loaded=False) == ([], [])
