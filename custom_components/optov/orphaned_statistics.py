"""Long-term statistics left behind by entities this integration switched off.

A tier switch disables entities; Home Assistant keeps their long-term statistics, on purpose,
so that an entity switched on again continues its history. Until then its statistics page
lists every one of them as having no state, with a delete button each and no way to delete
them together. This module finds those series, offers the deletion as a repair, and does it
through the recorder's own operation, which is what its delete button calls.

Only entities the integration disabled are offered. One the user disabled by hand is their
decision, and so is its history; the service can include those on request.
"""

from __future__ import annotations

import logging

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import list_statistic_ids
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .coordinator import OptolinkConfigEntry

_LOGGER = logging.getLogger(__name__)

ISSUE_KEY = "orphaned_statistics"


def issue_id(entry: OptolinkConfigEntry) -> str:
    return f"{ISSUE_KEY}_{entry.entry_id}"


@callback
def disabled_entity_ids(
    hass: HomeAssistant, entry: OptolinkConfigEntry, include_user: bool
) -> list[str]:
    """Entity ids of this entry that are disabled, by the integration or, on request, by hand."""
    allowed = {er.RegistryEntryDisabler.INTEGRATION}
    if include_user:
        allowed.add(er.RegistryEntryDisabler.USER)
    return [
        reg_entry.entity_id
        for reg_entry in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if reg_entry.disabled_by in allowed
    ]


async def async_orphaned_ids(
    hass: HomeAssistant, entry: OptolinkConfigEntry, include_user: bool = False
) -> list[str]:
    """The disabled entities that still have long-term statistics, as statistic ids."""
    if "recorder" not in hass.config.components:
        return []
    candidates = disabled_entity_ids(hass, entry, include_user)
    if not candidates:
        return []
    # A database query, so it runs where the recorder runs its own.
    found = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, set(candidates)
    )
    return sorted(item["statistic_id"] for item in found)


async def async_clear(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    """Delete the series, the same way the statistics page's delete button does."""
    if statistic_ids:
        get_instance(hass).async_clear_statistics(statistic_ids)
        _LOGGER.info(
            "Deleted the long-term statistics of %d entities", len(statistic_ids)
        )


async def async_update_issue(hass: HomeAssistant, entry: OptolinkConfigEntry) -> None:
    """Raise the repair when there is something to delete, withdraw it when there is not.

    Called after every setup, which is also where a tier switch ends up: switching entities
    off reloads the entry, and so does switching them on, thirty seconds later. Statistics
    compile a few minutes behind the readings, so an entity enabled only briefly has none and
    raises nothing.
    """
    # The recorder may be missing or still starting; a repair is not worth failing setup for.
    try:
        orphans = await async_orphaned_ids(hass, entry)
    except Exception as err:
        _LOGGER.debug("Could not look for orphaned statistics: %s", err)
        return
    if not orphans:
        ir.async_delete_issue(hass, DOMAIN, issue_id(entry))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id(entry),
        is_fixable=True,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_KEY,
        translation_placeholders={"count": str(len(orphans)), "name": entry.title},
        data={"entry_id": entry.entry_id},
    )
