"""Switches: writable single-bit datapoints.

A datapoint lands here when the catalog says it is writable and exactly one bit wide, which
leaves it two states and nothing else to say about them. Party mode, eco mode and the one-off
hot water run are all of that shape.
"""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OptolinkConfigEntry
from .entity import OptolinkEntity

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
        OptolinkSwitch(coordinator, entry, definition)
        for definition in coordinator.profile.switches
    ]
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "switch.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)


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
