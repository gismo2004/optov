"""Water heaters the user assembled from existing entities (custom_entities.py).

Like the thermostats in climate.py: no datapoint of its own, the states of the entities it was
built from, and writes through Home Assistant's own services.
"""

from typing import Any

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_OFF,
    STATE_ON,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OptolinkConfigEntry
from .custom_assembled import AssembledEntity
from .custom_entities import KEY_MODE, MODES, SECTION_WATER_HEATER

# Writes go one at a time, as for every other control of the controller.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptolinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = [
        OptolinkCustomWaterHeater(coordinator, cfg)
        for cfg in coordinator.custom[SECTION_WATER_HEATER]
    ]
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "water_heater.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)


class OptolinkCustomWaterHeater(AssembledEntity, WaterHeaterEntity):
    """A water heater: setpoint, current temperature, and optionally its operating mode.

    Only Home Assistant's own water heater features, each mapped to an entity: target
    temperature, current temperature, operation mode (a select whose values the user maps to the
    standard modes) and on/off from the same mapping. Without a mode it reports "on", one of the
    standard water heater states.
    """

    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, cfg: dict[str, Any]) -> None:
        super().__init__(coordinator, cfg)
        features = WaterHeaterEntityFeature.TARGET_TEMPERATURE
        if cfg.get(KEY_MODE):
            features |= WaterHeaterEntityFeature.OPERATION_MODE
            self._attr_operation_list = [
                m for m in MODES[SECTION_WATER_HEATER] if m in self.mapped_modes
            ]
            # On and off are Home Assistant's own water heater feature; they come from the
            # same mapping: off sets the "off" mode, on the first other one.
            if (
                STATE_OFF in self._attr_operation_list
                and len(self._attr_operation_list) > 1
            ):
                features |= WaterHeaterEntityFeature.ON_OFF
        self._attr_supported_features = features

    @property
    def current_operation(self) -> str | None:
        # A water heater's state is its operation. Without a mode select there is none to
        # report, and "unknown" would read like a fault; it is simply running.
        if not self._cfg.get(KEY_MODE):
            return STATE_ON
        return self.current_mode

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.async_set_target(temperature)

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        await self.async_set_mode(operation_mode)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.async_set_mode(STATE_OFF)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.async_set_mode(
            next(m for m in self._attr_operation_list if m != STATE_OFF)
        )
