"""A write that arrives while something else is talking to the controller.

The link carries one telegram at a time, but a poll and a write are each several telegrams, so
they interleave. Two things must still hold: a poll that finishes after a write must not put
back the value the write replaced, and two writes to one shared byte must not undo each other.

The coordinator imports Home Assistant, which the test run does not have, so the few names it
takes from there are stubbed and the integration is loaded as a package without its
`__init__`.
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

PACKAGE = "optov_under_test"
SOURCE = Path(__file__).resolve().parent.parent / "custom_components" / "optov"


class _Names(type):
    """Any attribute is its own name: enough for constants like Platform.SENSOR."""

    def __getattr__(cls, name):
        return name


class _Constants(metaclass=_Names):
    pass


class _Stub:
    """Stands in for any Home Assistant class, subscripted or not."""

    def __init__(self, *args, **kwargs):
        pass

    def __class_getitem__(cls, item):
        return cls


def _stub_home_assistant():
    def module(name, **attrs):
        mod = types.ModuleType(name)
        mod.__dict__.update(attrs)
        sys.modules.setdefault(name, mod)

    module("homeassistant")
    module("homeassistant.config_entries", ConfigEntry=_Stub)
    module("homeassistant.const", Platform=_Constants, EntityCategory=_Constants)
    module("homeassistant.core", HomeAssistant=_Stub, callback=lambda f: f)
    module("homeassistant.helpers")
    for name in ("device_registry", "entity_registry", "issue_registry"):
        module(f"homeassistant.helpers.{name}", DeviceInfo=dict)
    module("homeassistant.helpers.storage", Store=_Stub)
    module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_Stub,
        UpdateFailed=Exception,
    )
    module("homeassistant.util", dt=types.SimpleNamespace())


@pytest.fixture(scope="module")
def coordinator_module():
    _stub_home_assistant()
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(SOURCE)]
    sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.coordinator")


# Operating mode and a flag sharing one byte, the way the catalog lays them out.
REGISTER = 0x2323
MODE = {
    "id": "mode",
    "name": "mode",
    "address": REGISTER,
    "bytes": 1,
    "bit_start": 0,
    "bit_length": 4,
    "options": {"0": "off", "2": "heat"},
}
FLAG = {
    "id": "flag",
    "name": "flag",
    "address": REGISTER,
    "bytes": 1,
    "bit_start": 7,
    "bit_length": 1,
}
OTHER = {"id": "other", "name": "other", "address": 0x2400, "bytes": 1}


class FakeController:
    """Memory behind the link. Every telegram yields, so concurrent callers interleave."""

    protocol = "P300"

    def __init__(self):
        self.memory = {REGISTER: bytes([0x00]), OTHER["address"]: bytes([0x05])}
        self.hold = {}  # address -> Event a read of it waits for

    async def read_raw(self, address, length, fc=None):
        await asyncio.sleep(0)
        if address in self.hold:
            await self.hold[address].wait()
        return self.memory[address]

    async def write_raw(self, address, data, fc=None):
        await asyncio.sleep(0)
        self.memory[address] = bytes(data)
        return True


def _coordinator(module, controller):
    c = module.OptolinkCoordinator.__new__(module.OptolinkCoordinator)
    c.__dict__.update(
        client=controller,
        profile=types.SimpleNamespace(
            sensors=[],
            binary_sensors=[],
            numbers=[],
            selects=[MODE],
            switches=[FLAG, OTHER],
            schedules={},
        ),
        data={},
        async_update_listeners=lambda: None,
        _write_lock=asyncio.Lock(),
        _cycle_data=None,
        _cycle_blocks={},
        _unsupported_addresses=set(),
        _skipped_this_session=set(),
        _nothing_usable={},
        _polling=False,
        _cycle_unreachable=None,
        _cycle_answered=0,
        _current_telegrams=0,
        _current_bytes=0,
        _last_read={},
        _last_publish=0.0,
        _force_full_sweep=False,
        _read_cost=0.05,
        _recent_shares=[],
        _backoff=1.0,
        _error_history_dp=None,
        _gfa_dp=None,
        scan_interval_seconds=15.0,
        update_interval=None,
        sensor_status={},
        sensor_status_raw={},
    )
    # Everything a poll does beyond reading and publishing is beside the point here.
    c._should_poll = lambda item, domain: True
    c._select_due = lambda items: {item["id"] for item, _ in items}
    c.clock_datapoint = lambda: None
    c._check_dst_rule = lambda data: None
    c._disable_unsupported_entities = lambda raw: None
    return c


def test_poll_finishing_after_a_write_keeps_the_written_value(coordinator_module):
    async def scenario():
        controller = FakeController()
        c = _coordinator(coordinator_module, controller)
        # The poll reads the mode, then stalls on a later datapoint while the write runs.
        controller.hold[OTHER["address"]] = asyncio.Event()
        poll = asyncio.create_task(c._async_update_data())
        while c._current_telegrams < 2:
            await asyncio.sleep(0)
        await c.async_write_item(MODE, 2)
        assert c.data["mode"] == "heat"
        controller.hold[OTHER["address"]].set()
        return await poll

    data = asyncio.run(scenario())
    assert data["mode"] == "heat"


def test_two_writes_to_one_byte_both_land(coordinator_module):
    async def scenario():
        controller = FakeController()
        c = _coordinator(coordinator_module, controller)
        await asyncio.gather(c.async_write_item(MODE, 2), c.async_write_item(FLAG, 1))
        return controller.memory[REGISTER][0]

    assert (
        asyncio.run(scenario()) == 0x21
    )  # mode 2 in the top four bits, flag in the lowest


PROGRAMME = {
    "address": 0x2000,
    "format": "phase3",
    "day_bytes": 24,
    "default_level": 0,
    "levels": [0, 1, 2, 3],
    "windows_per_day": 8,
    "mapping_type": 5,
}


class FakeProgrammes:
    """A controller holding one weekly programme. The first read can be held halfway."""

    protocol = "P300"

    def __init__(self, decode):
        self.decode = decode
        self.days = {day: bytes(24) for day in range(7)}
        self.hold = asyncio.Event()
        self.held = asyncio.Event()  # set once the first read is waiting
        self.reads = 0

    async def read_circuit_schedule(self, base_addr, *, day_bytes, fmt, **kwargs):
        self.reads += 1
        snapshot = dict(self.days)
        if self.reads == 1:
            self.held.set()
            await self.hold.wait()
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return {names[i]: self.decode(raw, fmt, 0) for i, raw in snapshot.items()}

    async def write_day_schedule(self, base_addr, day_idx, raw, **kwargs):
        await asyncio.sleep(0)
        self.days[day_idx] = bytes(raw)


def test_programme_read_in_the_background_keeps_an_edit(coordinator_module):
    async def scenario():
        controller = FakeProgrammes(coordinator_module.decode_day_schedule)
        c = _coordinator(coordinator_module, controller)
        c.profile.schedules = {"hc1": PROGRAMME}
        c.schedules = {}
        c._unsupported_schedules = set()
        background = asyncio.create_task(c.async_refresh_schedules())
        await controller.held.wait()
        edit = asyncio.create_task(
            c.async_set_schedule_window("hc1", "mon", 1, "06:00", "22:00", 2)
        )
        for _ in range(20):
            await asyncio.sleep(0)
        controller.hold.set()
        await asyncio.gather(background, edit)
        return c.schedules["hc1"]["mon"]

    windows = asyncio.run(scenario())
    assert [(w["start"], w["end"], w["mode"]) for w in windows] == [
        ("06:00", "22:00", 2)
    ]


def test_raw_write_waits_for_a_setting_write(coordinator_module):
    async def scenario():
        controller = FakeController()
        c = _coordinator(coordinator_module, controller)

        async def no_refresh():
            pass

        c.async_request_refresh = no_refresh
        await asyncio.gather(
            c.async_write_item(MODE, 2),
            c.async_write_custom_datapoint(REGISTER, bytes([0x01])),
        )
        return controller.memory[REGISTER][0]

    # The raw write came second, so its byte stands whole; interleaved, the setting write would
    # have written its patch over it.
    assert asyncio.run(scenario()) == 0x01
