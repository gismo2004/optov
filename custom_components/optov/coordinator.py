"""DataUpdateCoordinator for OptoV integration."""

import asyncio
import contextlib
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import catalog_db, optolink
from .const import (
    CONF_ENABLE_CODING2,
    CONF_ENABLE_COMMISSIONING,
    CONF_ENABLE_DIAGNOSTICS,
    CONF_ENABLE_EXPERT,
    CONF_HOST,
    CONF_INSTANCE,
    CONF_LANGUAGE,
    CONF_PROXY_NAME,
    CONF_SYNC_CLOCK,
    DEFAULT_ENABLE_CODING2,
    DEFAULT_ENABLE_COMMISSIONING,
    DEFAULT_ENABLE_DIAGNOSTICS,
    DEFAULT_ENABLE_EXPERT,
    DEFAULT_LANGUAGE,
    DEFAULT_SYNC_CLOCK,
    DOMAIN,
)
from .conversions import (
    DAY_INDEX,
    DAYS,
    decode_datetime_bcd,
    decode_day_schedule,
    encode_datetime_bcd,
    encode_day_schedule,
    parse_level,
)
from .decode import decode_value, extract_bitfield, insert_bitfield, is_signed
from .errors import decode_boiler_error_history, decode_wp_error_history
from .optolink import OptolinkClient
from .profiles import DeviceProfile, parse_address, stable_object_id
from .translate import async_ui_text

_LOGGER = logging.getLogger(__name__)

# Conversions that cannot be expressed as a bare div_ratio and must go through decode.py's
# catalog-driven logic -- anything here either produces a non-numeric result or applies a
# rule a divisor cannot express.
EXOTIC_CONVERSIONS = {
    "sec2minute",
    "sec2hour",
    "sec2day",
    "sec2week",
    "daytodate",
    "hexbyte2asciibyte",
    "hexbyte2utf16byte",
    "hexbyte2decimalbyte",
    "datetimebcd",
    "datetime_bcd",
    "datebcd",
    "time53",
    "ipaddress",
    "rotatebytes",
    "convert4bytestofloat",
    "multoffset",
    "mult2",
    "mult5",
    "mult10",
    "mult100",
}

# Catalog Priority at or below this is part of the fast set, polled every cycle. Everything
# above rotates through the remaining bus budget -- see OptolinkCoordinator._select_due().
_URGENT_PRIORITY = 50

# Share of the poll interval the bus may spend reading, and how hard the scheduler chases it.
#
# Nothing here is real-time: if a cycle overruns, the next one simply starts late, so the cost
# of aiming high is a late update rather than a failure. A write is a single telegram (~66 ms)
# and cannot meaningfully overrun a cycle on its own. The headroom that is left exists for the
# bursty work -- a schedule fetch, an error-history sweep, or a resync after a timeout -- which
# can add seconds at a time.
#
# The target is therefore aggressive and the scheduler backs off on evidence rather than on
# assumption: a cycle that comes close to the interval shrinks the next slice sharply, and a
# comfortable one grows it back gradually. That is what makes a high target safe.
_BUS_BUDGET = 0.90

# How often a datapoint wants to be read, as a multiple of the poll interval. The catalog
# supplies the inputs: `Trending` marks the fast-moving process values, `priority` ranks the
# rest, and whether an entity is writable separates settings from readings.
#
# A setting cannot change unless somebody changes it -- at the controller's own panel, since
# this integration writes only on request -- so it tolerates being read a few cycles later than
# a temperature. These are *targets*, not guarantees: the scheduler reads whatever is most
# overdue relative to its target and simply gets further behind when the bus is busy, which is
# what makes it self-levelling instead of starving anything.
_INTERVAL_TRENDING = 1
_INTERVAL_READING_FAST = 2  # read-only, catalog priority <= 50
_INTERVAL_READING_SLOW = 3  # read-only, lower catalog priority
# Writable settings used to wait twice as long again, on the argument that nobody changes them
# often. Measured against this controller that was holding the bus back for no reason: a
# telegram is ~63 ms, so even the full expert set of 428 datapoints is one pass in about 27 s,
# and the budget already leaves the bus half idle. A setting changed at the controller's own
# panel now shows up in a minute rather than two, and nothing else is any slower for it.
_INTERVAL_PARAMETER = 1
# The corridor the sweep is allowed to sit in without the budget being touched, and how many
# cycles have to agree before it is.
#
# A single cycle is a noisy measurement: a schedule fetch, an error-history sweep, a resync
# after one timeout, or a write the user just made all land in one cycle and make it look
# overloaded. Reacting to each of those produced a budget that moved almost every cycle and
# never settled, which is worse than a slightly wrong constant one -- the poll set kept
# reshuffling underneath the scheduler.
#
# Two things fix that. The corridor is wide, so ordinary variation changes nothing at all. And
# the decision is made on the *median* of the last few cycles rather than the newest one, so a
# single outlier cannot move it: two of three cycles must agree before the budget shifts.
_OVERRUN_AT = 1.05  # only a sweep that overruns the interval counts as too close
_COMFORTABLE_AT = 0.70  # below this the slice may grow again
_BACKOFF_WINDOW = 3  # cycles the median is taken over
_BACKOFF_DOWN = 0.80
_BACKOFF_UP = 1.10
_BACKOFF_FLOOR = 0.15
_DEFAULT_READ_COST = 0.066

# The controller's clock has no time source of its own and drifts by minutes a month. Every
# weekly programme is read against it, so an hour out means the heating runs an hour late.
# Under a minute is not worth a telegram, and a correction is not repeated within the hour --
# if a write did not take, hammering it will not help.
_CLOCK_TOLERANCE = 60  # seconds of drift tolerated before the clock is set
_CLOCK_RETRY_AFTER = 3600  # seconds before another correction is attempted

# How often a sweep pushes what it has so far to the entities.
_PUBLISH_EVERY = 2.0

# Shortest gap left between the end of one sweep and the start of the next.
_MIN_GAP = 1.0

# Sensor-health code meaning "this sensor is not fitted". The health nibble shares a block with
# the value it describes, so every reading comes with it for free. 0 is healthy and 1..9 are
# various faults, of which only 6 says the hardware is absent rather than broken -- consistent
# across all 69 sensor-status enumerations in the catalog. The distinction matters: an absent
# sensor should disappear, a broken one should stay visible and unavailable so the fault shows.
_SENSOR_NOT_PRESENT = 6


class OptolinkCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Class to manage fetching OptoV data dynamically from a DeviceProfile."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: OptolinkClient,
        scan_interval: int,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the coordinator."""
        self.client = client
        self.config_entry = config_entry
        self.profile: DeviceProfile | None = None
        self.device_info: DeviceInfo | None = None
        self.data: dict[str, Any] = {}
        self.schedules: dict[str, dict[str, list[dict[str, Any]]]] = {}
        # Sensor-health status decoded from the same block read as the value it describes
        # (WPR3_SensorStatus_* datapoints). Entities read this via `available`, not a separate
        # entity -- see decode.py for why the status nibble rides along for free.
        self.sensor_status: dict[str, str] = {}
        self.sensor_status_raw: dict[str, int] = {}
        self._schedule_poll_ticks = 0
        # This controller's catalog. Held here rather than as a module global so two hubs can
        # use two different catalogs; every catalog query is given it explicitly.
        self.db_path: str | None = None
        self._language = config_entry.options.get(
            CONF_LANGUAGE,
            config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
        )

        self.error_history: list[dict[str, Any]] = []
        self.last_error: dict[str, Any] | None = None
        # Catalog entry describing this device's error-history buffer (address, size,
        # block_factor, fc_read) plus its device-specific fault-code texts. Both resolved in
        # async_init_device(); None means this device family has no such buffer in the catalog.
        self._error_history_dp: dict[str, Any] | None = None
        # The controller's own clock, found in the catalog rather than by address, plus how
        # far it was last seen to be out and when it was last corrected. See clock_datapoint().
        self.clock_drift: int | None = None
        # Minus infinity, not zero: `time.monotonic()` counts from system boot, so zero would
        # suppress the first correction for an hour on a machine that has just started -- which
        # is exactly when a controller that lost power most needs its clock back.
        self._clock_corrected_at: float = float("-inf")
        self.clock_corrected: str | None = None
        # Whether the controller's own changeover settings agree with the local time zone.
        self.dst_status: str | None = None
        self._dst_warned: str | None = None
        self._dst_expected: list[dict[str, int]] | None = None
        self._dst_expected_for: tuple[int, str] | None = None
        self._error_codes: dict[str, str] = {}
        self._error_poll_ticks: int = 0
        # Addresses this controller answered ERR_NOT_IMPLEMENTED for. Learned at runtime from
        # the device itself rather than guessed from the catalog, which describes the whole
        # family and so lists datapoints no individual unit implements. See _read_reg().
        self._unsupported_addresses: set[int] = set()
        # Bytes already read this cycle, keyed exactly as _read_reg() asks for them, so that
        # several entities sharing one register cost one telegram between them. Cleared at the
        # end of every cycle.
        self._cycle_blocks: dict[tuple[int, int, str | None], bytes] = {}
        # Per-programme result of calibrate_record_step(), measured once on first read.
        self._schedule_record_step: dict[str, str] = {}
        # Programmes the controller answered ERR_NOT_IMPLEMENTED for. The catalog lists every
        # programme of the family; the unit says which ones it has, and it is asked once.
        self._unsupported_schedules: set[str] = set()
        # Addresses already acted on by _disable_unsupported_entities(), so the registry is
        # touched once per address rather than on every cycle.
        self._retired_addresses: set[int] = set(
            config_entry.data.get("retired_addresses") or []
        )
        self._retired_item_ids: set[str] = set(
            config_entry.data.get("retired_items") or []
        )
        # Rotation state for the slow pool, plus the measured cost of one datapoint read and
        # the worst-case refresh interval that follows from it.
        self._read_cost: float = _DEFAULT_READ_COST
        self._backoff: float = 1.0
        # Cycle durations as a share of the interval; the budget follows their median.
        self._recent_shares: deque[float] = deque(maxlen=_BACKOFF_WINDOW)
        self._last_read: dict[str, float] = {}
        self._deferred_last: int = -1
        self._last_publish: float = 0.0
        self._force_full_sweep: bool = True
        # Addresses already known absent are skipped from the very first poll rather than
        # being re-discovered each start.
        self._unsupported_addresses |= self._retired_addresses

        self.bus_load: float | None = None
        self.poll_duration: float | None = None
        self.active_channels: int = 0
        self.telegrams_failed: int = 0
        self.avg_response_time_ms: float | None = None
        self.telegram_rate: float | None = None
        # Readings delivered per second, the figure that rises when merging works.
        self.datapoint_rate: float | None = None
        self.datapoints_read: int = 0
        self.bytes_transferred: int = 0
        self.active_wire_load: float | None = None
        self.scan_interval_seconds: float = float(scan_interval)
        self._current_telegrams: int = 0
        self._current_failed: int = 0
        self._current_bytes: int = 0
        self._current_datapoints: int = 0

        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.data = {}
        self.sensor_status = {}
        self.sensor_status_raw = {}

    async def async_init_device(self) -> None:
        """Detect controller System ID and load matching device profile.

        DeviceIdent is four consecutive bytes:

            0x00F8 DeviceGroup      0x00F9 Device
            0x00FA HardwareIndex    0x00FB SoftwareIndex

        System ID is DeviceGroup:Device big-endian. The last two are two independent
        single-byte indices, NOT a little-endian 16-bit version -- reading them as one number
        yields a meaningless "software version". All four come back in one telegram.
        """
        sys_id = self.config_entry.data.get("sys_id")
        hw_index = sw_index = None
        try:
            ident = await self.client.read_raw(0x00F8, 4)
            if not sys_id:
                sys_id = int.from_bytes(ident[0:2], "big")
            hw_index, sw_index = ident[2], ident[3]
        except Exception as err:
            _LOGGER.error("Failed to read DeviceIdent: %s", err)
            if not sys_id:
                sys_id = 0x0000

        sw_version = f"v{sw_index:02X}" if sw_index is not None else ""
        if hw_index is not None:
            _LOGGER.info(
                "DeviceIdent 0x%04X, hardware index %d, software index %d",
                sys_id,
                hw_index,
                sw_index,
            )

        # Register 0x00F0 (2 bytes little-endian) is only consulted under a narrow guard --
        # Device byte 0xC0..0xCB and software index >= 200 -- so an ordinary controller never
        # pays for the extra telegram. Within that range it is the only thing that separates
        # several controller variants declaring the same IdentificationExtension range.
        f0 = None
        if (
            sw_index is not None
            and (sys_id & 0xFF) in range(0xC0, 0xCC)
            and sw_index >= 200
        ):
            try:
                f0 = int.from_bytes(await self.client.read_raw(0x00F0, 2), "little")
                _LOGGER.info("DeviceIdentF0 = %d", f0)
            except Exception as err:
                _LOGGER.warning("Could not read DeviceIdentF0 at 0x00F0: %s", err)

        dev = await self.hass.async_add_executor_job(
            catalog_db.get_device_by_system_id,
            sys_id,
            self.db_path,
            self._language,
            hw_index,
            sw_index,
            f0,
        )
        if not dev:
            raise ValueError(
                f"Controller System ID 0x{sys_id:04X} is not supported in the database."
            )
        self.profile = await self._async_generate_self_configuring_profile(
            dev, sys_id, sw_version
        )

        # Build clean DeviceInfo
        host = self.config_entry.data.get(CONF_HOST, "")
        proxy_name = self.config_entry.data.get(CONF_PROXY_NAME)
        instance = self.config_entry.data.get(CONF_INSTANCE, 0)
        name_suffix = (
            f" ({proxy_name})"
            if proxy_name
            else (f" #{instance}" if instance > 0 else "")
        )
        self.device_info = DeviceInfo(
            identifiers={(DOMAIN, self.config_entry.entry_id)},
            name=f"{self.profile.device_name}{name_suffix}",
            manufacturer="Viessmann",
            model=self.profile.model,
            sw_version=self.profile.sw_version,
            configuration_url=f"http://{host}" if host else None,
        )

        # One entry per weekly programme, filled by the first read.
        for programme in self.profile.schedules:
            self.schedules[programme] = {}

        # Error-history buffer geometry + fault-code texts, resolved entirely from the catalog
        # (address, size, element count, wire function code and per-device code texts). No
        # System-ID branching: a new device family works as soon as it is in catalog.db.
        self._error_history_dp = None
        self._error_codes = {}
        device_id = dev.get("id")
        if device_id is not None:
            try:
                self._error_history_dp = await self.hass.async_add_executor_job(
                    catalog_db.get_error_history_datapoint, device_id, self.db_path
                )
                self._error_codes = await self.hass.async_add_executor_job(
                    catalog_db.get_error_codes, device_id, self.db_path, self._language
                )
            except Exception as err:
                _LOGGER.warning(
                    "Could not resolve error-history catalog entry: %s", err
                )

        if self._error_history_dp:
            _LOGGER.info(
                "Error history: %s @0x%04X, %d bytes / %d entries (%d B each), %s, %d known codes",
                self._error_history_dp["name"],
                self._error_history_dp["address"],
                self._error_history_dp["total_bytes"],
                self._error_history_dp["block_factor"],
                self._error_history_dp["entry_bytes"],
                self._error_history_dp["fc_read"],
                len(self._error_codes),
            )

    def object_id(self, circuit: str | None, name: str) -> str:
        """A stable entity id for one of this controller's datapoints. See profiles.py."""
        model = self.profile.model if self.profile else DOMAIN
        circuits = getattr(self.profile, "circuits", {}) if self.profile else {}
        return stable_object_id(model, circuits.get(circuit) if circuit else None, name)

    def get_device_info(self, circuit: str | None = None) -> DeviceInfo | None:
        """Return DeviceInfo for the main controller or a circuit sub-device.

        Sub-device circuit names come directly from the device's catalog Circuits map,
        fully localized without any hardcoded dictionary.
        """
        if not self.device_info:
            return None
        if not circuit:
            return self.device_info

        circuits = getattr(self.profile, "circuits", {}) if self.profile else {}
        circuit_label = circuits.get(circuit, circuit)

        host = self.config_entry.data.get(CONF_HOST, "")
        proxy_name = self.config_entry.data.get(CONF_PROXY_NAME)
        instance = self.config_entry.data.get(CONF_INSTANCE, 0)
        name_suffix = (
            f" ({proxy_name})"
            if proxy_name
            else (f" #{instance}" if instance > 0 else "")
        )
        base_name = (
            f"{self.profile.device_name}{name_suffix}"
            if self.profile
            else f"Controller{name_suffix}"
        )
        base_model = self.profile.model if self.profile else "Optolink"

        dev_reg = dr.async_get(self.hass)
        parent_dev = dev_reg.async_get_device_by_identifier(
            (DOMAIN, self.config_entry.entry_id),
            self.config_entry.entry_id,
        )
        via_device_id = parent_dev.id if parent_dev else None

        info: DeviceInfo = DeviceInfo(
            identifiers={(DOMAIN, f"{self.config_entry.entry_id}_{circuit}")},
            name=f"{base_name} {circuit_label}",
            manufacturer="Viessmann",
            model=f"{base_model} ({circuit_label})",
            sw_version=self.profile.sw_version if self.profile else None,
            configuration_url=f"http://{host}" if host else None,
        )
        if via_device_id:
            info["via_device_id"] = via_device_id
        return info

    def get_gateway_device_info(self) -> DeviceInfo:
        """Return DeviceInfo for the Optolink Gateway/Interface device."""
        host = self.config_entry.data.get(CONF_HOST, "")
        esp_info = getattr(self.client, "esphome_info", None)
        sw_ver = getattr(esp_info, "esphome_version", None) if esp_info else None
        model_name = getattr(esp_info, "model", "ESP32") if esp_info else "ESP32"
        mac = getattr(esp_info, "mac_address", None) if esp_info else None
        connections = {(dr.CONNECTION_NETWORK_MAC, mac)} if mac else None

        proxy_name = self.config_entry.data.get(CONF_PROXY_NAME)
        instance = self.config_entry.data.get(CONF_INSTANCE, 0)
        name_suffix = (
            f" ({proxy_name})"
            if proxy_name
            else (f" #{instance}" if instance > 0 else "")
        )

        # The name is the one this integration invents rather than reads from the catalog, so
        # it comes from translations/<language>.json and follows the Home Assistant language,
        # not the catalog language chosen for datapoint names.
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self.config_entry.entry_id}_gateway")},
            translation_key="gateway",
            manufacturer="ESPHome",
            model=f"Optolink P300 Bridge ({model_name}){name_suffix}",
            sw_version=sw_ver,
            connections=connections,
            configuration_url=f"http://{host}" if host else None,
        )

    async def _probe_equipment(
        self, targets: list[dict[str, Any]], probed_values: dict[int, int]
    ) -> None:
        """Read the equipment registers, which say what hardware is actually fitted.

        One telegram per register. Reading several of them in one wider telegram was tried and
        withdrawn: the controller accepts such a read and answers with bytes that are not what
        those addresses return individually, so cutting the answer up by address silently
        produces wrong values. The whole result is cached in the config entry, so this runs
        once per installation and never again.
        """
        for dp in targets:
            try:
                address = parse_address(dp["address"])
                block = dp.get("block_length") or dp.get("byte_length") or 1
                raw = await self._read_reg(address, block, dp.get("fc_read"))
            except Exception as err:
                _LOGGER.debug(
                    "Equipment probe failed for %s (%s): %s",
                    dp.get("name"),
                    dp.get("address"),
                    err,
                )
                continue
            byte_position = dp.get("byte_position") or 0
            byte_len = dp.get("byte_length") or 1
            bit_length = dp.get("bit_length") or 0
            field = raw if bit_length else raw[byte_position : byte_position + byte_len]
            try:
                probed_values[dp["id"]] = int(
                    decode_value(
                        field,
                        None,
                        parameter_type=dp.get("parameter_type"),
                        bit_start=dp.get("bit_start") or 0,
                        bit_length=bit_length,
                    )
                )
            except Exception as err:
                _LOGGER.debug("Could not decode %s: %s", dp.get("name"), err)

    async def _async_generate_self_configuring_profile(
        self, dev: dict[str, Any], sys_id: int, sw_version: str
    ) -> DeviceProfile:
        """Probe the controller and generate the entity set dynamically from catalog.db.
        No per-device hardcoding: every field comes from the SQLite catalog plus this one-time probe.
        """
        probed_values: dict[int, int] = {}
        targets = await self.hass.async_add_executor_job(
            catalog_db.get_probe_targets, dev["id"], self.db_path
        )
        cached_conditions = self.config_entry.data.get("condition_cache")
        cached_targets = self.config_entry.data.get("condition_targets")
        target_ids = sorted(int(t["id"]) for t in targets)
        # Reuse the cache only while it was built from the same set of rule inputs. Keying on
        # the *attempted* targets rather than the probed values matters: a register the
        # controller refuses never appears in the cache, so a "does the cache contain every
        # target" test can never be satisfied and the probe would repeat on every start --
        # which, because storing the result updates the config entry, also reloads it.
        if (
            cached_conditions
            and isinstance(cached_conditions, dict)
            and cached_targets is not None
            and sorted(int(x) for x in cached_targets) == target_ids
        ):
            _LOGGER.info(
                "Self-configuring: using cached condition inputs (%d registers)",
                len(cached_conditions),
            )
            probed_values = {int(k): int(v) for k, v in cached_conditions.items()}
        else:
            if cached_conditions:
                _LOGGER.info(
                    "Self-configuring: the set of rule inputs changed (%d now), re-probing",
                    len(targets),
                )
            _LOGGER.info(
                "Self-configuring: probing %d equipment configuration registers for %s...",
                len(targets),
                dev["model"],
            )
            await self._probe_equipment(targets, probed_values)

            _LOGGER.info(
                "Self-configuring: probed %d/%d equipment configuration registers.",
                len(probed_values),
                len(targets),
            )
            new_data = dict(self.config_entry.data)
            new_data["condition_cache"] = {str(k): v for k, v in probed_values.items()}
            new_data["condition_targets"] = target_ids
            if new_data != dict(self.config_entry.data):
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=new_data
                )

        # Optional tiers the user has switched on. These are *added* to the base set (the
        # controller's own overview readings plus its operation menu), which is not tier-driven
        # -- so leaving every toggle off gives the everyday set, and switching one on brings in
        # that whole branch for as long as it is on.
        enabled_tiers: set[str] = set()
        if self.config_entry.options.get(
            CONF_ENABLE_DIAGNOSTICS, DEFAULT_ENABLE_DIAGNOSTICS
        ):
            enabled_tiers.update(
                {
                    "Trending",
                    "Statistic",
                    "DiagnosisDiagnosis1",
                    "DiagnosisDiagnosis2",
                    "Lasterror",
                }
            )
        if self.config_entry.options.get(
            CONF_ENABLE_COMMISSIONING, DEFAULT_ENABLE_COMMISSIONING
        ):
            enabled_tiers.add("Installation")
        if self.config_entry.options.get(CONF_ENABLE_CODING2, DEFAULT_ENABLE_CODING2):
            enabled_tiers.update({"Coding2", "DefaultSettings"})
        if self.config_entry.options.get(CONF_ENABLE_EXPERT, DEFAULT_ENABLE_EXPERT):
            enabled_tiers.update({"Expertlayer", "CodeAccessLevelTD"})

        generated = await self.hass.async_add_executor_job(
            catalog_db.generate_profile,
            dev["id"],
            self.db_path,
            self._language,
            probed_values,
            enabled_tiers,
        )
        total = sum(len(v) for v in generated.values() if isinstance(v, list))
        enabled = sum(
            1
            for cat in generated.values()
            if isinstance(cat, list)
            for e in cat
            if e.get("enabled_by_default")
        )
        _LOGGER.info(
            "Self-configuring: generated %d entities (%d enabled by default) for %s (0x%04X)",
            total,
            enabled,
            dev["model"],
            sys_id,
        )
        return DeviceProfile(
            sys_id=sys_id,
            data=generated,
            sw_version=sw_version,
            model_name=dev["model"],
        )

    def _should_poll(self, item: dict[str, Any], domain: str) -> bool:
        """Whether this datapoint is read at all.

        The profile carries hundreds of entries that are disabled by default, and polling them
        regardless would turn a 15 s interval into minutes on a 4800-baud link. So a datapoint
        is read only while its entity is enabled, and that is checked against the live entity
        registry rather than the static default: an entity the user switches on later starts
        being read at the next cycle, not at the next restart.
        """
        if item.get("enabled_by_default", True):
            return True
        registry = er.async_get(self.hass)
        unique_id = f"{self.config_entry.entry_id}_{item['id']}"
        entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
        if entity_id is None:
            return False
        registry_entry = registry.async_get(entity_id)
        return registry_entry is not None and not registry_entry.disabled

    def _target_interval(self, item: dict[str, Any], domain: str) -> float:
        """How many poll intervals this datapoint should wait between reads."""
        if (item.get("tier") or "") == "Trending":
            return _INTERVAL_TRENDING
        priority = item.get("priority") or 100
        base = (
            _INTERVAL_READING_FAST
            if priority <= _URGENT_PRIORITY
            else _INTERVAL_READING_SLOW
        )
        if domain in ("number", "select", "switch"):
            base *= _INTERVAL_PARAMETER
        return base

    def _select_due(self, items: list[tuple[dict[str, Any], str]]) -> set[str]:
        """Decide which datapoints this cycle reads.

        The fast set -- the trending channels, plus anything never read -- goes every cycle.
        The rest is one pool that rotates: each cycle takes the next slice,
        sized so the whole sweep fits the bus budget, and resumes where it left off. A value
        therefore has its own slot in a repeating rotation rather than a fixed bucket, and the
        worst case for noticing a change made at the controller itself is one full turn.

        Sizing from measured bus time rather than a fixed divisor matters because the amount
        enabled varies enormously -- the everyday set is a few dozen datapoints, switching on
        diagnostics and expert tiers makes it several hundred. A fixed divisor either wastes
        the bus in the first case or overruns the interval in the second.
        """
        now = time.monotonic()
        budget_s = self.scan_interval_seconds * _BUS_BUDGET * self._backoff
        cost = self._read_cost or _DEFAULT_READ_COST
        capacity = max(1, int(budget_s / cost))

        scored: list[tuple[float, str]] = []
        for item, domain in items:
            item_id = item["id"]
            if item_id not in self.data:
                # Never read. Always the most overdue thing there is, so a restart fills in as
                # fast as the bus allows instead of leaving values blank. An address the
                # controller has already refused is skipped: it can never yield a value and
                # would otherwise sit at the top of the list for good.
                if parse_address(item["address"]) not in self._unsupported_addresses:
                    scored.append((float("inf"), item_id))
                continue
            target = self._target_interval(item, domain) * self.scan_interval_seconds
            scored.append(((now - self._last_read.get(item_id, 0.0)) / target, item_id))

        # Most overdue first. Anything not reached this cycle grows more overdue and wins the
        # next one, so nothing starves however tight the budget gets.
        scored.sort(key=lambda entry: -entry[0])
        picked: set[str] = {item_id for _, item_id in scored[:capacity]}
        for item_id in picked:
            self._last_read[item_id] = now

        deferred = len(scored) - len(picked)
        if deferred != self._deferred_last:
            self._deferred_last = deferred
            _LOGGER.debug(
                "Poll schedule: %d of %d datapoints this cycle (capacity %d, budget %.0f%% "
                "of %.0fs); %d deferred to a later cycle",
                len(picked),
                len(scored),
                capacity,
                _BUS_BUDGET * self._backoff * 100,
                self.scan_interval_seconds,
                deferred,
            )
        return picked

    async def async_refresh_all(self) -> None:
        """Read every polled datapoint on the next cycle, ignoring the rotation.

        For use right after changing something at the controller itself: the rotation would
        otherwise take up to a full turn to notice, and there is no change notification in the
        protocol to shortcut that.
        """
        self._force_full_sweep = True
        _LOGGER.info("Full refresh requested; next poll reads every enabled datapoint")
        await self.async_request_refresh()

    def _publish_partial(
        self,
        data: dict[str, Any],
        sensor_status: dict[str, str],
        sensor_status_raw: dict[str, int],
    ) -> None:
        """Push what has been read so far, without waiting for the sweep to finish.

        A DataUpdateCoordinator normally publishes once, when the update coroutine returns, so
        every entity changes at the same instant and nothing changes in between. With a few
        hundred datapoints at ~66 ms each that is a long silence -- and on the first cycle after
        a restart it means no values at all for the length of a full sweep.

        Publishing as results arrive costs nothing on the bus and makes the sweep visible: the
        set fills in progressively rather than appearing in one lump.
        """
        now = time.monotonic()
        if now - self._last_publish < _PUBLISH_EVERY:
            return
        self._last_publish = now
        merged = dict(self.data or {})
        merged.update(data)
        self.data = merged
        self.sensor_status = {**self.sensor_status, **sensor_status}
        self.sensor_status_raw = {**self.sensor_status_raw, **sensor_status_raw}
        self.async_update_listeners()

    def _carry_forward(self, data: dict[str, Any], item_id: str) -> None:
        """Reuse the previous reading for a datapoint that is not due this cycle."""
        if self.data and item_id in self.data:
            data[item_id] = self.data[item_id]

    async def _read_reg(
        self,
        address: int,
        length: int,
        fc_read: str | None = None,
        fresh: bool = False,
    ) -> bytes:
        """Read raw bytes from Optolink and track telegram and byte counts.

        Addresses the controller has declared unimplemented are dropped permanently. The
        catalog describes a whole controller family, so it always lists more datapoints than
        any single unit implements. Such an address answers ERR_NOT_IMPLEMENTED -- the same
        code a nonsense address gets -- so the controller itself is the authority. Without this
        the same handful of absent datapoints is re-asked every cycle forever, each costing a
        full telegram round trip and a failure in the statistics.
        """
        if address in self._unsupported_addresses:
            raise optolink.OptolinkDeviceError(
                f"0x{address:04X} is not implemented on this controller",
                address=address,
                code=optolink.ERR_NOT_IMPLEMENTED,
            )

        # Another entity may already have read this exact register in this cycle. Several
        # do -- a value and its sensor-health nibble, or the same reading exposed on two
        # circuits -- and the second one is free.
        #
        # `fresh` is for the write path, where the cache is not merely unhelpful but wrong: the
        # bytes it holds were read before the write, so a read-back served from it always
        # reports the old value and the entity snaps straight back to it.
        if fresh:
            self._cycle_blocks.pop((address, length, fc_read), None)
        else:
            cached = self._cycle_blocks.get((address, length, fc_read))
            if cached is not None:
                return cached

        self._current_telegrams += 1
        # P300 protocol wire overhead: TX telegram (8B) + ACK (1B) + RX telegram (8B + length) + ACK (1B)
        self._current_bytes += 18 + length
        try:
            raw = await self.client.read_raw(
                address, length, optolink.function_code(fc_read)
            )
        except optolink.OptolinkDeviceError as err:
            if err.is_permanent:
                self._unsupported_addresses.add(address)
                _LOGGER.debug(
                    "0x%04X is not implemented on this controller (error 0x%02X); "
                    "dropping it from the poll set (%d dropped so far)",
                    address,
                    err.code,
                    len(self._unsupported_addresses),
                )
            else:
                # ERR_BAD_RANGE and friends mean our request geometry is wrong, which is our
                # bug to fix -- do not hide it by skipping the address.
                _LOGGER.warning(
                    "0x%04X refused with error 0x%02X for a %d-byte read; "
                    "the requested range does not match the controller's layout",
                    address,
                    err.code,
                    length,
                )
            raise
        self._cycle_blocks[(address, length, fc_read)] = raw
        return raw

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data dynamically for all points defined in the active profile."""
        if not self.profile:
            await self.async_init_device()

        start_time = time.monotonic()
        self._current_telegrams = 0
        self._current_failed = 0
        self._current_bytes = 0
        self._current_datapoints = 0

        data: dict[str, Any] = {}
        sensor_status: dict[str, str] = {}
        sensor_status_raw: dict[str, int] = {}

        pollable = [
            (item, domain)
            for platform, domain in (
                ("sensors", "sensor"),
                ("binary_sensors", "binary_sensor"),
                ("numbers", "number"),
                ("selects", "select"),
                ("switches", "switch"),
            )
            for item in getattr(self.profile, platform, [])
            if self._should_poll(item, domain)
        ]
        due = self._select_due(pollable)
        self._current_datapoints = len(due)
        self._force_full_sweep = False
        self._last_publish = time.monotonic()

        # 1. Read sensors
        for s in self.profile.sensors:
            if not self._should_poll(s, "sensor"):
                continue
            if s["id"] not in due:
                self._carry_forward(data, s["id"])
                if self.sensor_status and s["id"] in self.sensor_status:
                    sensor_status[s["id"]] = self.sensor_status[s["id"]]
                continue
            try:
                block = s.get("block") or s["bytes"]
                raw = await self._read_reg(s["address"], block, s.get("fc_read"))
                conv = (s.get("conversion") or "").strip().lower()
                # Sec2Hour/DayToDate/hex-string conversions need decode.py's real logic even when
                # the field isn't block-sliced (most are plain same-size counters) -- a bare
                # div_ratio can't express them: Sec2Hour would silently report seconds
                # labelled as hours otherwise.
                if (
                    s.get("bit_length")
                    or block != s["bytes"]
                    or conv in EXOTIC_CONVERSIONS
                ):
                    # Block-addressed datapoint: request the whole telegram the controller
                    # expects at this address, then slice the field out via decode.py.
                    field = (
                        raw
                        if s.get("bit_length")
                        else raw[
                            s.get("byte_position", 0) : s.get("byte_position", 0)
                            + s["bytes"]
                        ]
                    )
                    data[s["id"]] = decode_value(
                        field,
                        s.get("conversion"),
                        parameter_type=s.get("parameter_type", "SInt"),
                        bit_start=s.get("bit_start", 0),
                        bit_length=s.get("bit_length", 0),
                        enum=s.get("options"),
                        factor=s.get("conversion_factor"),
                        offset=s.get("conversion_offset"),
                    )
                    # WPR3_SensorStatus_* rides along in the same block as the value (one read,
                    # not a second one) -- exposed via the entity's `available` flag/attributes
                    # in sensor.py, not as a separate diagnostic entity.
                    if s.get("status_bit_length"):
                        raw_status = decode_value(
                            raw,
                            bit_start=s.get("status_bit_start", 0),
                            bit_length=s["status_bit_length"],
                        )
                        sensor_status[s["id"]] = decode_value(
                            raw,
                            bit_start=s.get("status_bit_start", 0),
                            bit_length=s["status_bit_length"],
                            enum=s.get("status_options"),
                        )
                        if raw_status is not None:
                            with contextlib.suppress(ValueError, TypeError):
                                sensor_status_raw[s["id"]] = int(raw_status)
                elif s.get("format") == "datetime_bcd":
                    data[s["id"]] = decode_datetime_bcd(raw)
                elif "options" in s:
                    int_val = int.from_bytes(raw, "little", signed=False)
                    options = s["options"]
                    data[s["id"]] = options.get(
                        str(int_val), options.get(int_val, str(int_val))
                    )
                else:
                    int_val = int.from_bytes(raw, "little", signed=True)
                    div = s.get("div_ratio") or 1.0
                    data[s["id"]] = round(int_val / div, 2)
            except Exception as err:
                self._current_failed += 1
                _LOGGER.debug(
                    "Failed reading sensor %s (0x%04X): %s", s["id"], s["address"], err
                )
                if self.data and s["id"] in self.data:
                    data[s["id"]] = self.data[s["id"]]
            self._publish_partial(data, sensor_status, sensor_status_raw)

        # 2. Read binary sensors
        for bs in self.profile.binary_sensors:
            if not self._should_poll(bs, "binary_sensor"):
                continue
            if bs["id"] not in due:
                self._carry_forward(data, bs["id"])
                continue
            try:
                block = bs.get("block") or bs["bytes"]
                raw = await self._read_reg(bs["address"], block, bs.get("fc_read"))
                byte_position = bs.get("byte_position", 0)
                field = (
                    raw[byte_position : byte_position + bs["bytes"]]
                    if block != bs["bytes"]
                    else raw
                )
                int_val = int.from_bytes(field, "little", signed=False)
                data[bs["id"]] = bool(int_val > 0)
            except Exception as err:
                self._current_failed += 1
                _LOGGER.debug(
                    "Failed reading binary sensor %s (0x%04X): %s",
                    bs["id"],
                    bs["address"],
                    err,
                )
                if self.data and bs["id"] in self.data:
                    data[bs["id"]] = self.data[bs["id"]]
            self._publish_partial(data, sensor_status, sensor_status_raw)

        # 3. Read numbers (setpoints)
        for num in self.profile.numbers:
            if not self._should_poll(num, "number"):
                continue
            if num["id"] not in due:
                self._carry_forward(data, num["id"])
                continue
            try:
                block = num.get("block") or num["bytes"]
                raw = await self._read_reg(num["address"], block, num.get("fc_read"))
                data[num["id"]] = self._decode_number(num, raw)
            except Exception as err:
                self._current_failed += 1
                _LOGGER.debug(
                    "Failed reading number %s (0x%04X): %s",
                    num["id"],
                    num["address"],
                    err,
                )
                if self.data and num["id"] in self.data:
                    data[num["id"]] = self.data[num["id"]]
            self._publish_partial(data, sensor_status, sensor_status_raw)

        # 4. Read selects (operating modes)
        for sel in self.profile.selects:
            if not self._should_poll(sel, "select"):
                continue
            if sel["id"] not in due:
                self._carry_forward(data, sel["id"])
                continue
            try:
                block = sel.get("block") or sel["bytes"]
                raw = await self._read_reg(sel["address"], block, sel.get("fc_read"))
                data[sel["id"]] = self._decode_select(sel, raw)
            except Exception as err:
                self._current_failed += 1
                _LOGGER.debug(
                    "Failed reading select %s (0x%04X): %s",
                    sel["id"],
                    sel["address"],
                    err,
                )
                if self.data and sel["id"] in self.data:
                    data[sel["id"]] = self.data[sel["id"]]

        # 5. Read switches (writable single bits)
        for sw in self.profile.switches:
            if not self._should_poll(sw, "switch"):
                continue
            if sw["id"] not in due:
                self._carry_forward(data, sw["id"])
                continue
            try:
                block = sw.get("block") or sw["bytes"]
                raw = await self._read_reg(sw["address"], block, sw.get("fc_read"))
                data[sw["id"]] = self._raw_int(sw, raw, False)
            except Exception as err:
                self._current_failed += 1
                _LOGGER.debug(
                    "Failed reading switch %s (0x%04X): %s",
                    sw["id"],
                    sw["address"],
                    err,
                )
                if self.data and sw["id"] in self.data:
                    data[sw["id"]] = self.data[sw["id"]]

        # The cached bytes describe this cycle only. Anything reading outside it -- a write
        # and its read-back above all -- must go to the controller, not to a snapshot that is
        # already seconds old.
        self._cycle_blocks = {}

        duration = time.monotonic() - start_time
        self.poll_duration = round(duration, 2)
        # Duty is the share of the *period* spent talking to the controller, so it must be
        # measured against the configured interval rather than `update_interval`: the latter is
        # now shortened by however long the sweep took in order to hold the period steady, so
        # using it as the denominator divides by the idle gap and pins the figure at 100%.
        period = self.scan_interval_seconds
        self.bus_load = (
            round(min(100.0, (duration / period) * 100.0), 1) if period > 0 else 0.0
        )
        self.active_channels = self._current_telegrams
        self.telegrams_failed = self._current_failed
        self.bytes_transferred = self._current_bytes
        self.datapoints_read = self._current_datapoints
        self.datapoint_rate = (
            round(self._current_datapoints / duration, 1) if duration > 0 else 0.0
        )
        if duration > 0 and self._current_telegrams > 0:
            self.avg_response_time_ms = round(
                (duration / self._current_telegrams) * 1000.0, 1
            )
            self.telegram_rate = round(self._current_telegrams / duration, 1)
            # Physical baudrate 4800 (10 bits/char -> 480 B/s maximum wire rate)
            bytes_per_sec = self._current_bytes / duration
            self.active_wire_load = round(
                min(100.0, (bytes_per_sec / 480.0) * 100.0), 1
            )
        else:
            self.avg_response_time_ms = 0.0
            self.telegram_rate = 0.0
            self.active_wire_load = 0.0

        if not data:
            raise UpdateFailed("Failed to communicate with OptoV controller")

        self.sensor_status = sensor_status
        self.sensor_status_raw = sensor_status_raw

        # 5. Weekly programmes, in the background. Cheap enough not to be worth an option:
        # each is four telegrams, re-read once every 120 cycles, so three programmes cost under
        # a second of bus time per half hour.
        if self.profile.schedules:
            has_empty_sched = any(
                not self.schedules.get(key)
                for key in self.profile.schedules
                if key not in self._unsupported_schedules
            )
            if has_empty_sched or (self._schedule_poll_ticks % 120 == 0):
                self.config_entry.async_create_background_task(
                    self.hass, self.async_refresh_schedules(), "optov_programmes"
                )
            self._schedule_poll_ticks += 1

        # 6. Background error history poll (startup and every ~30 min / 60 poll cycles)
        if self._error_history_dp:
            if not self.error_history or (self._error_poll_ticks % 60 == 0):
                self.config_entry.async_create_background_task(
                    self.hass, self.async_refresh_error_history(), "optov_faults"
                )
            self._error_poll_ticks += 1

        # Learn what a read actually costs on this link, so the next cycle's slice is sized
        # from measurement rather than the compiled-in estimate. Smoothed, because a single
        # cycle that hit a retry would otherwise shrink the slice sharply.
        elapsed = time.monotonic() - start_time
        if self._current_telegrams:
            measured = elapsed / self._current_telegrams
            self._read_cost = (self._read_cost * 3 + measured) / 4

        # Hold the *period* at the configured interval, not the gap between sweeps.
        #
        # A DataUpdateCoordinator schedules the next refresh `update_interval` after the
        # previous one finishes, so the period is duration + interval, not interval. With a
        # 12 s sweep and a 15 s setting that made the real period 27 s: the bus sat idle 55% of
        # the time while the budget believed it was using 80%, and every target interval was
        # silently in units of 27 s rather than 15 s. Shrinking the gap by however long the
        # sweep took makes "15 s" mean 15 s between starts, which is what the targets assume.
        gap = max(_MIN_GAP, self.scan_interval_seconds - elapsed)
        wanted = timedelta(seconds=gap)
        if self.update_interval != wanted:
            self.update_interval = wanted

        # Closed-loop correction on the observed cycle duration. This is what catches the
        # things a per-read estimate cannot see: a resync after a timeout, a schedule fetch or
        # an error-history sweep running alongside, or simply a slower controller.
        share = elapsed / max(1.0, self.scan_interval_seconds)
        self._recent_shares.append(share)
        previous = self._backoff
        if len(self._recent_shares) == _BACKOFF_WINDOW:
            # The median, so one unusual cycle -- a schedule fetch, a fault-history sweep, a
            # resync -- cannot move the budget on its own.
            typical = sorted(self._recent_shares)[_BACKOFF_WINDOW // 2]
            if typical > _OVERRUN_AT:
                self._backoff = max(_BACKOFF_FLOOR, self._backoff * _BACKOFF_DOWN)
            elif typical < _COMFORTABLE_AT:
                self._backoff = min(1.0, self._backoff * _BACKOFF_UP)
            if abs(self._backoff - previous) > 0.01:
                _LOGGER.debug(
                    "Poll budget %.0f%% of interval (last %d cycles used %s, median %.0f%%)",
                    _BUS_BUDGET * self._backoff * 100,
                    _BACKOFF_WINDOW,
                    ", ".join(f"{x * 100:.0f}%" for x in self._recent_shares),
                    typical * 100,
                )

        # 7. Keep the controller's clock right. The sweep has just read it, so noticing the
        # drift is free; only the correction itself costs a telegram, and only when it is
        # both wrong enough and not been tried in the last hour.
        clock = self.clock_datapoint()
        if clock is not None:
            self.clock_drift = self._clock_drift(data.get(clock["id"]))
            if (
                self.config_entry.options.get(CONF_SYNC_CLOCK, DEFAULT_SYNC_CLOCK)
                and self.clock_drift is not None
                and abs(self.clock_drift) >= _CLOCK_TOLERANCE
                and time.monotonic() - self._clock_corrected_at >= _CLOCK_RETRY_AFTER
            ):
                self.config_entry.async_create_background_task(
                    self.hass, self.async_sync_clock(), "optov_clock"
                )

        self._check_dst_rule(data)

        # 8. Retire entities the controller says it does not have.
        self._disable_unsupported_entities(sensor_status_raw)

        return data

    def clock_datapoint(self) -> dict[str, Any] | None:
        """The controller's clock, identified by what it is rather than where it lives.

        The catalog marks it with the BCD timestamp conversion and a write function code, and
        there is exactly one such datapoint per controller. Matching on that rather than on an
        address keeps this working across the families, whose clocks sit at different
        registers.
        """
        if not self.profile:
            return None
        for item in self.profile.sensors:
            conversion = (item.get("conversion") or "").strip().lower()
            if conversion in ("datetimebcd", "datetime_bcd") and item.get("fc_write"):
                return item
        return None

    def _clock_drift(self, reading: Any) -> int | None:
        """Seconds the controller is ahead of Home Assistant, from an already-polled value."""
        if not reading:
            return None
        try:
            controller = datetime.strptime(str(reading), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
        return round((controller - datetime.now()).total_seconds())

    async def async_sync_clock(self, force: bool = False) -> int | None:
        """Set the controller's clock from this machine's, and report the drift corrected.

        The controller has no time source of its own and drifts by minutes a month, which
        matters because every weekly programme is read against it: an hour out means the
        heating comes on an hour late. Nothing else here writes without being asked, so the
        correction is deliberately quiet about small errors -- under a minute is not worth a
        write -- and never repeats within the hour unless asked to.
        """
        item = self.clock_datapoint()
        if item is None:
            _LOGGER.debug("This controller has no clock datapoint in the catalog")
            return None

        block = item.get("block") or item["bytes"]
        raw = await self._read_reg(
            item["address"], block, item.get("fc_read"), fresh=True
        )
        drift = self._clock_drift(decode_datetime_bcd(raw))
        self.clock_drift = drift
        if drift is None:
            _LOGGER.warning(
                "Could not read the controller clock at 0x%04X", item["address"]
            )
            return None
        if not force:
            if abs(drift) < _CLOCK_TOLERANCE:
                return drift
            if time.monotonic() - self._clock_corrected_at < _CLOCK_RETRY_AFTER:
                return drift

        now = datetime.now()
        if now.year < 2024:
            # Home Assistant's own clock is not trustworthy yet -- a container that starts
            # before the host has a time source reports the epoch. Writing that would replace
            # a slightly wrong controller clock with a badly wrong one.
            _LOGGER.warning(
                "Not setting the controller clock: this machine reports %s, which cannot be "
                "right",
                now.strftime("%Y-%m-%d %H:%M:%S"),
            )
            return drift
        payload = encode_datetime_bcd(now)
        _LOGGER.info(
            "Controller clock is %+d s out; setting it to %s",
            drift,
            now.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await self.client.write_raw(
            item["address"],
            payload,
            optolink.function_code(item.get("fc_write"), optolink.FC_VIRTUAL_WRITE),
        )
        self._clock_corrected_at = time.monotonic()
        self.clock_corrected = now.strftime("%Y-%m-%d %H:%M:%S")
        readback = await self._read_reg(
            item["address"], block, item.get("fc_read"), fresh=True
        )
        self.clock_drift = self._clock_drift(decode_datetime_bcd(readback))
        self._last_read.pop(item["id"], None)
        return drift

    def _expected_dst_rule(self) -> list[dict[str, int]] | None:
        """When the local time zone actually changes over, as month/week/weekday.

        Taken from the zone's own transitions for the current year rather than from a rule
        written down here, so it is right wherever the installation is. The week is reported
        as 5 when the changeover falls on the last such weekday of the month, which is how the
        controller expresses "last": its own setting stays at 5 across years in which the last
        Sunday is the fourth one, and no other reading of the field survives that.
        """
        zone = dt_util.get_default_time_zone()
        year = datetime.now().year
        # Walking a year in hourly steps is thousands of conversions, and the answer only
        # changes when the year or the zone does.
        if self._dst_expected_for == (year, str(zone)):
            return self._dst_expected
        changeovers: list[dict[str, int]] = []
        moment = datetime(year, 1, 1, tzinfo=UTC)
        previous = moment.astimezone(zone).utcoffset()
        for hour in range(1, 366 * 24):
            local = (moment + timedelta(hours=hour)).astimezone(zone)
            if local.utcoffset() == previous:
                continue
            previous = local.utcoffset()
            is_last = (local + timedelta(days=7)).month != local.month
            changeovers.append(
                {
                    "month": local.month,
                    "week": 5 if is_last else (local.day - 1) // 7 + 1,
                    "weekday": local.isoweekday(),
                }
            )
        self._dst_expected = changeovers if len(changeovers) == 2 else None
        self._dst_expected_for = (year, str(zone))
        return self._dst_expected

    def _check_dst_rule(self, data: dict[str, Any]) -> None:
        """Compare the controller's changeover settings with the local time zone's.

        Reported, never written. Which field is the month, the week and the weekday is read
        off the catalog's value ranges rather than stated by it, and the meaning of the week
        field is inferred; that is sound enough to tell somebody their controller disagrees
        with their time zone, and not sound enough to silently change their heating's settings
        on the strength of it. It matters little either way now -- the clock is corrected every
        cycle, so a wrong rule costs one poll interval twice a year -- but a wrong rule is
        still worth saying out loud.
        """
        rule = getattr(self.profile, "clock_dst", None) or {}
        changeovers = rule.get("changeovers")
        if not changeovers:
            self.dst_status = None
            return

        by_address: dict[str, Any] = {}
        for platform in ("numbers", "selects", "switches", "sensors"):
            for item in getattr(self.profile, platform, []):
                by_address.setdefault(f"0x{item['address']:04X}", item)

        def reading(address: str) -> int | None:
            item = by_address.get(address)
            if item is None:
                return None
            value = data.get(item["id"])
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return None

        configured = [
            {role: reading(address) for role, address in changeover.items()}
            for changeover in changeovers
        ]
        if any(v is None for c in configured for v in c.values()):
            return  # not read yet this cycle; say nothing rather than something wrong

        expected = self._expected_dst_rule()
        if expected is None:
            self.dst_status = "no changeover in this time zone"
            return

        def render(rules: list[dict[str, int]]) -> str:
            return ", ".join(
                f"{r['month']}/{'last' if r['week'] == 5 else r['week']}/{r['weekday']}"
                for r in rules
            )

        zone = str(dt_util.get_default_time_zone())
        if configured == expected:
            self.dst_status = f"matches {zone} ({render(expected)})"
            if self._dst_warned is not None:
                self._dst_warned = None
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._notify_dst_mismatch("", "", zone),
                    "optov_dst_clear",
                )
            return
        self.dst_status = (
            f"controller {render(configured)} but {zone} changes at {render(expected)}"
        )
        if self.dst_status != self._dst_warned:
            self._dst_warned = self.dst_status
            _LOGGER.warning(
                "The controller's daylight-saving changeover does not match this time zone: "
                "%s. Its clock is corrected every cycle regardless, so the effect is limited "
                "to one poll interval twice a year.",
                self.dst_status,
            )
            self.config_entry.async_create_background_task(
                self.hass,
                self._notify_dst_mismatch(render(configured), render(expected), zone),
                "optov_dst_notify",
            )

    async def _notify_dst_mismatch(
        self, configured: str, expected: str, zone: str
    ) -> None:
        """Raise, or clear, a notification about the controller's changeover settings.

        Worth interrupting somebody for once, because it is a real misconfiguration and a log
        line is not read. It carries a fixed id so repeats replace rather than stack, and it is
        dismissed as soon as the settings agree again.
        """
        from homeassistant.components.persistent_notification import (
            async_create,
            async_dismiss,
        )

        note_id = f"{DOMAIN}_dst_{self.config_entry.entry_id}"
        if not configured:
            async_dismiss(self.hass, note_id)
            return
        async_create(
            self.hass,
            await async_ui_text(
                self.hass,
                "dst_mismatch",
                configured=configured,
                expected=expected,
                zone=zone,
            ),
            title=await async_ui_text(self.hass, "dst_mismatch_title"),
            notification_id=note_id,
        )

    def _persist_retired(self) -> None:
        """Remember retired entities across restarts.

        Without this the reconciliation in async_setup_entry would see an entity that is still
        `enabled_by_default` but disabled in the registry, re-enable it on every start, and the
        first poll would retire it again -- churning the registry on each restart and briefly
        showing entities for hardware that is not there.
        """
        new_data = dict(self.config_entry.data)
        new_data["retired_items"] = sorted(self._retired_item_ids)
        new_data["retired_addresses"] = sorted(self._retired_addresses)
        # Left behind by the withdrawn merged-read experiment; drop them rather than carry
        # dead fields in the entry for ever.
        for gone in (
            "refused_ranges",
            "merged_ranges",
            "hopeless_ranges",
            "merge_knowledge",
        ):
            new_data.pop(gone, None)
        if new_data != dict(self.config_entry.data):
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )

    def _disable_unsupported_entities(
        self, sensor_status_raw: dict[str, int] | None = None
    ) -> None:
        """Disable entities for hardware this controller does not have.

        Two independent signals, both the controller's own answer rather than a guess:

        * the address is refused outright with a permanent error, and
        * the address reads fine but its sensor-health nibble reports the sensor as not fitted.

        The second is why temperatures could show up as "unavailable" rather than simply not
        being there: the value is readable, it is just meaningless, and the health nibble is
        what says so. A sensor reporting a *fault* is deliberately left alone -- that is
        something worth seeing.

        The catalog describes a controller family, so it always offers more datapoints than a
        given unit implements. Display-condition rules remove most of the difference, but they
        cannot cover everything -- some rules reference registers this controller does not
        have, and some absent hardware is simply not described by a rule at all.

        The controller itself settles it: the address is refused with a permanent error, which
        _read_reg() records. Those entities can never produce a value, so leaving them enabled
        only yields a permanently unavailable entity. Disabling is one-way -- re-enabling by
        hand sticks, because the address stays in the skip set only for this session.
        """
        if not self.profile:
            return

        # Entities whose health nibble says the sensor is absent.
        absent_ids = {
            item_id
            for item_id, code in (sensor_status_raw or {}).items()
            if code == _SENSOR_NOT_PRESENT and item_id not in self._retired_item_ids
        }
        pending = self._unsupported_addresses - self._retired_addresses
        if not pending and not absent_ids:
            return

        by_address: dict[int, list[tuple[str, str]]] = {}
        for platform, domain in (
            ("sensors", "sensor"),
            ("binary_sensors", "binary_sensor"),
            ("numbers", "number"),
            ("selects", "select"),
            ("switches", "switch"),
        ):
            for item in getattr(self.profile, platform, []):
                by_address.setdefault(item["address"], []).append((domain, item["id"]))

        registry = er.async_get(self.hass)

        def _disable(domain: str, item_id: str) -> bool:
            entity_id = registry.async_get_entity_id(
                domain, DOMAIN, f"{self.config_entry.entry_id}_{item_id}"
            )
            if not entity_id:
                return False
            entry = registry.async_get(entity_id)
            if entry is None or entry.disabled:
                return False
            registry.async_update_entity(
                entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
            return True

        by_id = {
            item["id"]: domain
            for platform, domain in (
                ("sensors", "sensor"),
                ("binary_sensors", "binary_sensor"),
                ("numbers", "number"),
                ("selects", "select"),
            )
            for item in getattr(self.profile, platform, [])
        }

        unimplemented = 0
        for address in pending:
            for domain, item_id in by_address.get(address, []):
                unimplemented += _disable(domain, item_id)
                # Record the item as well as the address. The guard in async_setup_entry that
                # stops a retired entity being re-enabled on the next start works on item ids,
                # so storing only the address let these come back on every restart -- and then
                # sit in the poll set permanently, because a datapoint that never yields a
                # value counts as "never read" and is treated as always due.
                self._retired_item_ids.add(item_id)
            self._retired_addresses.add(address)

        not_fitted = 0
        for item_id in absent_ids:
            domain = by_id.get(item_id)
            if domain:
                not_fitted += _disable(domain, item_id)
            self._retired_item_ids.add(item_id)

        if unimplemented or not_fitted:
            self._persist_retired()
        if unimplemented:
            _LOGGER.info(
                "Disabled %d entities for %d addresses this controller does not implement",
                unimplemented,
                len(pending),
            )
        if not_fitted:
            _LOGGER.info(
                "Disabled %d entities whose sensor the controller reports as not fitted",
                not_fitted,
            )

    async def async_read_custom_datapoint(self, address: int, length: int = 2) -> bytes:
        """Perform an ad-hoc read of any address without polling."""
        return await self.client.read_raw(address, length)

    def _slice_field(self, item: dict[str, Any], raw: bytes) -> bytes:
        """Cut the field out of the block the controller returned."""
        block = item.get("block") or item["bytes"]
        pos = item.get("byte_position", 0)
        return raw[pos : pos + item["bytes"]] if block != item["bytes"] else raw

    def _raw_int(self, item: dict[str, Any], raw: bytes, signed: bool) -> int:
        """The datapoint's own integer, whether it is whole bytes or bits of a shared one."""
        bit_length = item.get("bit_length") or 0
        if bit_length:
            return extract_bitfield(raw, item.get("bit_start", 0), bit_length)
        return int.from_bytes(self._slice_field(item, raw), "little", signed=signed)

    def _decode_number(self, item: dict[str, Any], raw: bytes) -> float:
        return round(self._raw_int(item, raw, True) / (item.get("div_ratio") or 1.0), 1)

    def _decode_select(self, item: dict[str, Any], raw: bytes) -> str:
        raw_int = self._raw_int(item, raw, False)
        options = item.get("options", {})
        return options.get(str(raw_int), options.get(raw_int, str(raw_int)))

    async def async_write_custom_datapoint(self, address: int, data: bytes) -> bool:
        """Perform an ad-hoc write of any address."""
        res = await self.client.write_raw(address, data)
        await self.async_request_refresh()
        return res

    async def async_write_item(self, item: dict[str, Any], value: int) -> bool:
        """Write one catalog datapoint, patching it into its block when it is not alone there.

        A datapoint is often a slice of a larger register rather than the whole thing -- a
        4-bit operating mode sharing a byte with the party and eco flags, or a two-byte value
        at an offset inside a ten-byte block. Writing the bare value to the block's address
        would put it at the wrong offset and overwrite whatever else lives there, so the block
        is read first, the field replaced, and the whole block written back.

        A rejected write is reported by the controller as an error telegram, the same mechanism
        that rejects a bad read, and surfaces as OptolinkDeviceError.
        """
        # A write must see the controller's current bytes, never this cycle's snapshot.
        self._cycle_blocks = {}

        address = item["address"]
        width = item["bytes"]
        block = item.get("block") or width
        bit_length = item.get("bit_length") or 0
        signed = is_signed(item.get("parameter_type"))

        if bit_length or block != width:
            current = await self._read_reg(
                address, block, item.get("fc_read"), fresh=True
            )
            if bit_length:
                payload = insert_bitfield(
                    current, item.get("bit_start", 0), bit_length, value
                )
            else:
                position = item.get("byte_position", 0)
                buffer = bytearray(current)
                buffer[position : position + width] = value.to_bytes(
                    width, "little", signed=signed
                )
                payload = bytes(buffer)
        else:
            payload = value.to_bytes(width, "little", signed=signed)

        # "undefined" is the catalog's way of saying it never recorded one, not a code.
        fc_write = item.get("fc_write")
        if not fc_write or fc_write == "undefined":
            fc_write = "Virtual_WRITE"
        _LOGGER.info(
            "Writing %s (0x%04X): value=%s block=%s -> %s via %s",
            item.get("name", item["id"]),
            address,
            value,
            current.hex(" ") if (bit_length or block != width) else "-",
            payload.hex(" "),
            fc_write,
        )
        result = await self.client.write_raw(
            address,
            payload,
            optolink.function_code(fc_write, optolink.FC_VIRTUAL_WRITE),
        )
        # The controller applies a write immediately, but the entity keeps showing whatever the
        # last poll read until the scheduler gets round to this address again -- up to a couple
        # of cycles, because a datapoint polled recently is the least overdue thing there is.
        # In the meantime the frontend snaps the control back to the stale value, which looks
        # exactly like a write that never happened (it has been reported as one). Reading the
        # block straight back and publishing it closes that gap, refreshes every other control
        # sharing the register (operating mode, party and eco live in one byte), and is the only
        # way to notice a write the controller acknowledged but did not apply.
        try:
            readback = await self._read_reg(
                address, block, item.get("fc_read"), fresh=True
            )
        except Exception as err:
            _LOGGER.warning("Read-back of 0x%04X after write failed: %s", address, err)
        else:
            if readback != payload:
                _LOGGER.warning(
                    "Write to %s (0x%04X) was accepted but not applied: sent %s, controller "
                    "reports %s",
                    item.get("name", item["id"]),
                    address,
                    payload.hex(" "),
                    readback.hex(" "),
                )
            self._publish_block(address, block, item.get("fc_read"), readback)
        # Leave the datapoint overdue as well, so the next regular cycle confirms the value
        # from a fresh read rather than trusting the one taken right after the write.
        self._last_read.pop(item["id"], None)
        return result

    def _publish_block(
        self, address: int, block: int, fc_read: str | None, raw: bytes
    ) -> None:
        """Decode one freshly read block into every control that lives in it and notify."""
        if not self.profile:
            return

        def _same_block(item: dict[str, Any]) -> bool:
            return (
                parse_address(item["address"]) == address
                and (item.get("block") or item["bytes"]) == block
                and item.get("fc_read") == fc_read
            )

        updated: dict[str, Any] = {}
        for num in self.profile.numbers:
            if _same_block(num):
                updated[num["id"]] = self._decode_number(num, raw)
        for sel in self.profile.selects:
            if _same_block(sel):
                updated[sel["id"]] = self._decode_select(sel, raw)
        for sw in getattr(self.profile, "switches", []):
            if _same_block(sw):
                updated[sw["id"]] = self._raw_int(sw, raw, False)
        if not updated:
            return
        self.data = {**(self.data or {}), **updated}
        self.async_update_listeners()

    @property
    def language(self) -> str:
        """The catalog language this entry was set up with."""
        return self._language

    def resolve_schedule(self, ref: str | None) -> str | None:
        """Map a programme reference from a service call onto the profile's own key.

        Accepts the key itself in any case, the programme's catalog name, or -- for calls
        written against earlier versions -- a circuit code, which resolves to that circuit's
        first programme.
        """
        if not self.profile or not ref:
            return None
        wanted = str(ref).strip().lower()
        for key, cfg in self.profile.schedules.items():
            if key.lower() == wanted or str(cfg.get("name") or "").lower() == wanted:
                return key
        in_circuit = sorted(
            (parse_address(cfg["address"]), key)
            for key, cfg in self.profile.schedules.items()
            if str(cfg.get("circuit") or "").lower() == wanted
        )
        return in_circuit[0][1] if in_circuit else None

    async def async_refresh_schedules(self, schedule: str | None = None) -> None:
        """Fetch one programme, or all of them, from the controller."""
        if not self.profile or not self.profile.schedules:
            return

        resolved = self.resolve_schedule(schedule)
        keys = [resolved] if resolved else list(self.profile.schedules)

        for key in keys:
            if key in self._unsupported_schedules:
                continue
            cfg = self.profile.schedules[key]
            base_addr = parse_address(cfg["address"])
            block_length = cfg.get("block_length")
            block_factor = cfg.get("block_factor")
            fc = optolink.function_code(cfg.get("fc_read"))
            try:
                if (
                    block_length
                    and block_factor
                    and key not in self._schedule_record_step
                ):
                    # Determine once, from the controller, whether this datapoint's address
                    # counts records or bytes; the catalog does not record which
                    # (see optolink.read_block()).
                    self._schedule_record_step[
                        key
                    ] = await self.client.calibrate_record_step(
                        base_addr, block_length, block_factor, fc
                    )
                    _LOGGER.info(
                        "Programme %s at 0x%04X uses %s address stepping",
                        key,
                        base_addr,
                        self._schedule_record_step[key],
                    )
                self.schedules[key] = await self.client.read_circuit_schedule(
                    base_addr,
                    day_bytes=cfg["day_bytes"],
                    fmt=cfg["format"],
                    default_level=cfg.get("default_level", 0),
                    block_length=block_length,
                    block_factor=block_factor,
                    record_step=self._schedule_record_step.get(key, "record"),
                    fc=fc,
                )
                _LOGGER.debug("Refreshed programme %s (0x%04X)", key, base_addr)
            except optolink.OptolinkDeviceError as err:
                if err.is_permanent:
                    self._unsupported_schedules.add(key)
                    _LOGGER.info(
                        "Programme %s (0x%04X) is not implemented on this controller",
                        key,
                        base_addr,
                    )
                else:
                    _LOGGER.warning("Could not refresh programme %s: %s", key, err)
            except Exception as err:
                _LOGGER.warning("Could not refresh programme %s: %s", key, err)

        self.async_update_listeners()

    async def async_refresh_error_history(self) -> None:
        """Fetch and decode the controller's error-history buffer.

        Everything comes from the catalog entry resolved at setup (address, size, element
        count, wire function code) -- no address probing and no System-ID branching. The
        heat-pump buffers are RPC arrays that must be fetched as N indexed sub-reads; the
        boiler buffer is ordinary chunked memory. `read_error_history()` dispatches on fc_read.
        """
        dp = self._error_history_dp
        if not dp:
            return

        try:
            fc = (
                optolink.FC_RPC
                if dp["fc_read"] == "Remote_Procedure_Call"
                else optolink.FC_VIRTUAL_READ
            )
            raw = await self.client.read_error_history(
                dp["address"],
                total_bytes=dp["total_bytes"],
                block_factor=dp["block_factor"],
                function_code=fc,
            )
            if not raw:
                _LOGGER.warning(
                    "Error history read at 0x%04X returned no data", dp["address"]
                )
                return

            # Both layouts put the fault code at entry byte 5 and terminate on 0x00; the WP
            # family stores a little-endian epoch at bytes 1..4 while the boiler stores BCD.
            if dp["fc_read"] == "Remote_Procedure_Call":
                entries = decode_wp_error_history(
                    raw, entry_bytes=dp["entry_bytes"], error_codes=self._error_codes
                )
            else:
                entries = decode_boiler_error_history(
                    raw, entry_bytes=dp["entry_bytes"], error_codes=self._error_codes
                )

            self.error_history = entries
            self.last_error = entries[0] if entries else None
            _LOGGER.debug(
                "Refreshed error history from %s (0x%04X): %d entries",
                dp["name"],
                dp["address"],
                len(entries),
            )
            self.async_update_listeners()
        except Exception as err:
            _LOGGER.warning(
                "Could not refresh error history at 0x%04X: %s", dp["address"], err
            )

    @staticmethod
    def _day_index(day: Any) -> int:
        if isinstance(day, int):
            idx = day
        else:
            idx = DAY_INDEX.get(str(day).lower().strip())
        if idx is None or not 0 <= idx <= 6:
            raise ValueError(f"Invalid day specifier: {day}")
        return idx

    async def async_set_day_schedule(
        self,
        schedule: str,
        day: Any,
        windows: list[dict[str, Any]],
    ) -> bool:
        """Write one programme's day, or several days at once, and re-read the programme.

        `day` may be a single day or a list of them. The controller has no notion of grouped
        days -- it stores seven independent days, and any "Mon-Fri" grouping is a display
        convention over days whose contents happen to match -- so applying one day's windows
        to a whole week means writing each day. Days that already hold exactly these bytes are
        skipped, which is what makes "apply to the whole week" cheap on a bus this slow.

        The re-read at the end is deliberate: the days go out as record telegrams, and the
        controller rounds times to its grid and drops windows it considers invalid, so what it
        now holds is the only trustworthy picture.
        """
        key = self.resolve_schedule(schedule)
        if key is None:
            raise ValueError(f"'{schedule}' is not a programme of this controller")
        requested = day if isinstance(day, (list, tuple, set)) else [day]
        day_indexes = sorted({self._day_index(d) for d in requested})
        if not day_indexes:
            raise ValueError("No day given")

        cfg = self.profile.schedules[key]
        limit = cfg.get("windows_per_day", 8)
        if len(windows) > limit:
            raise ValueError(f"{key} allows at most {limit} switching windows per day")

        base_addr = parse_address(cfg["address"])
        fmt = cfg["format"]
        default_level = cfg.get("default_level", 0)
        levels = cfg.get("levels")
        raw = encode_day_schedule(windows, fmt, default_level, levels)

        written: list[str] = []
        for day_idx in day_indexes:
            day_name = DAYS[day_idx]
            current = self.schedules.get(key, {}).get(day_name)
            if (
                current is not None
                and encode_day_schedule(current, fmt, default_level, levels) == raw
            ):
                continue
            _LOGGER.info(
                "Writing programme %s day %s (%s): %s", key, day_name, fmt, raw.hex(" ")
            )
            await self.client.write_day_schedule(
                base_addr,
                day_idx,
                raw,
                day_bytes=cfg["day_bytes"],
                block_length=cfg.get("block_length"),
                block_factor=cfg.get("block_factor"),
                record_step=self._schedule_record_step.get(key, "record"),
                fc=optolink.function_code(
                    cfg.get("fc_write"), optolink.FC_VIRTUAL_WRITE
                ),
            )
            written.append(day_name)
            # Show the intended result at once; the re-read below replaces it with the
            # controller's own. Replace the programme's dict rather than mutating it: the
            # entity's published attributes still reference the old one, and Home Assistant
            # decides whether to fire a state change by comparing the two. Mutating in place
            # makes it compare an object with itself, so nothing is published and the card
            # keeps showing the previous programme however often this runs.
            self.schedules[key] = {
                **self.schedules.get(key, {}),
                day_name: decode_day_schedule(raw, fmt, default_level),
            }

        if not written:
            _LOGGER.info(
                "Programme %s already holds these windows on every requested day", key
            )
            return True

        self.async_update_listeners()
        await self.async_refresh_schedules(key)

        # What the controller now holds is the only truth. One retry covers a unit that needs
        # a moment to commit; beyond that, say so rather than leaving a card that silently
        # disagrees with the appliance.
        expected = decode_day_schedule(raw, fmt, default_level)
        for attempt in range(2):
            mismatched = [
                d for d in written if self.schedules.get(key, {}).get(d) != expected
            ]
            if not mismatched:
                return True
            if attempt == 0:
                await asyncio.sleep(1.0)
                await self.async_refresh_schedules(key)
        _LOGGER.warning(
            "Programme %s: the controller did not take %s. Wrote %s, it reports %s",
            key,
            ", ".join(mismatched),
            raw.hex(" "),
            self.schedules.get(key, {}).get(mismatched[0]),
        )
        return True

    async def async_set_schedule_window(
        self,
        schedule: str,
        day: Any,
        window: int,
        start: str,
        end: str,
        mode: Any = None,
    ) -> bool:
        """Update or add one switching window of one day."""
        key = self.resolve_schedule(schedule)
        if key is None:
            raise ValueError(f"'{schedule}' is not a programme of this controller")
        day_idx = self._day_index(day)
        cfg = self.profile.schedules[key]
        levels = cfg.get("levels") or [0, 1]
        default_level = cfg.get("default_level", 0)
        if mode is None or mode == "":
            # A window without a level means "switched on": the programme's usual active
            # level (2 = normal where that exists), else the first level that is not "off".
            active = [lv for lv in levels if lv != default_level]
            mode = 2 if 2 in active else (active[0] if active else default_level)
        level = parse_level(mode, levels, default_level)

        day_name = DAYS[day_idx]
        current = [dict(w) for w in self.schedules.get(key, {}).get(day_name, [])]
        new_window = {"window": window, "start": start, "end": end, "mode": level}
        if 0 <= window - 1 < len(current):
            current[window - 1] = new_window
        else:
            current.append(new_window)
        return await self.async_set_day_schedule(key, day_idx, current)


@dataclass
class OptolinkRuntime:
    """What a loaded config entry holds. Stored on `entry.runtime_data`."""

    client: OptolinkClient
    coordinator: OptolinkCoordinator
    # The options the entry was set up with, so the update listener can tell an options
    # change (reload) from the entry data this integration writes to itself while running.
    options: dict[str, Any]


OptolinkConfigEntry = ConfigEntry[OptolinkRuntime]
