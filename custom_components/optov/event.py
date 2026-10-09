"""Events: a new entry in a fault history, as something an automation can trigger on.

The fault history sensors show the log; this says the moment it grows. One event entity per
fault buffer -- the controller's own, and on boilers that have one the burner automat's.
"""

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import OptolinkConfigEntry, OptolinkCoordinator

PARALLEL_UPDATES = 0

EVENT_FAULT = "fault"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptolinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities: list[OptolinkFaultEvent] = []
    if coordinator._error_history_dp:
        entities.append(OptolinkFaultEvent(coordinator, entry))
    if coordinator._gfa_dp:
        entities.append(OptolinkBurnerFaultEvent(coordinator, entry))
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "event.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)


class OptolinkFaultEvent(CoordinatorEntity[OptolinkCoordinator], EventEntity):
    """Fires `fault` when the newest entry of the controller's fault history changes.

    What counts is the newest entry's code and the time it was logged, so a fault that comes
    back with the same code is still a new event. The first reading after the entity is
    created only sets the starting point: a history that already holds years of entries must
    not fire for the most recent of them. After a restart the last event's entry is the
    starting point, so a fault logged while Home Assistant was down still fires once the
    history has been read.

    A history that empties (cleared at the controller) or whose newest entry turns out older
    than the last one seen fires nothing; it only moves the starting point.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "fault"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_event_types = [EVENT_FAULT]
    _unique_suffix = "fault_event"
    _object_id_words = "fault"

    def __init__(self, coordinator: OptolinkCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.stable_id}_{self._unique_suffix}"
        self._attr_device_info = coordinator.get_device_info(None)
        self._seen: tuple[str, str] | None = None
        self._have_start = False

    @property
    def object_id_hint(self) -> str:
        """Entity id built from the model rather than the device's display name."""
        return self.coordinator.object_id(None, self._object_id_words)

    def _history(self) -> tuple[bool, list[dict[str, Any]]]:
        """Whether the buffer has been read yet, and its entries, newest first."""
        return self.coordinator._error_history_loaded, self.coordinator.error_history

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            code = last.attributes.get("code")
            logged_at = last.attributes.get("logged_at")
            if code is not None:
                self._seen = (str(code), str(logged_at or ""))
            # An entity that existed before but never fired has a state and no entry: its
            # start is whatever the history holds when it is first read, as for a new one.
            self._have_start = self._seen is not None
        self._handle_coordinator_update()

    @callback
    def _handle_coordinator_update(self) -> None:
        loaded, entries = self._history()
        if not loaded or not entries:
            # Nothing read yet, or a history with no entries: nothing to compare.
            return
        newest = entries[0]
        current = (str(newest.get("code", "")), str(newest.get("timestamp") or ""))
        if not self._have_start:
            self._seen, self._have_start = current, True
            return
        if current == self._seen:
            return
        previous, self._seen = self._seen, current
        # Timestamps of one buffer share one format and sort as text; a history that went
        # back in time was cleared or rewritten, not extended.
        if previous and current[1] and previous[1] and current[1] < previous[1]:
            return
        self._trigger_event(
            EVENT_FAULT,
            {
                "code": current[0],
                "description": newest.get("description"),
                "logged_at": current[1],
            },
        )
        self.async_write_ha_state()


class OptolinkBurnerFaultEvent(OptolinkFaultEvent):
    """The same for the burner automat's own fault history."""

    _attr_translation_key = "burner_fault"
    _attr_icon = "mdi:fire-alert"
    _unique_suffix = "fault_event_fa"
    _object_id_words = "burner fault"

    def _history(self) -> tuple[bool, list[dict[str, Any]]]:
        return self.coordinator._gfa_loaded, self.coordinator.gfa_error_history
