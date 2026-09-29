"""Numbers: writable numeric datapoints."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OptolinkConfigEntry
from .decode import unscale
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
        OptolinkNumber(coordinator, entry, definition)
        for definition in coordinator.profile.numbers
    ]
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "number.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)


class OptolinkNumber(OptolinkEntity, NumberEntity):
    """A setting with a numeric value. Limits and step come from the catalog."""

    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, entry, definition) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_native_min_value = float(definition.get("min", 0.0))
        self._attr_native_max_value = float(definition.get("max", 100.0))
        self._attr_native_step = float(definition.get("step", 1.0))
        self._attr_native_unit_of_measurement = definition.get("unit")

    @property
    def native_value(self):
        return self.raw_value

    async def async_set_native_value(self, value: float) -> None:
        # The controller stores the raw integer; the conversion that scales it for display is
        # undone: a 0.1-degree setpoint of 21.5 is written as 215, a x5 delay of 750 as 150.
        raw = unscale(
            value,
            self._def.get("conversion"),
            self._def.get("conversion_factor"),
            self._def.get("conversion_offset"),
        )
        await self.coordinator.async_write_item(self._def, raw)
