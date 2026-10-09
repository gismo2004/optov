"""Dates: writable calendar dates, a holiday's first and last day above all."""

from datetime import date, timedelta

from homeassistant.components.date import DateEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import OptolinkConfigEntry
from .decode import DecodeError, encode_date
from .entity import OptolinkEntity

# Writes go one at a time, as for every other setting.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptolinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = [
        OptolinkDate(coordinator, entry, definition)
        for definition in coordinator.profile.dates
    ]
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "date.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)


class OptolinkDate(OptolinkEntity, DateEntity):
    """A date setting. Unknown while the controller holds no date there."""

    @property
    def native_value(self) -> date | None:
        value = self.raw_value
        return date.fromisoformat(value) if value else None

    async def async_set_value(self, value: date) -> None:
        conversion = self._def.get("conversion")
        width = self._def["bytes"]
        try:
            field = encode_date(value, conversion, width)
        except DecodeError as err:
            # Only a day count runs out of room: two bytes of days end in 2149.
            first = date(1970, 1, 1)
            last = first + timedelta(days=(1 << (8 * min(width, 3))) - 1)
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="date_out_of_range",
                translation_placeholders={
                    "date": value.isoformat(),
                    "first": first.isoformat(),
                    "last": last.isoformat(),
                },
            ) from err
        await self.coordinator.async_write_item(self._def, field)
