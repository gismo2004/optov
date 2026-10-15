"""Binary sensors: read-only single-bit datapoints, and on/off entities of two-state readings."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OptolinkConfigEntry
from .custom_entities import KEY_DEVICE_CLASS, KEY_NAME, KEY_ON, is_on, onoff_unique_id
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
        for entity in coordinator.custom_onoff
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


class OptolinkOnOffBinarySensor(OptolinkEntity, BinarySensorEntity):
    """On/off view of a two-state reading; "on" is the state the user chose for it."""

    def __init__(self, coordinator, entity) -> None:
        definition = entity["item"]
        super().__init__(coordinator, definition)
        self._on_value = int(entity[KEY_ON])
        self._attr_unique_id = onoff_unique_id(coordinator.stable_id, definition["id"])
        self._attr_name = entity[KEY_NAME]
        # Custom entities sit together on their own sub-device rather than among the values.
        self._attr_device_info = coordinator.get_custom_device_info()
        device_class = entity[KEY_DEVICE_CLASS]
        self._attr_device_class = (
            None if device_class in (None, "none") else device_class
        )
        # The user asked for this one, so it is shown regardless of the source's tier.
        self._attr_entity_registry_enabled_default = True
        self._attr_entity_category = None

    @property
    def object_id_hint(self) -> str:
        return self.coordinator.object_id(self._def.get("circuit"), self._attr_name)

    @property
    def is_on(self) -> bool | None:
        return is_on(self.raw_value, self._def, self._on_value)
