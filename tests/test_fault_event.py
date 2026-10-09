"""The fault event fires for a new newest entry only, not for history it finds on arrival.

event.py imports Home Assistant, which the test run does not have, so the base classes are
replaced by minimal stand-ins and the integration is loaded as a package without `__init__`.
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

PACKAGE = "optov_event_under_test"
SOURCE = Path(__file__).resolve().parent.parent / "custom_components" / "optov"


class _EventEntity:
    def _trigger_event(self, event_type, attributes):
        self.fired.append((event_type, attributes))

    def async_write_ha_state(self):
        pass


class _CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    def __class_getitem__(cls, item):
        return cls

    async def async_added_to_hass(self):
        pass


@pytest.fixture(scope="module")
def event_module():
    # The stand-ins live only for the import: other test modules stub the same names their
    # own way, and must find sys.modules as it was.
    saved = dict(sys.modules)

    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules[name] = mod

    module("homeassistant")
    module("homeassistant.components")
    module("homeassistant.components.event", EventEntity=_EventEntity)
    module("homeassistant.config_entries", ConfigEntry=object)
    module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    module("homeassistant.helpers")
    module("homeassistant.helpers.entity", async_generate_entity_id=lambda *a, **k: "")
    module(
        "homeassistant.helpers.entity_platform", AddConfigEntryEntitiesCallback=object
    )
    module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=_CoordinatorEntity,
        DataUpdateCoordinator=_CoordinatorEntity,
        UpdateFailed=Exception,
    )
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(SOURCE)]
    sys.modules[PACKAGE] = package
    # Only the names event.py takes from the coordinator matter here.
    sys.modules[f"{PACKAGE}.coordinator"] = types.SimpleNamespace(
        OptolinkConfigEntry=object, OptolinkCoordinator=object
    )
    try:
        return importlib.import_module(f"{PACKAGE}.event")
    finally:
        sys.modules.clear()
        sys.modules.update(saved)


def _entity(module, last_state=None):
    coordinator = types.SimpleNamespace(
        stable_id="x",
        get_device_info=lambda circuit: {},
        object_id=lambda circuit, words: words,
        _error_history_loaded=False,
        error_history=[],
    )
    entity = module.OptolinkFaultEvent(coordinator, None)
    entity.fired = []

    async def last():
        return last_state

    entity.async_get_last_state = last
    return entity, coordinator


def _entry(code, when):
    return {"code": code, "description": f"text {code}", "timestamp": when}


def test_history_found_on_arrival_does_not_fire(event_module):
    entity, coordinator = _entity(event_module)
    asyncio.run(entity.async_added_to_hass())
    coordinator._error_history_loaded = True
    coordinator.error_history = [_entry("C4", "2024-01-02T03:04:05+00:00")]
    entity._handle_coordinator_update()
    assert entity.fired == []


def test_a_new_entry_fires_once_even_with_a_repeated_code(event_module):
    entity, coordinator = _entity(event_module)
    asyncio.run(entity.async_added_to_hass())
    coordinator._error_history_loaded = True
    coordinator.error_history = [_entry("C4", "2024-01-02T03:04:05+00:00")]
    entity._handle_coordinator_update()
    coordinator.error_history = [
        _entry("C4", "2026-10-08T10:00:00+00:00"),
        _entry("C4", "2024-01-02T03:04:05+00:00"),
    ]
    entity._handle_coordinator_update()
    entity._handle_coordinator_update()  # the next poll sees the same history
    assert entity.fired == [
        (
            "fault",
            {
                "code": "C4",
                "description": "text C4",
                "logged_at": "2026-10-08T10:00:00+00:00",
            },
        )
    ]


def test_a_history_going_back_in_time_does_not_fire(event_module):
    entity, coordinator = _entity(event_module)
    asyncio.run(entity.async_added_to_hass())
    coordinator._error_history_loaded = True
    coordinator.error_history = [_entry("C4", "2026-10-08T10:00:00+00:00")]
    entity._handle_coordinator_update()
    coordinator.error_history = [_entry("A2", "2025-01-01T00:00:00+00:00")]
    entity._handle_coordinator_update()
    assert entity.fired == []


def test_a_fault_logged_while_home_assistant_was_down_fires_after_restart(
    event_module,
):
    last = types.SimpleNamespace(
        attributes={"code": "C4", "logged_at": "2024-01-02T03:04:05+00:00"}
    )
    entity, coordinator = _entity(event_module, last)
    asyncio.run(entity.async_added_to_hass())
    coordinator._error_history_loaded = True
    coordinator.error_history = [
        _entry("D1", "2026-10-08T09:00:00+00:00"),
        _entry("C4", "2024-01-02T03:04:05+00:00"),
    ]
    entity._handle_coordinator_update()
    assert [attrs["code"] for _, attrs in entity.fired] == ["D1"]


def test_nothing_happens_before_the_history_is_read(event_module):
    entity, coordinator = _entity(event_module)
    asyncio.run(entity.async_added_to_hass())
    coordinator.error_history = [_entry("C4", "2026-10-08T10:00:00+00:00")]
    entity._handle_coordinator_update()  # not loaded yet
    coordinator._error_history_loaded = True
    entity._handle_coordinator_update()  # first read: the starting point
    assert entity.fired == []
