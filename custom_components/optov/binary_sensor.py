"""Binary sensors: read-only single-bit datapoints, and on/off entities of two-state readings."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OptolinkConfigEntry
from .custom_entities import KEY_DEVICE_CLASS, SECTION_ONOFF
from .entity import OptolinkEntity, OptolinkOnOffEntity

# Polling is the coordinator's; nothing here refreshes on its own.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptolinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = [
        OptolinkBinarySensor(coordinator, definition)
        for definition in coordinator.profile.binary_sensors
    ]
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "binary_sensor.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)

    # On/off entities the user added (custom_entities.py); this platform takes its kind.
    onoff = [
        OptolinkOnOffBinarySensor(coordinator, entity)
        for entity in coordinator.custom[SECTION_ONOFF]
        if entity["platform"] == "sensors"
    ]
    for entity in onoff:
        entity.entity_id = async_generate_entity_id(
            "binary_sensor.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(onoff)


class OptolinkBinarySensor(OptolinkEntity, BinarySensorEntity):
    """A datapoint that is either set or not."""

    def __init__(self, coordinator, definition) -> None:
        super().__init__(coordinator, definition)
        self._attr_device_class = definition.get("device_class")

    @property
    def is_on(self) -> bool:
        return bool(self.raw_value)


class OptolinkOnOffBinarySensor(OptolinkOnOffEntity, BinarySensorEntity):
    """On/off view of a two-state reading; "on" is the state the user chose for it."""

    def __init__(self, coordinator, entity) -> None:
        super().__init__(coordinator, entity)
        device_class = entity[KEY_DEVICE_CLASS]
        self._attr_device_class = (
            None if device_class in (None, "none") else device_class
        )
