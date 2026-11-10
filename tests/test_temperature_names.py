"""The assembled thermostat and water heater publish their temperatures under the names the
running Home Assistant reads: `native_*` from 2026.11 on, the earlier names before that.

Home Assistant is not installed for the test run, so both generations of its base classes are
stand-ins, and the integration is loaded as a package without `__init__`.
"""

import importlib
import sys
import types
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "custom_components" / "optov"


class _Anything:
    """Stands in for enums and flags that are only looked up at import."""

    def __getattr__(self, name):
        return name


def _load(native: bool):
    """Both platform modules, imported against a Home Assistant with or without `native_*`."""
    names = {"native_temperature_unit": None} if native else {}
    climate_base = type("ClimateEntity", (), dict(names))
    heater_base = type("WaterHeaterEntity", (), dict(names))
    package = f"optov_temperature_{'new' if native else 'old'}"
    saved = dict(sys.modules)

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod

    anything = _Anything()
    module("homeassistant")
    module("homeassistant.components")
    module(
        "homeassistant.components.climate",
        ATTR_HVAC_MODE="hvac_mode",
        PRESET_NONE="none",
        ClimateEntity=climate_base,
        ClimateEntityFeature=anything,
        HVACAction=anything,
        HVACMode=anything,
    )
    module(
        "homeassistant.components.water_heater",
        WaterHeaterEntity=heater_base,
        WaterHeaterEntityFeature=anything,
    )
    module(
        "homeassistant.const",
        ATTR_TEMPERATURE="temperature",
        STATE_OFF="off",
        STATE_ON="on",
        STATE_UNAVAILABLE="unavailable",
        STATE_UNKNOWN="unknown",
        UnitOfTemperature=types.SimpleNamespace(CELSIUS="°C"),
    )
    module(
        "homeassistant.core",
        Event=dict,
        EventStateChangedData=dict,
        HomeAssistant=object,
        callback=lambda f: f,
    )
    module("homeassistant.helpers")
    module("homeassistant.helpers.entity", Entity=object)
    module("homeassistant.helpers.event", async_track_state_change_event=None)
    module(
        "homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object
    )
    pkg = types.ModuleType(package)
    pkg.__path__ = [str(SOURCE)]
    sys.modules[package] = pkg
    # Only the names the two platforms take from these matter here.
    sys.modules[f"{package}.coordinator"] = types.SimpleNamespace(
        OptolinkConfigEntry=object
    )
    sys.modules[f"{package}.entity"] = types.SimpleNamespace(add_with_stable_ids=None)
    try:
        return (
            importlib.import_module(f"{package}.climate").OptolinkCustomClimate,
            importlib.import_module(
                f"{package}.water_heater"
            ).OptolinkCustomWaterHeater,
        )
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


OLD_NAMES = ("_attr_temperature_unit", "current_temperature", "target_temperature")


@pytest.mark.parametrize("index", [0, 1], ids=["climate", "water_heater"])
def test_the_current_names_only_where_home_assistant_has_them(index):
    cls = _load(native=True)[index]

    assert cls._attr_native_temperature_unit == "°C"
    assert isinstance(cls.native_current_temperature, property)
    assert isinstance(cls.native_target_temperature, property)
    # Defining an earlier name there is what Home Assistant reports as deprecated.
    assert not any(hasattr(cls, name) for name in OLD_NAMES)


@pytest.mark.parametrize("index", [0, 1], ids=["climate", "water_heater"])
def test_the_earlier_names_as_well_where_home_assistant_reads_those(index):
    cls = _load(native=False)[index]

    assert cls._attr_temperature_unit == "°C"
    assert cls.current_temperature is cls.native_current_temperature
    assert cls.target_temperature is cls.native_target_temperature
