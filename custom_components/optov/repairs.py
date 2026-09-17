"""Repair flows: what Home Assistant runs when the user presses Fix on one of our issues."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import orphaned_statistics
from .const import DOMAIN


class OrphanedStatisticsFlow(RepairsFlow):
    """Confirm, then delete the statistics of the entities the integration switched off."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._entry = hass.config_entries.async_get_entry(entry_id)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> data_entry_flow.FlowResult:
        # Counted again here rather than taken from the issue: the tier may have changed
        # since it was raised, and the number shown must be the number deleted.
        orphans = (
            await orphaned_statistics.async_orphaned_ids(self.hass, self._entry)
            if self._entry
            else []
        )
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                data_schema=vol.Schema({}),
                description_placeholders={"count": str(len(orphans))},
            )
        await orphaned_statistics.async_clear(self.hass, orphans)
        if self._entry:
            ir.async_delete_issue(
                self.hass, DOMAIN, orphaned_statistics.issue_id(self._entry)
            )
        return self.async_create_entry(title="", data={})


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    """Only one fixable issue exists; anything else is a programming error."""
    if issue_id.startswith(orphaned_statistics.ISSUE_KEY) and data:
        return OrphanedStatisticsFlow(hass, data["entry_id"])
    raise ValueError(f"No fix flow for issue {issue_id}")
