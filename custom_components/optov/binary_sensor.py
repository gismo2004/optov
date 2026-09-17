"""Binary sensors: read-only single-bit datapoints."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OptolinkConfigEntry
from .entity import OptolinkEntity

# Polling is the coordinator's; nothing here refreshes on its own.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptolinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = [
        OptolinkBinarySensor(coordinator, entry, definition)
        for definition in coordinator.profile.binary_sensors
    ]
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "binary_sensor.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)


class OptolinkBinarySensor(OptolinkEntity, BinarySensorEntity):
    """A datapoint that is either set or not."""

    def __init__(self, coordinator, entry, definition) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_device_class = definition.get("device_class")

    @property
    def is_on(self) -> bool:
        return bool(self.raw_value)
