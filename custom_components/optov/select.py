"""Selects: writable datapoints with an enumeration of named values."""

from homeassistant.components.select import SelectEntity
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
        OptolinkSelect(coordinator, entry, definition)
        for definition in coordinator.profile.selects
    ]
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "select.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)


class OptolinkSelect(OptolinkEntity, SelectEntity):
    """A setting chosen from the names the catalog gives its values."""

    def __init__(self, coordinator, entry, definition) -> None:
        super().__init__(coordinator, entry, definition)
        options = definition.get("options", {})
        self._attr_options = list(options.values())
        self._value_of = {label: value for value, label in options.items()}

    @property
    def current_option(self) -> str | None:
        # The coordinator publishes the label. A code the catalog does not name is left
        # unselected rather than shown as a number that is not one of the options.
        label = self.raw_value
        return label if label in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        if option not in self._value_of:
            raise ValueError(f"{option!r} is not an option of {self.entity_id}")
        await self.coordinator.async_write_item(self._def, int(self._value_of[option]))
