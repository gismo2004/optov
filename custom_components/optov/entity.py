"""What every catalog-driven entity has in common.

One datapoint becomes one entity, and the platform only decides how it is presented. The
identity, device, category, icon, default enablement and the address attributes are the same
for a sensor, a number, a select and a switch, so they live here once.
"""

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import OptolinkCoordinator


class OptolinkEntity(CoordinatorEntity[OptolinkCoordinator]):
    """One catalog datapoint of the controller."""

    _attr_has_entity_name = False
    # The description never changes and can run to a couple of thousand characters, so it
    # would be written to the database on every state change for no benefit at all.
    _unrecorded_attributes = frozenset({"description"})

    def __init__(
        self,
        coordinator: OptolinkCoordinator,
        entry: ConfigEntry,
        definition: dict[str, Any],
    ) -> None:
        super().__init__(coordinator)
        self._def = definition
        self._attr_name = definition["name"]
        self._attr_unique_id = f"{coordinator.stable_id}_{definition['id']}"
        self._attr_device_info = coordinator.get_device_info(definition.get("circuit"))
        self._attr_entity_category = definition.get("entity_category")
        self._attr_icon = definition.get("icon")
        self._attr_entity_registry_enabled_default = definition.get(
            "enabled_by_default", True
        )

    @property
    def object_id_hint(self) -> str:
        """The entity id to suggest: built from the model, not the device's display name.

        Home Assistant composes an entity id from the device name at creation and never
        revisits it, so ids would otherwise depend on what the device happened to be called
        at the time. The model code is stable, which is what lets a reinstall land on the same
        ids and keep its history. See profiles.stable_object_id.
        """
        return self.coordinator.object_id(self._def.get("circuit"), self._def["name"])

    @property
    def raw_value(self) -> Any:
        """The decoded value the last poll produced for this datapoint, or None."""
        return (self.coordinator.data or {}).get(self._def["id"])

    @property
    def available(self) -> bool:
        """Unavailable until the datapoint has been read once, as well as whenever polling fails.

        Before the first read there is nothing to show. Reported as unknown instead, a switch
        is drawn with separate on and off buttons and its on icon, which reads like a state
        rather than the absence of one.
        """
        return super().available and self._def["id"] in (self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Where the value came from, and what the controller says it means.

        The description is the catalog's own text for the datapoint. A coding parameter's
        name tells you what it is called; only this tells you what changing it will do, and
        for the settings that matter -- changeover thresholds, hysteresis, temperature
        limits -- that is the difference between an informed change and a guess.
        """
        attrs: dict[str, Any] = {
            "address": f"0x{self._def['address']:04X}",
            "bytes": self._def["bytes"],
        }
        if description := self._def.get("description"):
            attrs["description"] = description
        return attrs
