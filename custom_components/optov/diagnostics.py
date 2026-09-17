"""What a report about this integration needs to say, without saying where it runs."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import catalog_db
from .const import CONF_DEVICE, CONF_ENCRYPTION_KEY, CONF_HOST
from .coordinator import OptolinkConfigEntry
from .profiles import DeviceProfile

# An address and a key say where an installation is and how to reach it, never what is wrong
# with it. The catalog name stays: which build a controller runs on is half of most answers.
TO_REDACT = {CONF_DEVICE, CONF_ENCRYPTION_KEY, CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: OptolinkConfigEntry
) -> dict[str, Any]:
    """The controller, its catalog, what it taught us and how its bus is doing."""
    coordinator = entry.runtime_data.coordinator
    profile: DeviceProfile | None = coordinator.profile
    learned = coordinator.learned
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "catalog": await hass.async_add_executor_job(
            catalog_db.catalog_info, coordinator.db_path
        ),
        "controller": {
            "system_id": f"0x{profile.sys_id:04X}" if profile else None,
            "model": profile.model if profile else None,
            "software": profile.sw_version if profile else None,
            "circuits": sorted(profile.circuits) if profile else [],
            "schedules": sorted(profile.schedules) if profile else [],
            "entities": {
                platform: len(getattr(profile, platform))
                for platform in DeviceProfile.PLATFORMS
            }
            if profile
            else {},
        },
        # Why a datapoint is not polled and why the entity set looks as it does.
        "learned": {
            "retired_items": sorted(coordinator.retired_items),
            "retired_addresses": [
                f"0x{int(a):04X}" for a in learned.get("retired_addresses") or []
            ],
            "probed_registers": len(learned.get("condition_cache") or {}),
        },
        "bus": {
            "protocol": entry.runtime_data.client.protocol,
            "scan_interval": coordinator.scan_interval_seconds,
            "polled_datapoints": coordinator.active_channels,
            "cycle_seconds": coordinator.poll_duration,
            "load_percent": coordinator.bus_load,
            "datapoints_per_second": coordinator.datapoint_rate,
            "response_ms": coordinator.avg_response_time_ms,
            "telegrams_per_second": coordinator.telegram_rate,
            "datapoints_read": coordinator.datapoints_read,
            "bytes_transferred": coordinator.bytes_transferred,
            "wire_utilization_percent": coordinator.active_wire_load,
            "telegrams_failed": coordinator.telegrams_failed,
            "last_update_success": coordinator.last_update_success,
        },
        "clock": {
            "drift_seconds": coordinator.clock_drift,
            "last_corrected": coordinator.clock_corrected,
            "daylight_saving": coordinator.dst_status,
        },
        "faults": {
            "entries": len(coordinator.error_history),
            "latest": coordinator.last_error,
        },
    }
