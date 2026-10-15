"""Thermostats the user assembled from existing entities (custom_entities.py).

A thermostat here owns no datapoint. It shows the states of the entities it was built from and
sets them through Home Assistant's own services, so a write goes the same way, with the same
checks, as when the setpoint or the mode select is changed directly.
"""

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import async_generate_entity_id
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import OptolinkConfigEntry
from .custom_assembled import AssembledEntity
from .custom_entities import KEY_HEATING, MODES, PRESETS, SECTION_CLIMATE

# Writes go one at a time, as for every other control of the controller.
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OptolinkConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    entities = [
        OptolinkCustomClimate(coordinator, cfg)
        for cfg in coordinator.custom[SECTION_CLIMATE]
    ]
    for entity in entities:
        entity.entity_id = async_generate_entity_id(
            "climate.{}", entity.object_id_hint, hass=hass
        )
    async_add_entities(entities)


class OptolinkCustomClimate(AssembledEntity, ClimateEntity):
    """A thermostat: setpoint, operating mode, and optionally the current temperature."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator, cfg: dict[str, Any]) -> None:
        self._presets: dict[str, dict[str, Any]] = cfg.get("preset_on") or {}
        super().__init__(
            coordinator,
            cfg,
            extra=[
                cfg.get(KEY_HEATING),
                *(p["entity"] for p in self._presets.values()),
            ],
        )
        self._attr_hvac_modes = [
            HVACMode(m) for m in MODES[SECTION_CLIMATE] if m in self.mapped_modes
        ]
        features = ClimateEntityFeature.TARGET_TEMPERATURE
        if HVACMode.OFF in self._attr_hvac_modes:
            features |= ClimateEntityFeature.TURN_OFF
            if len(self._attr_hvac_modes) > 1:
                features |= ClimateEntityFeature.TURN_ON
        if self._presets:
            features |= ClimateEntityFeature.PRESET_MODE
            self._attr_preset_modes = [
                PRESET_NONE,
                *(p for p in PRESETS if p in self._presets),
            ]
        self._attr_supported_features = features

    @property
    def target_temperature_step(self) -> float | None:
        return self.target_step

    @property
    def hvac_mode(self) -> HVACMode | None:
        mode = self.current_mode
        return HVACMode(mode) if mode else None

    @property
    def hvac_action(self) -> HVACAction | None:
        heating = self._cfg.get(KEY_HEATING)
        if not heating:
            return None
        if self.hass.states.is_state(heating, "on"):
            return HVACAction.HEATING
        return HVACAction.OFF if self.hvac_mode == HVACMode.OFF else HVACAction.IDLE

    def _preset_is_on(self, preset: str) -> bool:
        target = self._presets[preset]
        state = self.hass.states.get(target["entity"])
        if state is None:
            return False
        if target["on_label"] is None:
            return state.state == "on"
        return state.state == target["on_label"]

    @property
    def preset_mode(self) -> str | None:
        if not self._presets:
            return None
        for preset in PRESETS:
            if preset in self._presets and self._preset_is_on(preset):
                return preset
        return PRESET_NONE

    async def _async_switch_preset(self, preset: str, on: bool) -> None:
        """Turn the entity behind a preset on or off, through its own service."""
        target = self._presets[preset]
        entity_id = target["entity"]
        if target["on_label"] is None:
            await self.hass.services.async_call(
                "switch",
                "turn_on" if on else "turn_off",
                {"entity_id": entity_id},
                blocking=True,
            )
            return
        state = self.hass.states.get(entity_id)
        options = list(state.attributes.get("options") or []) if state else []
        option = (
            target["on_label"]
            if on
            else next((o for o in options if o != target["on_label"]), None)
        )
        if option is not None:
            await self.hass.services.async_call(
                "select",
                "select_option",
                {"entity_id": entity_id, "option": option},
                blocking=True,
            )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Turn off the other presets that are on, then this one on; only what changes."""
        for preset in self._presets:
            if preset != preset_mode and self._preset_is_on(preset):
                await self._async_switch_preset(preset, False)
        if preset_mode in self._presets and not self._preset_is_on(preset_mode):
            await self._async_switch_preset(preset_mode, True)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if (mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(mode)
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.async_set_target(temperature)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.async_set_mode(str(hvac_mode))

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        for mode in (HVACMode.AUTO, HVACMode.HEAT, HVACMode.HEAT_COOL, HVACMode.COOL):
            if mode in self._attr_hvac_modes:
                await self.async_set_hvac_mode(mode)
                return
