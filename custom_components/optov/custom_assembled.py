"""What a thermostat and a water heater assembled from other entities have in common.

They read the states of the entities they were built from and follow them through state-change
events, so they need no polling of their own and add no bus time. They set values through Home
Assistant's services, the same way the setpoint or mode select is set directly. A mode is mapped
by the select's raw value; the select publishes the value's name, which follows the catalog
language, so the name is looked up in the select's options each time.
"""

from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, callback
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change_event

from .custom_entities import KEY_CURRENT, KEY_MODE, KEY_MODES, KEY_TARGET, KEY_WRITE

_MISSING = (None, STATE_UNAVAILABLE, STATE_UNKNOWN)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AssembledEntity(Entity):
    """Shows and sets the entities a custom thermostat or water heater was built from."""

    _attr_has_entity_name = False
    _attr_should_poll = False

    def __init__(
        self, coordinator, cfg: dict[str, Any], extra: list | None = None
    ) -> None:
        self._cfg = cfg
        self._attr_unique_id = cfg["unique_id"]
        self._attr_device_info = coordinator.get_custom_device_info()
        self._watched = [
            entity_id
            for entity_id in (cfg[KEY_TARGET], cfg.get(KEY_CURRENT), cfg.get(KEY_MODE))
            + tuple(extra or ())
            if entity_id
        ]
        self.mapped_modes = set(cfg.get(KEY_MODES, {}).values())
        options = cfg.get("options") or {}
        self._raw_of_label = {label: raw for raw, label in options.items()}

    async def async_added_to_hass(self) -> None:
        @callback
        def _changed(event: Event[EventStateChangedData]) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_state_change_event(self.hass, self._watched, _changed)
        )

    def _state(self, entity_id: str | None):
        return self.hass.states.get(entity_id) if entity_id else None

    @property
    def available(self) -> bool:
        target = self._state(self._cfg[KEY_TARGET])
        if target is None or target.state in _MISSING:
            return False
        if self._cfg.get(KEY_MODE):
            mode = self._state(self._cfg[KEY_MODE])
            return mode is not None and mode.state not in _MISSING
        return True

    @property
    def current_temperature(self) -> float | None:
        current = self._state(self._cfg.get(KEY_CURRENT))
        return _float(current.state) if current else None

    @property
    def target_temperature(self) -> float | None:
        target = self._state(self._cfg[KEY_TARGET])
        return _float(target.state) if target else None

    def _target_attr(self, name: str, default: float) -> float:
        target = self._state(self._cfg[KEY_TARGET])
        value = _float(target.attributes.get(name)) if target else None
        return default if value is None else value

    @property
    def min_temp(self) -> float:
        return self._target_attr("min", 7)

    @property
    def max_temp(self) -> float:
        return self._target_attr("max", 35)

    @property
    def target_step(self) -> float:
        return self._target_attr("step", 1)

    @property
    def current_mode(self) -> str | None:
        """The Home Assistant mode the mode select's value maps to; None if unmapped."""
        mode = self._state(self._cfg.get(KEY_MODE))
        if mode is None:
            return None
        raw = self._raw_of_label.get(mode.state)
        return self._cfg[KEY_MODES].get(raw) if raw is not None else None

    async def async_set_target(self, value: float) -> None:
        target = self._cfg[KEY_TARGET]
        await self.hass.services.async_call(
            target.split(".")[0],
            "set_value",
            {"entity_id": target, "value": value},
            blocking=True,
        )

    async def async_set_mode(self, ha_mode: str) -> None:
        """Select the controller mode the user chose to write for this Home Assistant mode."""
        raw = self._cfg[KEY_WRITE].get(ha_mode)
        option = (self._cfg.get("options") or {}).get(raw)
        if option is None:
            raise ValueError(f"{ha_mode!r} is not a mode of {self.entity_id}")
        await self.hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": self._cfg[KEY_MODE], "option": option},
            blocking=True,
        )
