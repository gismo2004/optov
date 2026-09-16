"""OptoV integration for Home Assistant."""

import json
import logging
import os
from typing import Any
from urllib.parse import urlencode

from homeassistant.components.esphome.serial_proxy import build_url
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.persistent_notification import (
    async_create as pn_async_create,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    Event,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import (
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType
from homeassistant.setup import async_when_setup

from . import catalog_db
from .const import (
    CONF_CATALOG,
    CONF_DEVICE,
    CONF_ENCRYPTION_KEY,
    CONF_HOST,
    CONF_INSTANCE,
    CONF_PORT,
    CONF_PROXY_NAME,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
    option,
)
from .coordinator import OptolinkConfigEntry, OptolinkCoordinator, OptolinkRuntime
from .optolink import OptolinkClient, OptolinkDeviceError
from .profiles import DeviceProfile, parse_address

_LOGGER = logging.getLogger(__name__)

CARD_FILENAME = "optov-cards.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILENAME}"
REGISTRY_LISTENER_KEY = f"{DOMAIN}_registry_listener"
OWN_ENABLES_KEY = f"{DOMAIN}_own_enables"
# Set in an entity's registry options once its enabled state has been changed by hand.
MANUAL_OPTION = "manual"

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


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Put the services and the dashboard cards in place, before any controller is set up.

    Both belong to the integration rather than to a controller. Home Assistant validates an
    automation against the services that exist, so registering them with an entry left every
    automation calling one broken until that entry loaded. The card file is the same story from
    the other side: served from an entry, it is missing while Home Assistant is still starting
    and missing altogether while a controller is unreachable, and a dashboard that asked for it
    in that window shows a broken card until someone reloads the page.
    """
    _async_register_services(hass)
    await _async_register_frontend(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: OptolinkConfigEntry) -> bool:
    """Connect to the controller, build its entity set and start polling."""
    _async_track_manual_changes(hass)
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

    client = OptolinkClient(_port_url(hass, entry), entry.data[CONF_HOST])
    scan_interval = option(entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
    coordinator = OptolinkCoordinator(hass, client, scan_interval, entry)
    coordinator.db_path = coordinator_db_path
    try:
        await client.connect()
        await coordinator.async_init_device()
    except catalog_db.UnknownControllerError as err:
        # The link works and the controller answered; the catalog simply has no entry for what
        # it said. Retrying reads the same bytes again, so this is a setup error that names
        # them instead of a "not ready" that hides them in a retry loop.
        await client.disconnect()
        # Shortened: a System ID covers twenty boards in the GWG families.
        known = ", ".join(err.variants[:8]) + ("..." if len(err.variants) > 8 else "")
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="controller_unknown",
            translation_placeholders={
                "sys_id": f"0x{err.sys_id:04X}",
                "hw": "-" if err.hw_index is None else f"0x{err.hw_index:02X}",
                "sw": "-" if err.sw_index is None else f"0x{err.sw_index:02X}",
                "name": os.path.basename(coordinator_db_path),
                "variants": known or "-",
            },
        ) from err
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
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        # Entities Home Assistant restored from an earlier installation of this controller
        # only reach the registry while the platforms set up; see _async_apply_enabled_states.
        _async_apply_enabled_states(hass, entry, coordinator)
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
    return True


async def async_unload_entry(hass: HomeAssistant, entry: OptolinkConfigEntry) -> bool:
    """Stop polling and drop the connection. The services stay, as they do not need an entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        try:
            await entry.runtime_data.client.disconnect()
        except Exception as err:
            _LOGGER.debug("Disconnect on unload: %s", err)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: OptolinkConfigEntry) -> None:
    """Delete the catalog of a removed entry, unless another entry still records it.

    Reconfigure refuses to delete a catalog an entry uses, and a removed entry has no Reconfigure
    left, so without this the catalog of the last controller could never be removed from within
    Home Assistant. Home Assistant still lists the entry being removed, hence the id check.
    """
    if not [
        other
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ]:
        await _async_remove_card_resource(hass)

    name = entry.data.get(CONF_CATALOG)
    if not name or any(
        other.data.get(CONF_CATALOG) == name
        for other in hass.config_entries.async_entries(DOMAIN)
        if other.entry_id != entry.entry_id
    ):
        return
    try:
        await hass.async_add_executor_job(
            catalog_db.remove_catalog, hass.config.path(DOMAIN), name
        )
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as err:
        _LOGGER.warning("Could not delete catalog %s of the removed entry: %s", name, err)


def _port_url(hass: HomeAssistant, entry: OptolinkConfigEntry) -> str:
    """The entry's serial port as a serialx URL.

    A node Home Assistant's ESPHome integration has is reached over that integration's own
    connection, looked up by the node's address at every start: such a URL names that node's
    config entry, and setting the node up again gives it a new one while its address stays.
    Any other node keeps the URL it was set up with, which carries its own key. The URL is
    written back to the entry, which is where Home Assistant looks to tell who holds a port.
    """
    host, instance = entry.data[CONF_HOST], entry.data.get(CONF_INSTANCE, 0)
    url = entry.data.get(CONF_DEVICE, "")
    for node in hass.config_entries.async_entries("esphome"):
        if node.data.get("host") == host:
            info = node.state is ConfigEntryState.LOADED and node.runtime_data.device_info
            if not info or instance >= len(info.serial_proxies):
                raise ConfigEntryNotReady(f"ESPHome at {host} has not listed its proxies yet")
            url = str(build_url(node.entry_id, info.serial_proxies[instance].name))
            break
    else:
        if not url:
            # Set up before the port was recorded, and its node is not one Home Assistant has.
            query = urlencode(
                {"port_name": instance, "key": entry.data[CONF_ENCRYPTION_KEY]}
            )
            url = f"esphome://{host}:{entry.data.get(CONF_PORT, DEFAULT_PORT)}/?{query}"
    if url != entry.data.get(CONF_DEVICE):
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICE: url})
    return url


async def _async_options_updated(
    hass: HomeAssistant, entry: OptolinkConfigEntry
) -> None:
    """Apply an options or catalog change; ignore the entry data this integration writes itself.

    Another catalog, or options that switch nothing on, reload at once. Options that switch
    entities on do not: those entities are enabled here, and Home Assistant reloads an entry
    30 seconds after one of its entities is enabled, which then applies the whole change.
    Reloading here as well would reload twice. The options are recorded as applied, so entry
    data written while that reload is pending cannot bring it forward.
    """
    runtime = entry.runtime_data
    options = dict(entry.options)
    same_catalog = runtime.catalog == entry.data.get(CONF_CATALOG)
    if runtime.options == options and same_catalog:
        _LOGGER.debug(
            "Config entry updated without an options or catalog change; not reloading"
        )
        return
    if same_catalog:
        profile = await runtime.coordinator.async_profile_for_options(options)
        if profile and _async_apply_enabled_states(
            hass, entry, runtime.coordinator, profile
        ):
            runtime.options = options
            _LOGGER.info(
                "Options saved; Home Assistant reloads the entry in about 30 seconds to apply them"
            )
            return
    await hass.config_entries.async_reload(entry.entry_id)


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

    expected = _expected_entities(coordinator)
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

    _async_apply_enabled_states(hass, entry, coordinator)

    # Who enabled an entity is recorded on the entity itself; the entry no longer carries the
    # lists an older version kept for it, which a re-added entry started without.
    if stale := [key for key in ("auto_enabled", "user_enabled") if key in entry.data]:
        hass.config_entries.async_update_entry(
            entry, data={k: v for k, v in entry.data.items() if k not in stale}
        )


def _expected_entities(
    coordinator: OptolinkCoordinator, profile: DeviceProfile | None = None
) -> dict[tuple[str, str], dict[str, Any] | None]:
    """Every entity a profile produces, keyed the way the registry keys them.

    The running profile unless another is given. A catalog datapoint maps to its profile item;
    the integration's own sensors map to None.
    """
    profile = profile or coordinator.profile
    prefix = f"{coordinator.stable_id}_"
    expected: dict[tuple[str, str], dict[str, Any] | None] = {}
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
    return expected


@callback
def _async_track_manual_changes(hass: HomeAssistant) -> None:
    """Mark an entity as the user's to decide the moment its enabled state is changed by hand.

    Home Assistant records no author for enabling or disabling an entity. The registry update
    does carry the state before the change, which is enough to tell a person's switch from the
    rest: enabling a whole config entry or device lifts CONFIG_ENTRY or DEVICE rather than
    anything in between, and the enables this integration makes itself are announced in
    OWN_ENABLES_KEY first (its disables set INTEGRATION, which no person does).

    The mark goes into the entity's registry options. Home Assistant keeps those with a removed
    entity and restores them when it returns, so a choice made by hand outlives removing and
    re-adding the integration. Registered once per Home Assistant, at the start of the first
    setup, so a switch made while an entry is not running is seen as well.
    """
    if hass.data.get(REGISTRY_LISTENER_KEY):
        return
    hass.data[REGISTRY_LISTENER_KEY] = True
    own_enables: set[str] = hass.data.setdefault(OWN_ENABLES_KEY, set())
    entity_reg = er.async_get(hass)
    by_hand = (None, er.RegistryEntryDisabler.USER)
    from_hand_or_us = (*by_hand, er.RegistryEntryDisabler.INTEGRATION)

    @callback
    def _is_enabled_state_change(event_data: er.EventEntityRegistryUpdatedData) -> bool:
        return event_data["action"] == "update" and "disabled_by" in event_data["changes"]

    @callback
    def _async_changed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id in own_enables:
            own_enables.discard(entity_id)
            return
        reg_entry = entity_reg.async_get(entity_id)
        if (
            reg_entry is None
            or reg_entry.platform != DOMAIN
            or reg_entry.options.get(DOMAIN, {}).get(MANUAL_OPTION)
        ):
            return
        if (
            reg_entry.disabled_by in by_hand
            and event.data["changes"]["disabled_by"] in from_hand_or_us
        ):
            entity_reg.async_update_entity_options(
                entity_id, DOMAIN, {**reg_entry.options.get(DOMAIN, {}), MANUAL_OPTION: True}
            )

    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        _async_changed,
        event_filter=_is_enabled_state_change,
    )


@callback
def _async_apply_enabled_states(
    hass: HomeAssistant,
    entry: OptolinkConfigEntry,
    coordinator: OptolinkCoordinator,
    profile: DeviceProfile | None = None,
) -> int:
    """Switch each entity on or off as its tier says, except those decided by hand.

    Decided by hand means marked by _async_track_manual_changes, or disabled by the user, which
    Home Assistant records itself. Everything else follows the options: on if the profile enables
    it and the controller has not reported the datapoint absent, off otherwise. The running
    profile unless another is given; returns how many entities it switched on.

    Called before the platforms set up, so an entity a tier now enables is added normally, and
    again after them, for the entities Home Assistant has just restored from an earlier
    installation of this controller, which only reach the registry at that point. Switching one
    off then needs no reload; Home Assistant reloads the entry only after switching one on.
    """
    if not (profile or coordinator.profile):
        return 0
    entity_reg = er.async_get(hass)
    own_enables: set[str] = hass.data.setdefault(OWN_ENABLES_KEY, set())
    expected = _expected_entities(coordinator, profile)
    retired = coordinator.retired_items
    switched_on = 0
    for reg_entry in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
        item = expected.get((reg_entry.domain, reg_entry.unique_id))
        if (
            item is None
            or reg_entry.disabled_by == er.RegistryEntryDisabler.USER
            or reg_entry.options.get(DOMAIN, {}).get(MANUAL_OPTION)
        ):
            continue
        wants_on = bool(item.get("enabled_by_default")) and item["id"] not in retired
        if reg_entry.disabled_by is None and not wants_on:
            entity_reg.async_update_entity(
                reg_entry.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
            )
        elif reg_entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION and wants_on:
            own_enables.add(reg_entry.entity_id)
            entity_reg.async_update_entity(reg_entry.entity_id, disabled_by=None)
            switched_on += 1
    return switched_on


async def _async_register_frontend(hass: HomeAssistant) -> None:
    """Serve the dashboard cards and make Home Assistant load them.

    A dashboard kept in storage gets a resource entry, the way a card has always reached a
    browser: the frontend asks for that list over the websocket every time it loads a
    dashboard, so a new version of the card arrives by itself. The page around it does not --
    Home Assistant's service worker serves the app shell from its own cache until Home
    Assistant's frontend is updated -- so a module named in that page reaches nobody whose
    shell predates it, which is why the list, not the page, carries the card here.

    A dashboard kept in YAML has no such list to write to: there the module is added to the
    page, which is the only way that works, and a card update then waits for the shell.

    The resource is written once Lovelace is up, which is after this runs.
    """
    card_path = hass.config.path("custom_components", DOMAIN, "frontend", CARD_FILENAME)
    try:
        mtime = await hass.async_add_executor_job(os.path.getmtime, card_path)
    except OSError:
        _LOGGER.debug("No card at %s, skipping frontend registration", card_path)
        return

    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, card_path, cache_headers=False)]
    )
    # The stamp makes a browser fetch the file again after an update rather than keep its copy.
    url = f"{CARD_URL}?v={int(mtime)}"

    async def _register(hass: HomeAssistant, _component: str) -> None:
        resources = _card_resources(hass)
        if resources is None:
            add_extra_js_url(hass, url)
            return
        try:
            await resources.async_get_info()  # loads the collection from storage
            ours = next(
                (
                    item
                    for item in resources.async_items()
                    if str(item.get("url", "")).split("?")[0] == CARD_URL
                ),
                None,
            )
            if ours is None:
                await resources.async_create_item({"res_type": "module", "url": url})
                _LOGGER.info("Registered the dashboard cards as a resource: %s", url)
            elif ours.get("url") != url:
                await resources.async_update_item(
                    ours["id"], {"res_type": "module", "url": url}
                )
        except Exception as err:
            _LOGGER.warning("Could not register the dashboard cards as a resource: %s", err)

    async_when_setup(hass, "lovelace", _register)


@callback
def _card_resources(hass: HomeAssistant) -> Any | None:
    """The dashboard resource list, if this installation keeps one that can be written to."""
    lovelace = hass.data.get("lovelace")
    if getattr(lovelace, "resource_mode", None) != "storage":
        return None
    return getattr(lovelace, "resources", None)


async def _async_remove_card_resource(hass: HomeAssistant) -> None:
    """Take the cards' resource entry out again when the last controller goes.

    Without this the entry outlives the integration and points at a file that is no longer
    served, which shows up on every dashboard as a card that cannot be loaded.
    """
    resources = _card_resources(hass)
    if resources is None:
        return
    try:
        await resources.async_get_info()
        for item in list(resources.async_items()):
            if str(item.get("url", "")).split("?")[0] == CARD_URL:
                await resources.async_delete_item(item["id"])
                _LOGGER.info("Removed the dashboard resource of the cards")
    except Exception as err:
        _LOGGER.warning("Could not remove the dashboard resource: %s", err)


# ---------------------------------------------------------------------------------------
# Services. Registered once for the domain, whatever the number of controllers, and routed
# to a controller by `config_entry_id` where it matters. A single controller needs no id.
# ---------------------------------------------------------------------------------------


def _coordinators(hass: HomeAssistant, call: ServiceCall) -> list[OptolinkCoordinator]:
    """The controllers a call addresses: one by id, or every loaded one."""
    wanted = call.data.get("config_entry_id")
    found = [
        entry.runtime_data.coordinator
        for entry in hass.config_entries.async_loaded_entries(DOMAIN)
        if not wanted or entry.entry_id == wanted
    ]
    if not found:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="no_controller"
        )
    return found


def _one_coordinator(hass: HomeAssistant, call: ServiceCall) -> OptolinkCoordinator:
    """The single controller a call addresses; ambiguity is an error, not a guess."""
    found = _coordinators(hass, call)
    if len(found) > 1:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="which_controller"
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
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_schedule",
        translation_placeholders={"schedule": ref},
    )


def _refused(err: OptolinkDeviceError, address: int) -> HomeAssistantError:
    """The controller's own refusal, in words the caller can act on.

    It answered and said no: either it does not have the address, or the length asked for does
    not match its own layout. Neither is a fault of the link, so it must not read like one.
    """
    return HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="controller_refused",
        translation_placeholders={
            "address": f"0x{address:04X}",
            "code": f"0x{err.code:02X}" if err.code is not None else "-",
        },
    )


@callback
def _async_register_services(hass: HomeAssistant) -> None:
    async def refresh_all(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            await coordinator.async_refresh_all()

    async def sync_clock(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass, call):
            if await coordinator.async_sync_clock(force=True) is None:
                _LOGGER.warning(
                    "Clock sync: %s exposes no clock datapoint", coordinator.name
                )

    async def read_datapoint(call: ServiceCall) -> ServiceResponse:
        coordinator = _one_coordinator(hass, call)
        address = parse_address(call.data["address"])
        # A datapoint is read as the whole block it sits in, and the block can be longer than
        # the value: asking for the value's own length is what the controller refuses. So the
        # catalog decides unless the caller says otherwise, which is what an address the
        # catalog does not know needs.
        item = coordinator.datapoint_at(address)
        length = int(call.data.get("bytes") or (item or {}).get("block") or 2)
        try:
            raw = await coordinator.async_read_custom_datapoint(address, length)
        except OptolinkDeviceError as err:
            raise _refused(err, address) from err
        except Exception as err:
            pn_async_create(
                hass,
                f"Error reading 0x{address:04X}: {err}",
                title=f"Read 0x{address:04X}",
                notification_id=f"optov_read_{address}",
            )
            raise
        answer: dict[str, Any] = {
            "address": f"0x{address:04X}",
            "bytes": length,
            "raw": raw.hex(" ").upper(),
            "value": int.from_bytes(raw, "little", signed=True),
        }
        if item is not None:
            # The catalog knows this one, so the answer is its value rather than its bytes.
            answer["name"] = item["name"]
            answer["value"] = coordinator.decode_datapoint(item, raw)
            if item.get("unit"):
                answer["unit"] = item["unit"]
        pn_async_create(
            hass,
            "\n".join(f"{key}: {value}" for key, value in answer.items()),
            title=f"Read 0x{address:04X}",
            notification_id=f"optov_read_{address}",
        )
        return answer

    async def write_datapoint(call: ServiceCall) -> None:
        coordinator = _one_coordinator(hass, call)
        address = parse_address(call.data["address"])
        data = bytes.fromhex(str(call.data["data"]).replace(" ", "").replace("0x", ""))
        try:
            await coordinator.async_write_custom_datapoint(address, data)
        except OptolinkDeviceError as err:
            raise _refused(err, address) from err
        pn_async_create(
            hass,
            f"Wrote {len(data)} bytes to 0x{address:04X}: {data.hex(' ').upper()}",
            title=f"Write 0x{address:04X}",
            notification_id=f"optov_write_{address}",
        )

    async def read_schedule(call: ServiceCall) -> ServiceResponse:
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
        return {"schedule": key, "weekly_schedule": coordinator.schedules.get(key, {})}

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

    # The two reading services answer their caller as well as posting the notification, so a
    # script can use the value without going through an entity.
    answers = SupportsResponse.OPTIONAL
    for name, handler, response in (
        ("refresh_all", refresh_all, SupportsResponse.NONE),
        ("sync_clock", sync_clock, SupportsResponse.NONE),
        ("read_datapoint", read_datapoint, answers),
        ("write_datapoint", write_datapoint, SupportsResponse.NONE),
        ("read_schedule", read_schedule, answers),
        ("set_schedule_day", set_schedule_day, SupportsResponse.NONE),
        ("set_schedule_window", set_schedule_window, SupportsResponse.NONE),
    ):
        hass.services.async_register(DOMAIN, name, handler, supports_response=response)
