"""Sensor platform for OptoV integration."""

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import catalog_db
from .conversions import DAYS, snap
from .coordinator import OptolinkConfigEntry, OptolinkCoordinator
from .entity import OptolinkEntity
from .profiles import parse_address
from .translate import async_ui_text

# Polling is the coordinator's; nothing here refreshes on its own.
PARALLEL_UPDATES = 0

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptolinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator

    entities = [
        OptolinkSensor(coordinator, entry, sensor_def)
        for sensor_def in coordinator.profile.sensors
    ]

    # One schedule sensor per circuit that has a weekly programme in the catalog
    for key, sched_cfg in coordinator.profile.schedules.items():
        entities.append(OptolinkScheduleSensor(coordinator, entry, key, sched_cfg))

    # Diagnostic bus communication sensors under Optolink Gateway
    entities.append(OptolinkBusLoadSensor(coordinator, entry))
    entities.append(OptolinkPollDurationSensor(coordinator, entry))
    entities.append(OptolinkActiveChannelsSensor(coordinator, entry))
    entities.append(OptolinkLatencySensor(coordinator, entry))
    entities.append(OptolinkDatapointRateSensor(coordinator, entry))
    entities.append(
        OptolinkCatalogSensor(
            coordinator,
            await hass.async_add_executor_job(
                catalog_db.catalog_info, coordinator.db_path
            ),
        )
    )

    # Error history sensor under main heating controller
    entities.append(
        OptolinkErrorHistorySensor(
            coordinator, entry, await async_ui_text(hass, "no_faults")
        )
    )

    # A stable entity id, so a reinstall lands on the same ids and keeps its history.
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "sensor.{}", entity.object_id_hint, hass=hass
        )

    async_add_entities(entities)


class OptolinkSensor(OptolinkEntity, SensorEntity):
    """A reading. Unit, device class and enumeration texts all come from the catalog."""

    def __init__(self, coordinator, entry, definition) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_native_unit_of_measurement = definition.get("unit")
        self._attr_device_class = definition.get("device_class")
        self._attr_state_class = definition.get("state_class")
        self._attr_suggested_display_precision = definition.get("display_precision")
        if "options" in definition:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = list(definition["options"].values())
        # The controller's clock is published as its drift from Home Assistant, not as the
        # time it shows. The time is a new state every poll -- thousands of recorder rows a
        # day that all say "one poll later" -- while the drift is what anyone acts on, and it
        # only changes when the clock does. Snapped to five seconds, so the second the poll
        # happens to land on does not count as a change either. The time itself is
        # now + drift; the sync service and the DST check read it directly.
        clock = coordinator.clock_datapoint()
        self._is_clock = clock is not None and clock["id"] == definition["id"]
        if self._is_clock:
            self._attr_native_unit_of_measurement = "s"
            self._attr_device_class = None
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_suggested_display_precision = 0
            self._attr_icon = "mdi:clock-check-outline"

    @property
    def native_value(self) -> Any:
        """The reading, or for an enumeration its text.

        A controller occasionally answers an enumeration with a code the catalog does not
        name -- the catalog covers a family, and firmware adds states to it. Home Assistant
        rejects a state outside the declared options outright, which loses the reading and
        raises on every poll, so the unknown code is added to the options instead and shown
        as itself.
        """
        if self._is_clock:
            return snap(self.coordinator.clock_drift, 5)
        value = self.raw_value
        options = getattr(self, "_attr_options", None)
        if options is None or value is None:
            return value
        text = str(value)
        if text not in options:
            _LOGGER.debug(
                "%s answered %s, which the catalog does not name; showing the raw code",
                self._def["name"],
                text,
            )
            self._attr_options = [*options, text]
        return text

    @property
    def available(self) -> bool:
        """Unavailable when the controller's own sensor-health code reports a fault.

        The health code (short circuit, open circuit, sensor missing, ...) rides along in the
        same block as the value, so every reading comes with it for free and no separate
        diagnostic entity per sensor is needed.
        """
        if not super().available:
            return False
        raw_code = self.coordinator.sensor_status_raw.get(self._def["id"])
        if raw_code is not None:
            return raw_code == 0
        status = self.coordinator.sensor_status.get(self._def["id"])
        if status is None:
            return True
        return str(status).strip().lower() in ("sensor ok", "ok", "0")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = dict(super().extra_state_attributes)
        status = self.coordinator.sensor_status.get(self._def["id"])
        if status is not None:
            attrs["sensor_status"] = status
        if self._is_clock:
            # Whether the controller's daylight-saving settings agree with this time zone,
            # and when its clock was last set. Both change rarely, so neither costs a recorder
            # row per poll; the controller's own time deliberately is not here, see __init__.
            if self.coordinator.dst_status:
                attrs["daylight_saving"] = self.coordinator.dst_status
            attrs["last_corrected"] = self.coordinator.clock_corrected
        return attrs


class OptolinkScheduleSensor(CoordinatorEntity[OptolinkCoordinator], SensorEntity):
    """One weekly programme, published in a form a frontend can edit without assumptions.

    The state is the number of days with at least one switching window. The attributes
    carry everything an editor needs; all of it comes from the catalog:

      schedule         key the schedule services take
      name             the programme's catalog name in the configured language
      circuit          circuit the programme belongs to, and its label in circuit_name
      device_name      the controller, for a heading
      format           phase2 (start, end), phase3 (start, end, level) or bitmap
      windows_per_day  slots per day
      minutes_per_step time grid
      modes            selectable levels as {value, label, color}, both from the catalog
                       per programme; the bundled card uses the labels but draws in Home
                       Assistant theme colours and by bar height
      default_level    the level that means "nothing scheduled"
      days             day tokens in controller order
      weekly_schedule  {day: [{window, start, end, mode}, ...]}

    Until the first successful read the state is unknown, which is how a frontend tells a
    programme that exists from one that has not been fetched yet.
    """

    _attr_has_entity_name = False
    _attr_icon = "mdi:calendar-clock"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: OptolinkCoordinator,
        entry: ConfigEntry,
        key: str,
        sched_cfg: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._key = key
        self._cfg = sched_cfg
        self._attr_name = sched_cfg.get("name") or key
        self._attr_unique_id = f"{coordinator.stable_id}_schaltzeiten_{key}"
        self._attr_device_info = coordinator.get_device_info(sched_cfg.get("circuit"))
        self._base_address = parse_address(sched_cfg["address"])

    @property
    def object_id_hint(self) -> str:
        """Entity id built from the model rather than the device's display name."""
        return self.coordinator.object_id(
            self._cfg.get("circuit"), self._cfg.get("name") or self._key
        )

    @property
    def native_value(self) -> int | None:
        """Days with at least one switching window, or unknown before the first read."""
        sched = self.coordinator.schedules.get(self._key)
        if not sched:
            return None
        return sum(1 for windows in sched.values() if windows)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Everything an editor needs, see the class docstring."""
        cfg = self._cfg
        return {
            "schedule": self._key,
            "name": cfg.get("name"),
            "circuit": cfg.get("circuit"),
            "circuit_name": cfg.get("circuit_name"),
            "device_name": self.coordinator.profile.device_name
            if self.coordinator.profile
            else None,
            "format": cfg.get("format"),
            "windows_per_day": cfg.get("windows_per_day"),
            "minutes_per_step": cfg.get("minutes_per_step", 10),
            "modes": cfg.get("modes", []),
            "default_level": cfg.get("default_level", 0),
            "days": list(DAYS),
            "base_address": f"0x{self._base_address:04X}",
            # A copy: the attributes of the state Home Assistant has already published must
            # never share structure with the coordinator, or an update to one silently edits
            # the other and the comparison that decides whether to publish sees no change.
            "weekly_schedule": {
                day: [dict(window) for window in windows]
                for day, windows in self.coordinator.schedules.get(
                    self._key, {}
                ).items()
            },
        }


class OptolinkBusLoadSensor(CoordinatorEntity[OptolinkCoordinator], SensorEntity):
    """The share of each poll interval spent talking to the controller.

    The bus sensors below publish on a coarse grid (whole percent, half a second, five
    milliseconds) rather than what the coordinator measured. A sweep never takes exactly as
    long twice, and Home Assistant writes a recorder row for every change, so full precision
    turned five diagnostics into a fifth of the integration's database writes while telling
    nobody anything. The exact figures are in the diagnostics download.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "bus_load"
    _attr_icon = "mdi:speedometer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OptolinkCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stable_id}_bus_load"
        self._attr_device_info = coordinator.get_gateway_device_info()

    @property
    def object_id_hint(self) -> str:
        """Entity id built from the model rather than the device's display name."""
        return "optov bus load"

    @property
    def native_value(self) -> Any:
        """Duty cycle in whole percent."""
        return snap(self.coordinator.bus_load, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the load is made of, limited to figures that hold still between polls.

        Attributes count as state: a changed attribute is a recorder row just like a changed
        value, so the per-sweep counters (bytes, telegrams, wire share) are not here. They are
        in the diagnostics download, exact.
        """
        return {
            "scan_interval_seconds": self.coordinator.scan_interval_seconds,
            "active_channels_count": self.coordinator.active_channels,
            "telegrams_failed": self.coordinator.telegrams_failed,
        }


class OptolinkPollDurationSensor(CoordinatorEntity[OptolinkCoordinator], SensorEntity):
    """Representation of the total polling cycle duration."""

    _attr_has_entity_name = True
    _attr_translation_key = "poll_duration"
    _attr_icon = "mdi:timer-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = "s"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OptolinkCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stable_id}_poll_duration"
        self._attr_device_info = coordinator.get_gateway_device_info()

    @property
    def object_id_hint(self) -> str:
        """Entity id built from the model rather than the device's display name."""
        return "optov poll duration"

    @property
    def native_value(self) -> Any:
        """Sweep duration on a half-second grid, see OptolinkBusLoadSensor."""
        return snap(self.coordinator.poll_duration, 0.5)


class OptolinkActiveChannelsSensor(
    CoordinatorEntity[OptolinkCoordinator], SensorEntity
):
    """How many datapoints were read in the last cycle.

    Deliberately without a unit. Long-term statistics are keyed by unit of measurement, so a
    translated one ("channels", "Kanäle") breaks the series the moment the Home Assistant
    language changes, and Home Assistant then refuses to compile statistics until the unit is
    put back. A count does not need one.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "active_channels"
    _attr_icon = "mdi:format-list-numbered"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OptolinkCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stable_id}_active_channels"
        self._attr_device_info = coordinator.get_gateway_device_info()

    @property
    def object_id_hint(self) -> str:
        """Entity id built from the model rather than the device's display name."""
        return "optov polled datapoints"

    @property
    def native_value(self) -> Any:
        """Return number of active polled channels."""
        return self.coordinator.active_channels


class OptolinkLatencySensor(CoordinatorEntity[OptolinkCoordinator], SensorEntity):
    """Representation of average per-telegram round-trip response time."""

    _attr_has_entity_name = True
    _attr_translation_key = "avg_response_time"
    _attr_icon = "mdi:timer-sand"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OptolinkCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stable_id}_avg_response_time"
        self._attr_device_info = coordinator.get_gateway_device_info()

    @property
    def object_id_hint(self) -> str:
        """Entity id built from the model rather than the device's display name."""
        return "optov telegram round trip"

    @property
    def native_value(self) -> Any:
        """Average round trip on a five-millisecond grid, see OptolinkBusLoadSensor."""
        return snap(self.coordinator.avg_response_time_ms, 5)


class OptolinkCatalogSensor(SensorEntity):
    """Which catalog this controller runs on, and which version of the catalog format it has.

    The state is the catalog's structure version, the number this integration checks against
    `catalog_db.CATALOG_SCHEMA_VERSION` at every start. The attributes say which file it is and
    how many controllers and which languages it covers.

    Read once at setup: a catalog only changes with a reload. It describes the catalog, not the
    controller, so it stays available while the controller does not answer.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "catalog"
    _attr_icon = "mdi:database-cog"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, coordinator: OptolinkCoordinator, info: dict[str, Any]) -> None:
        self._info = info
        self._attr_unique_id = f"{coordinator.stable_id}_catalog"
        self._attr_device_info = coordinator.get_gateway_device_info()

    @property
    def object_id_hint(self) -> str:
        """Entity id built from the model rather than the device's display name."""
        return "optov catalog"

    @property
    def native_value(self) -> Any:
        """The catalog's structure version."""
        return self._info["meta"].get("schema_version")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "file": self._info["file"],
            "controllers": self._info["controllers"],
            "languages": self._info["languages"],
        }


class OptolinkDatapointRateSensor(CoordinatorEntity[OptolinkCoordinator], SensorEntity):
    """How many datapoints a second the sweep actually delivers.

    Telegrams per second was the obvious measure while every datapoint cost one telegram. Now
    that neighbouring datapoints are fetched together it is the wrong way round: the better
    the merging, the fewer telegrams a second, so the number falls as throughput rises. What
    matters is readings delivered, which is what this counts. The telegram figure is still on
    the bus-load sensor for anyone diagnosing the link itself.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "datapoint_rate"
    _attr_icon = "mdi:transfer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "dp/s"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: OptolinkCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stable_id}_datapoint_rate"
        self._attr_device_info = coordinator.get_gateway_device_info()

    @property
    def object_id_hint(self) -> str:
        """Entity id built from the model rather than the device's display name."""
        return "optov datapoint rate"

    @property
    def native_value(self) -> Any:
        """Datapoints a second, whole numbers, see OptolinkBusLoadSensor."""
        return snap(self.coordinator.datapoint_rate, 1)


class OptolinkErrorHistorySensor(CoordinatorEntity[OptolinkCoordinator], SensorEntity):
    """The controller's logged fault history (most recent entry as state).

    Deliberately NOT EntityCategory.DIAGNOSTIC: Home Assistant hides diagnostic entities in a
    collapsed section of the device page and keeps them off auto-generated dashboards, which
    made this effectively invisible. A fault log is primary information, so it stays a normal
    entity.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "error_history"
    _attr_icon = "mdi:history"

    def __init__(
        self, coordinator: OptolinkCoordinator, entry: ConfigEntry, empty_text: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stable_id}_fehlerhistorie"
        self._attr_device_info = coordinator.get_device_info(None)
        # The one state this integration words itself; every other state is catalog text.
        # Taken from the integration's translations in the user's Home Assistant language.
        self._empty_text = empty_text

    @property
    def object_id_hint(self) -> str:
        """Entity id built from the model rather than the device's display name."""
        return self.coordinator.object_id(None, "fault history")

    @property
    def native_value(self) -> str:
        """Newest logged entry, prefixed with its date.

        The date matters: this is a *history* buffer, so the newest entry is often years old
        and is not necessarily an active fault. Without the date the state reads like a
        current alarm. HA truncates states at 255 chars; the full list is in `entries`.
        """
        if not self.coordinator.error_history:
            return self._empty_text
        latest = self.coordinator.error_history[0]
        desc = latest.get("description") or latest.get("code", "")
        stamp = (latest.get("timestamp_str") or "").split(" ")[0]
        return (f"{stamp} - {desc}" if stamp else desc)[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the decoded history plus the catalog metadata it was read with."""
        dp = self.coordinator._error_history_dp or {}
        entries = self.coordinator.error_history
        return {
            # The controller this belongs to, so a card can head its list with it.
            "device_name": self.coordinator.profile.device_name
            if self.coordinator.profile
            else None,
            "error_count": len(entries),
            "last_error_code": (entries[0].get("code") if entries else None),
            "last_error_time": (entries[0].get("timestamp_str") if entries else None),
            "entries": entries,
            # Provenance: which catalog datapoint this came from and how it was read.
            "buffer_name": dp.get("name"),
            "buffer_address": f"0x{dp['address']:04X}"
            if dp.get("address") is not None
            else None,
            "buffer_bytes": dp.get("total_bytes"),
            "buffer_entries": dp.get("block_factor"),
            "entry_bytes": dp.get("entry_bytes"),
            "read_function": dp.get("fc_read"),
            "known_codes": len(self.coordinator._error_codes),
        }
