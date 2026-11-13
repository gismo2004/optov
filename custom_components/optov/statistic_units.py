"""Long-term statistics recorded under a unit this integration no longer shows.

A unit is recognised by the catalog's key for it. An earlier version went by the word the
catalog language has, so a reading in "Seconds" or "bar (absolut)" was recorded under that
word. Shown as "s" or "bar" now, its statistics would stop: Home Assistant does not continue a
series under another unit, it raises a repair and waits. The series is the same quantity
under another label, so the label is corrected here and nobody has to.
"""

import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    STATISTIC_UNIT_TO_UNIT_CONVERTER,
    async_update_statistics_metadata,
    list_statistic_ids,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .coordinator import OptolinkConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_relabel(hass: HomeAssistant, entry: OptolinkConfigEntry) -> None:
    """Give the statistics of this controller's readings the unit the readings have now."""
    if "recorder" not in hass.config.components:
        return
    coordinator = entry.runtime_data.coordinator
    registry = er.async_get(hass)
    # entity id -> (the word it was recorded under, the unit it has)
    renamed: dict[str, tuple[str, str]] = {}
    for item in coordinator.profile.sensors:
        if item.get("unit_text"):
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{coordinator.stable_id}_{item['id']}"
            )
            if entity_id:
                renamed[entity_id] = (item["unit_text"], item["unit"])
    if not renamed:
        return
    # A database query, so it runs where the recorder runs its own. The recorder may still be
    # starting; a label is not worth failing setup for, and the next start tries again.
    try:
        recorded = await get_instance(hass).async_add_executor_job(
            list_statistic_ids, hass, set(renamed)
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not look at the statistics' units: %s", err)
        return
    for series in recorded:
        was, unit = renamed[series["statistic_id"]]
        if series.get("statistics_unit_of_measurement") != was:
            continue
        converter = STATISTIC_UNIT_TO_UNIT_CONVERTER.get(unit)
        async_update_statistics_metadata(
            hass,
            series["statistic_id"],
            new_unit_class=converter.UNIT_CLASS if converter else None,
            new_unit_of_measurement=unit,
        )
        _LOGGER.info(
            "Statistics of %s are now in %s, recorded as %s before",
            series["statistic_id"],
            unit,
            was,
        )
