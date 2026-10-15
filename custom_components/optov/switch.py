"""Switches: writable single-bit datapoints.

A datapoint lands here when the catalog says it is writable and exactly one bit wide, which
leaves it two states and nothing else to say about them. Party mode, eco mode and the one-off
hot water run are all of that shape. A writable setting with two named values comes in as a
select instead; the user can add an on/off entity for it and say which value is "on".
"""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OptolinkConfigEntry
from .custom_entities import KEY_DEVICE_CLASS, SECTION_ONOFF, off_value
from .entity import OptolinkEntity, OptolinkOnOffEntity

# Writes go one at a time: the controller sits on a serial line and the client queues
# telegrams anyway, so more than one in flight would only wait on the other.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptolinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = [
        OptolinkSwitch(coordinator, definition)
        for definition in coordinator.profile.switches
    ]
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "switch.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)

    # On/off entities the user added (custom_entities.py); this platform takes its kind.
    onoff = [
        OptolinkOnOffSwitch(coordinator, entity)
        for entity in coordinator.custom[SECTION_ONOFF]
        if entity["platform"] == "selects"
    ]
    for entity in onoff:
        entity.entity_id = async_generate_entity_id(
            "switch.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(onoff)


class OptolinkSwitch(OptolinkEntity, SwitchEntity):
    """One writable bit on the controller. The coordinator publishes the bit itself, 0 or 1."""

    @property
    def is_on(self) -> bool | None:
        value = self.raw_value
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_write_item(self._def, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_write_item(self._def, 0)


class OptolinkOnOffSwitch(OptolinkOnOffEntity, SwitchEntity):
    """On/off control of a two-state setting; writes the raw value the user chose as "on"."""

    def __init__(self, coordinator, entity) -> None:
        super().__init__(coordinator, entity)
        self._off_value = off_value(self._def, self._on_value)
        self._attr_device_class = entity[KEY_DEVICE_CLASS] or "switch"

    # A write only when the state actually changes: every write is a telegram, and for a
    # stored setting a write cycle of the controller's memory.
    async def async_turn_on(self, **kwargs: Any) -> None:
        if self.is_on is not True:
            await self.coordinator.async_write_item(self._def, self._on_value)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self.is_on is not False:
            await self.coordinator.async_write_item(self._def, self._off_value)
