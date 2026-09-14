"""OptoV integration for Home Assistant."""

import json
import logging
import os
from typing import Any

from homeassistant.components.http import StaticPathConfig
from homeassistant.components.persistent_notification import (
    async_create as pn_async_create,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from . import catalog_db
from .const import (
    CONF_CATALOG,
    CONF_DEBUG_LOGGING,
    CONF_ENCRYPTION_KEY,
    CONF_HOST,
    CONF_INSTANCE,
    CONF_PORT,
    CONF_PROXY_NAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_DEBUG_LOGGING,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import OptolinkConfigEntry, OptolinkCoordinator, OptolinkRuntime
from .optolink import OptolinkClient
from .profiles import parse_address

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "optov-cards.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"
FRONTEND_KEY = f"{DOMAIN}_frontend"
LOG_LEVEL_KEY = f"{DOMAIN}_saved_log_level"

# Entities this integration creates that are not catalog datapoints.
GATEWAY_SENSORS = (
    "bus_load",
    "poll_duration",
    "active_channels",
    "avg_response_time",
    "datapoint_rate",
    "catalog",
)
FAULT_HISTORY_SENSOR = "fehlerhistorie"


async def async_setup_entry(hass: HomeAssistant, entry: OptolinkConfigEntry) -> bool:
    """Connect to the controller, build its entity set and start polling."""
    _async_apply_log_level(hass, entry)
    _LOGGER.info("Setting up OptoV for %s", entry.title)

    # The catalog is the user's own build and lives in <config>/optov/. Without one there is
    # nothing to set up, and no amount of retrying changes that, so anything wrong with it is a
    # setup error carrying instructions rather than a "not ready".
    catalog_dir = hass.config.path(DOMAIN)
    chosen = entry.data.get(CONF_CATALOG)
    try:
        coordinator_db_path = await hass.async_add_executor_job(
            catalog_db.ensure_catalog, catalog_dir, chosen
        )
    except FileNotFoundError as err:
        # Either nothing is there at all, or the file this entry was set up with has been
        # renamed or deleted since. The two need different advice.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="catalog_gone" if chosen else "catalog_missing",
            translation_placeholders={"path": catalog_dir, "name": chosen or ""},
        ) from err
    except ValueError as err:
        # Several catalogs and nothing recorded: an entry from before catalogs had names of
        # their own. Which one this controller wants is the user's to say.
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="catalog_ambiguous",
            translation_placeholders={"path": catalog_dir},
        ) from err
    except Exception as err:
        raise ConfigEntryNotReady(f"Could not prepare the catalog: {err}") from err

    # The structure is checked on every start, not only when the catalog is installed: an
    # update to this integration can need a catalog newer than the one already in place, and a
    # catalog can be copied in by hand without ever passing through the upload.
    try:
        await hass.async_add_executor_job(catalog_db.check_catalog, coordinator_db_path)
    except catalog_db.CatalogSchemaError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="catalog_outdated" if err.outdated else "catalog_too_new",
            translation_placeholders={
                "name": os.path.basename(coordinator_db_path),
                "found": str(err.found) if err.found is not None else "-",
                "needed": str(err.needed),
            },
        ) from err
    except ValueError as err:
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="catalog_unreadable",
            translation_placeholders={"name": os.path.basename(coordinator_db_path)},
        ) from err

    if not chosen:
        # Settle it now, so that adding a second catalog later cannot change what this
        # controller reads.
        hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONF_CATALOG: os.path.basename(coordinator_db_path)},
        )

    client = OptolinkClient(
        host=entry.data[CONF_HOST],
        port=entry.data.get(CONF_PORT, DEFAULT_PORT),
        encryption_key=entry.data[CONF_ENCRYPTION_KEY],
        instance=entry.data.get(CONF_INSTANCE, 0),
    )
    scan_interval = entry.options.get(
        CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    )
    coordinator = OptolinkCoordinator(hass, client, scan_interval, entry)
    coordinator.db_path = coordinator_db_path
    try:
        await client.connect()
        await coordinator.async_init_device()
    except Exception as err:
        await client.disconnect()
        raise ConfigEntryNotReady(f"Controller not reachable: {err}") from err

    entry.runtime_data = OptolinkRuntime(
        client=client,
        coordinator=coordinator,
        options=dict(entry.options),
        catalog=os.path.basename(coordinator_db_path),
    )

    try:
        _async_update_title(hass, entry, coordinator)
        _async_migrate_identity(hass, entry, coordinator)
        _async_reconcile_registry(hass, entry, coordinator)
        await _async_register_frontend(hass)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Home Assistant does not unload an entry whose setup failed, so async_unload_entry
        # never runs for it. Without closing the connection here its socket would stay open,
        # receiving bytes, for as long as Home Assistant runs.
        await client.disconnect()
        raise

    # Home Assistant fires update listeners for any change to the entry, and this integration
    # writes to its own entry data while running (the title, the probe cache, the retired
    # addresses). Only an options change or another catalog is a reason to reload; the listener
    # checks. It is also what reloads after Reconfigure, which updates the entry and stops there.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # The first sweep runs in the background so setup returns at once and the device page is
    # usable immediately. Tied to the entry, so an unload cancels it.
    entry.async_create_background_task(
        hass, coordinator.async_refresh(), f"{DOMAIN}_first_refresh"
    )
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OptolinkConfigEntry) -> bool:
    """Stop polling and drop the connection. Services go with the last entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        try:
            await entry.runtime_data.client.disconnect()
        except Exception as err:
            _LOGGER.debug("Disconnect on unload: %s", err)
        if not [
            e
            for e in hass.config_entries.async_loaded_entries(DOMAIN)
            if e is not entry
        ]:
            for service in _SERVICES:
                hass.services.async_remove(DOMAIN, service)
    return unload_ok


async def _async_options_updated(
    hass: HomeAssistant, entry: OptolinkConfigEntry
) -> None:
    """Reload when the options or the catalog changed, and only then. See async_setup_entry."""
    runtime = entry.runtime_data
    if runtime.options == dict(entry.options) and runtime.catalog == entry.data.get(
        CONF_CATALOG
    ):
        _LOGGER.debug(
            "Config entry updated without an options or catalog change; not reloading"
        )
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _async_apply_log_level(hass: HomeAssistant, entry: OptolinkConfigEntry) -> None:
    """Raise this integration's logger when the verbose option is on, and only then.

    Everything said on a timer is debug, so a normal installation is quiet and the option is
    how you get detail without editing Home Assistant's `logger:` configuration. Switching it
    off restores whatever level was in force before, because a `logger:` entry naming this
    integration is the user's own decision and must survive.
    """
    logger = logging.getLogger(__package__)
    verbose = entry.options.get(CONF_DEBUG_LOGGING, DEFAULT_DEBUG_LOGGING)
    saved = hass.data.get(LOG_LEVEL_KEY)
    if verbose:
        if saved is None:
            hass.data[LOG_LEVEL_KEY] = logger.level
        logger.setLevel(logging.DEBUG)
    elif saved is not None:
        logger.setLevel(saved)
        hass.data.pop(LOG_LEVEL_KEY, None)


@callback
def _async_update_title(
    hass: HomeAssistant, entry: OptolinkConfigEntry, coordinator: OptolinkCoordinator
) -> None:
    """Name the entry after the controller once it has identified itself."""
    if not coordinator.profile or not coordinator.profile.device_name:
        return
    proxy_name = entry.data.get(CONF_PROXY_NAME)
    instance = entry.data.get(CONF_INSTANCE, 0)
    suffix = (
        f" · {proxy_name}" if proxy_name else (f" #{instance}" if instance > 0 else "")
    )
    title = (
        f"{coordinator.profile.device_name} "
        f"(0x{coordinator.profile.sys_id:04X} @ {entry.data[CONF_HOST]}){suffix}"
    )
    if entry.title != title:
        hass.config_entries.async_update_entry(entry, title=title)


@callback
def _async_migrate_identity(
    hass: HomeAssistant, entry: OptolinkConfigEntry, coordinator: OptolinkCoordinator
) -> None:
    """Move entities and devices off the config entry id and onto the stable id.

    Identity used to be prefixed with the config entry id, which is regenerated whenever the
    integration is removed and added again. Home Assistant matches a returning entity to what
    it remembers -- enabled state, area, custom name, hidden flag, labels -- by unique id
    alone, so that prefix quietly discarded all of it on every reinstall. See
    OptolinkCoordinator.stable_id for what replaced it.

    This renames what is already in the registries, once. Without it the fix would itself
    orphan every entity it is meant to protect. Doing nothing is correct and cheap on every
    later start, and for an installation that has no stable id to move to.
    """
    stable = coordinator.stable_id
    if stable == entry.entry_id:
        return

    old_prefix = f"{entry.entry_id}_"
    device_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
        renamed = {
            (
                domain,
                stable + identifier[len(entry.entry_id) :]
                if domain == DOMAIN
                and (identifier == entry.entry_id or identifier.startswith(old_prefix))
                else identifier,
            )
            for domain, identifier in device.identifiers
        }
        if renamed != device.identifiers:
            device_reg.async_update_device(device.id, new_identifiers=renamed)

    entity_reg = er.async_get(hass)
    moved = 0
    for reg_entry in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
        if not reg_entry.unique_id.startswith(old_prefix):
            continue
        new_unique_id = f"{stable}_{reg_entry.unique_id[len(old_prefix) :]}"
        if entity_reg.async_get_entity_id(reg_entry.domain, DOMAIN, new_unique_id):
            # Something already holds the new id; leave the old one rather than collide.
            continue
        entity_reg.async_update_entity(reg_entry.entity_id, new_unique_id=new_unique_id)
        moved += 1
    if moved:
        _LOGGER.info(
            "Moved %d entities onto the stable identity %s; they now survive a reinstall",
            moved,
            stable,
        )


def _async_reconcile_registry(
    hass: HomeAssistant, entry: OptolinkConfigEntry, coordinator: OptolinkCoordinator
) -> None:
    """Bring the device and entity registries in line with this controller's profile.

    The profile can change between starts -- a circuit gets hidden, a tier is switched on or
    off, a datapoint is reclassified -- and Home Assistant only applies some of that to
    entities that already exist. This is where the rest is applied.
    """
    profile = coordinator.profile
    if not profile:
        return
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)

    # Devices: the controller, one sub-device per circuit it has, and the gateway. Renamed if
    # the catalog now yields a better name than when they were created; Home Assistant takes
    # a device's name from the first registration only.
    for circuit in [None, *profile.circuits]:
        info = coordinator.get_device_info(circuit)
        if not info:
            continue
        dev = device_reg.async_get_or_create(config_entry_id=entry.entry_id, **info)
        wanted = info.get("name")
        if wanted and dev.name != wanted and dev.name_by_user is None:
            device_reg.async_update_device(dev.id, name=wanted, model=info.get("model"))

    gateway_info = coordinator.get_gateway_device_info()
    gateway = device_reg.async_get_or_create(
        config_entry_id=entry.entry_id, **gateway_info
    )
    if (
        gateway_info.get("sw_version")
        and gateway.sw_version != gateway_info["sw_version"]
    ):
        device_reg.async_update_device(
            gateway.id,
            sw_version=gateway_info["sw_version"],
            model=gateway_info.get("model"),
            connections=gateway_info.get("connections"),
        )

    # Circuit sub-devices the profile no longer has (a circuit hidden by the equipment probe).
    live_circuits = set(profile.circuits) | {"gateway"}
    for dev in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
        for domain, identifier in dev.identifiers:
            device_prefix = f"{coordinator.stable_id}_"
            if (
                domain == DOMAIN
                and identifier.startswith(device_prefix)
                and identifier[len(device_prefix) :] not in live_circuits
            ):
                _LOGGER.info("Removing circuit device %s", dev.name)
                device_reg.async_remove_device(dev.id)

    # Every entity the profile produces, keyed the way the registry keys them.
    prefix = f"{coordinator.stable_id}_"
    expected: dict[tuple, dict[str, Any] | None] = {}
    for platform, domain in (
        ("sensors", "sensor"),
        ("binary_sensors", "binary_sensor"),
        ("numbers", "number"),
        ("selects", "select"),
        ("switches", "switch"),
    ):
        for item in getattr(profile, platform, []):
            expected[(domain, f"{prefix}{item['id']}")] = item
    for key in profile.schedules:
        expected[("sensor", f"{prefix}schaltzeiten_{key}")] = None
    for suffix in (*GATEWAY_SENSORS, FAULT_HISTORY_SENSOR):
        expected[("sensor", f"{prefix}{suffix}")] = None

    # Home Assistant records nothing about who enabled an entity: one the user switched on by
    # hand looks exactly like one a tier brought in. Two lists stand in for that.
    #
    # `auto_enabled` is what the profile switched on last time, and is the only thing this
    # integration will ever switch off again. `user_enabled` is what someone switched on by
    # hand, and is never switched off -- switching a tier on and then off again used to take
    # those with it, because a tier can make a hand-picked datapoint default-on for a while,
    # which quietly moved it into the first list.
    previously_auto = set(entry.data.get("auto_enabled") or [])
    user_enabled = set(entry.data.get("user_enabled") or [])
    auto_enabled = sorted(
        uid
        for (_domain, uid), item in expected.items()
        if item is not None and item.get("enabled_by_default")
    )
    retired = set(entry.data.get("retired_items") or [])

    for reg_entry in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
        key = (reg_entry.domain, reg_entry.unique_id)
        if key not in expected:
            _LOGGER.info(
                "Removing entity %s: not in this controller's profile",
                reg_entry.entity_id,
            )
            entity_reg.async_remove(reg_entry.entity_id)
            continue
        item = expected[key]
        if item is None:
            continue
        updates: dict[str, Any] = {}
        uid = key[1]
        default_on = bool(item.get("enabled_by_default"))

        # On, not on by default, and not something this integration switched on: someone did
        # it by hand. Remembered from here on, so no later tier change takes it away.
        if (
            reg_entry.disabled_by is None
            and not default_on
            and uid not in previously_auto
        ):
            user_enabled.add(uid)
        # Switched off by hand again: forget it, or it would be switched back on below.
        if reg_entry.disabled_by == er.RegistryEntryDisabler.USER:
            user_enabled.discard(uid)

        # Switch on what the profile enables and what the user asked for, unless the
        # controller itself said the datapoint is absent. Switch off only what this
        # integration switched on and a tier has since taken away.
        wants_on = (default_on or uid in user_enabled) and item["id"] not in retired
        if reg_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION and wants_on:
            updates["disabled_by"] = None
        elif reg_entry.disabled_by is None and not wants_on and uid in previously_auto:
            updates["disabled_by"] = er.RegistryEntryDisabler.INTEGRATION
        # Controls versus Configuration is decided at creation only; a reclassified datapoint
        # would otherwise move only for fresh installations.
        if reg_entry.entity_category != item.get("entity_category"):
            updates["entity_category"] = item.get("entity_category")
        # A datapoint that moved between circuits follows its circuit's device.
        info = coordinator.get_device_info(item.get("circuit"))
        if info:
            # Identifiers are no longer unique across config entries, so the lookup has to be
            # scoped to this one.
            device = device_reg.async_get_device_by_identifier(
                next(iter(info["identifiers"])), entry.entry_id
            )
            if device and reg_entry.device_id != device.id:
                updates["device_id"] = device.id
        if updates:
            entity_reg.async_update_entity(reg_entry.entity_id, **updates)

    # Written once, after the loop, because the hand-picked entities are only discovered
    # while walking it. Only what an entity still in the profile says counts: an entity that
    # has gone is dropped from both lists rather than remembered forever.
    known = {uid for _domain, uid in expected}
    changes: dict[str, Any] = {}
    if auto_enabled != sorted(previously_auto):
        changes["auto_enabled"] = auto_enabled
    kept_user = sorted(user_enabled & known)
    if kept_user != sorted(entry.data.get("user_enabled") or []):
        changes["user_enabled"] = kept_user
    if changes:
        hass.config_entries.async_update_entry(entry, data={**entry.data, **changes})


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the dashboard cards and register them as a resource, once per Home Assistant.

    The card file is served straight from the integration directory, so it is upgraded
    together with the Python code. In storage mode the resource is created, or its
    cache-busting version bumped; in YAML mode resources are the user's file and only a hint
    is logged.
    """
    if hass.data.get(FRONTEND_KEY):
        return
    card_path = hass.config.path("custom_components", DOMAIN, "frontend", CARD_FILENAME)
    try:
        mtime = await hass.async_add_executor_job(os.path.getmtime, card_path)
    except OSError:
        _LOGGER.debug("No card at %s, skipping frontend registration", card_path)
        return

    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, card_path, cache_headers=False)]
    )
    hass.data[FRONTEND_KEY] = True
    url = f"{CARD_URL}?v={int(mtime)}"

    lovelace = hass.data.get("lovelace")
    if lovelace is None or getattr(lovelace, "resource_mode", None) != "storage":
        _LOGGER.info(
            "Dashboard resources are managed in YAML; add %s as a module resource", url
        )
        return
    resources = lovelace.resources
    try:
        await resources.async_get_info()  # loads the collection from storage
        ours = None
        for item in resources.async_items():
            item_url = str(item.get("url", ""))
            if item_url.split("?")[0] == CARD_URL:
                ours = item
        if ours is None:
            await resources.async_create_item({"res_type": "module", "url": url})
            _LOGGER.info("Registered the dashboard cards as a resource: %s", url)
        elif ours.get("url") != url:
            await resources.async_update_item(
                ours["id"], {"res_type": "module", "url": url}
            )
    except Exception as err:
        _LOGGER.warning("Could not register the dashboard cards as a resource: %s", err)


# ---------------------------------------------------------------------------------------
# Services. Registered once for the domain, whatever the number of controllers, and routed
# to a controller by `config_entry_id` where it matters. A single controller needs no id.
# ---------------------------------------------------------------------------------------

_SERVICES = (
    "refresh_all",
    "sync_clock",
    "read_datapoint",
    "write_datapoint",
    "read_schedule",
    "set_schedule_day",
    "set_schedule_window",
)


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list[OptolinkCoordinator]:
    """The controllers a call addresses: one by id, or every loaded one."""
    wanted = call.data.get("config_entry_id")
    found = [
        entry.runtime_data.coordinator
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if not wanted or entry.entry_id == wanted
    ]
    if not found:
        raise ServiceValidationError("No OptoV controller is loaded")
    return found


def _one_coordinator(hass: HomeAssistant, call: ServiceCall) -> OptolinkCoordinator:
    """The single controller a call addresses; ambiguity is an error, not a guess."""
    found = _coordinators(hass, call)
    if len(found) > 1:
        raise ServiceValidationError(
            "More than one controller is set up; pass config_entry_id"
        )
    return found[0]


def _schedule_owner(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[OptolinkCoordinator, str]:
    """The controller that has the programme a call names, and the programme's key."""
    ref = str(call.data.get("schedule") or "")
    for coordinator in _coordinators(hass, call):
        key = coordinator.resolve_schedule(ref)
        if key is not None:
            return coordinator, key
    raise ServiceValidationError(f"{ref!r} is not a programme of any controller")


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, "refresh_all"):
        return

    async def refresh_all(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_refresh_all()

    async def sync_clock(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            if await coordinator.async_sync_clock(force=True) is None:
                _LOGGER.warning(
                    "Clock sync: %s exposes no clock datapoint", coordinator.name
                )

    async def read_datapoint(call: ServiceCall) -> None:
        coordinator = _one_coordinator(hass, call)
        address = parse_address(call.data["address"])
        length = int(call.data.get("bytes", 2))
        try:
            raw = await coordinator.async_read_custom_datapoint(address, length)
        except Exception as err:
            pn_async_create(
                hass,
                f"Error reading 0x{address:04X}: {err}",
                title=f"Read 0x{address:04X}",
                notification_id=f"optov_read_{address}",
            )
            raise
        pn_async_create(
            hass,
            f"Address 0x{address:04X} ({length} bytes)\nRaw: {raw.hex(' ').upper()}\n"
            f"Little-endian: {int.from_bytes(raw, 'little', signed=True)}",
            title=f"Read 0x{address:04X}",
            notification_id=f"optov_read_{address}",
        )

    async def write_datapoint(call: ServiceCall) -> None:
        coordinator = _one_coordinator(hass, call)
        address = parse_address(call.data["address"])
        data = bytes.fromhex(str(call.data["data"]).replace(" ", "").replace("0x", ""))
        await coordinator.async_write_custom_datapoint(address, data)
        pn_async_create(
            hass,
            f"Wrote {len(data)} bytes to 0x{address:04X}: {data.hex(' ').upper()}",
            title=f"Write 0x{address:04X}",
            notification_id=f"optov_write_{address}",
        )

    async def read_schedule(call: ServiceCall) -> None:
        coordinator, key = _schedule_owner(hass, call)
        await coordinator.async_refresh_schedules(key)
        lines = []
        for day, windows in coordinator.schedules.get(key, {}).items():
            if windows:
                lines.append(
                    f"{day}: "
                    + ", ".join(
                        f"{w['start']}-{w['end']} ({w['mode']})" for w in windows
                    )
                )
            else:
                lines.append(f"{day}: -")
        pn_async_create(
            hass,
            "\n".join(lines) or "-",
            title=f"Programme {key}",
            notification_id=f"optov_schedule_{key}",
        )

    async def set_schedule_day(call: ServiceCall) -> None:
        coordinator, key = _schedule_owner(hass, call)
        windows = call.data.get("windows", [])
        if isinstance(windows, str):
            windows = json.loads(windows)
        # Errors propagate: a call that fails must fail visibly for the caller.
        await coordinator.async_set_day_schedule(
            key, call.data.get("day"), list(windows)
        )

    async def set_schedule_window(call: ServiceCall) -> None:
        coordinator, key = _schedule_owner(hass, call)
        await coordinator.async_set_schedule_window(
            key,
            call.data.get("day"),
            int(call.data.get("window", 1)),
            str(call.data.get("start", "06:00")),
            str(call.data.get("end", "22:00")),
            call.data.get("mode"),
        )

    for name, handler in (
        ("refresh_all", refresh_all),
        ("sync_clock", sync_clock),
        ("read_datapoint", read_datapoint),
        ("write_datapoint", write_datapoint),
        ("read_schedule", read_schedule),
        ("set_schedule_day", set_schedule_day),
        ("set_schedule_window", set_schedule_window),
    ):
        hass.services.async_register(DOMAIN, name, handler)
