"""Setup and options.

Setup offers the serial ports Home Assistant knows, which include the proxies of every
ESPHome node, falling back to typing the connection of a node it does not have. Nothing is
read from the controller during setup: the first poll identifies it and builds the entity
set, so setup stays fast and does not touch a serial link that a previous instance may still
be releasing.
"""

import contextlib
import logging
import os
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    FileSelector,
    FileSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    SerialPortSelector,
    TextSelector,
)

from . import catalog_db, custom_file, optolink
from .catalog_db import get_available_languages
from .const import (
    CONF_CATALOG,
    CONF_DEVICE,
    CONF_ENABLE_CODING2,
    CONF_ENABLE_COMMISSIONING,
    CONF_ENABLE_DIAGNOSTICS,
    CONF_ENABLE_EXPERT,
    CONF_ENCRYPTION_KEY,
    CONF_HOST,
    CONF_INSTANCE,
    CONF_LANGUAGE,
    CONF_PORT,
    CONF_PROXY_NAME,
    CONF_SCAN_INTERVAL,
    CONF_SYNC_CLOCK,
    DEFAULT_ENABLE_CODING2,
    DEFAULT_ENABLE_COMMISSIONING,
    DEFAULT_ENABLE_DIAGNOSTICS,
    DEFAULT_ENABLE_EXPERT,
    DEFAULT_LANGUAGE,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SYNC_CLOCK,
    DOMAIN,
    option,
)
from .custom_entities import (
    KEY_CURRENT,
    KEY_DEVICE_CLASS,
    KEY_HEATING,
    KEY_MODE,
    KEY_MODES,
    KEY_NAME,
    KEY_ON,
    KEY_PRESETS,
    KEY_TARGET,
    KEY_WRITE,
    MODES,
    PRESETS_OFFERED,
    SECTION_CLIMATE,
    SECTION_ONOFF,
    SECTION_WATER_HEATER,
    SECTIONS,
    check_device_class,
    default_on_value,
    device_classes,
    find_item,
    onoff_entity,
    shared_modes,
    two_state_items,
    write_values,
)
from .optolink import IDENT_ADDRESS, IDENT_LENGTH, OptolinkClient
from .translate import async_ui_text

_LOGGER = logging.getLogger(__name__)

CONF_CATALOG_FILE = "catalog_file"
CONF_DELETE = "delete"


def _install_upload(
    hass: HomeAssistant, file_id: str, install: Callable[[Path], Any]
) -> Any:
    """Hand an uploaded file to `install`, which checks it and puts it in place. Executor only.

    `process_uploaded_file` hands over a path and deletes the file when the block ends, so the
    check and the copy both happen inside it. Used for catalogs and custom entities files.
    """
    with process_uploaded_file(hass, file_id) as path:
        return install(path)


def _install_catalog(config_dir: str, path: Path) -> str:
    """Install an uploaded catalog and return its name. The name the file arrived with is
    kept, so several catalogs can sit side by side under names their owner recognises."""
    return os.path.basename(
        catalog_db.install_catalog(str(path), config_dir, path.name)
    )


class OptoVConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a controller."""

    VERSION = 1

    def __init__(self) -> None:
        # Which catalog this controller will use, settled before anything else happens, and
        # whether it arrived as an upload, which may have replaced a file of the same name.
        self._catalog: str | None = None
        self._uploaded = False
        # What the controller said it is, ready to be shown where a catalog is asked for, and
        # the port it said it on, so it does not have to be picked twice.
        self._ident: str = ""
        self._device: str = ""
        # Catalogs deleted on the way, so the closing message can name them.
        self._deleted: list[str] = []

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return OptoVOptionsFlow()

    async def _async_languages(self) -> list:
        """The languages the chosen catalog carries text in.

        Which languages are on offer is a property of the catalog, not of the integration: a
        catalog built for two languages can only be shown in those two.
        """
        path = os.path.join(self.hass.config.path(DOMAIN), self._catalog or "")
        return await self.hass.async_add_executor_job(get_available_languages, path)

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the serial ports Home Assistant knows, or take a node it does not have.

        A missing catalog is dealt with first: without one there is nothing to set up, and
        asking for it here is far kinder than letting setup fail afterwards.

        The list is Home Assistant's own, so it groups the serial proxies of every ESPHome node
        and marks the ones another integration already holds. Nothing in the API tells a port
        wired to a controller from any other, so which one it is stays the user's to say; a
        wrong one simply fails to start. Anything entered by hand that is not a port URL is
        taken for the address of a node Home Assistant does not have, whose connection details
        the next step asks for.
        """
        if self._catalog is None:
            catalogs = await self.hass.async_add_executor_job(
                catalog_db.list_catalogs, self.hass.config.path(DOMAIN)
            )
            if len(catalogs) == 1 and catalogs[0]["usable"]:
                self._catalog = catalogs[0]["name"]
            elif any(c["usable"] for c in catalogs):
                # Several to choose from: the choice is the user's, and the catalogs are here.
                return await self.async_step_catalog()
            else:
                # Nothing usable is here, so a catalog has to be built -- and it is built for a
                # system id the controller itself can be asked for. Offer to ask it first.
                return await self.async_step_identify()

        if user_input is not None:
            device = str(user_input[CONF_DEVICE]).strip()
            if "://" not in device:
                return await self.async_step_manual({CONF_HOST: device, **user_input})
            return await self._async_create(device, *self._proxy_of(device), user_input)

        languages = await self._async_languages()
        # A port already picked to ask the controller its identity is offered again here, so
        # nobody has to find it twice.
        device_key = (
            vol.Required(CONF_DEVICE, description={"suggested_value": self._device})
            if self._device
            else vol.Required(CONF_DEVICE)
        )
        schema = vol.Schema(
            {
                device_key: SerialPortSelector(),
                vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): vol.In(
                    languages
                ),
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): vol.All(int, vol.Range(min=5, max=600)),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    @callback
    def _proxy_of(self, device: str) -> tuple[str, int, int, str]:
        """The node's address and API port, the proxy's position on it and its name, from a URL.

        A serial proxy of a node Home Assistant has is addressed by name; the position is what
        an unnamed one falls back to, in the entity identities, so both are kept.
        """
        url = urlparse(device)
        name = parse_qs(url.query).get("port_name", [""])[0]
        node = self.hass.config_entries.async_get_entry(url.path.strip("/"))
        info = getattr(getattr(node, "runtime_data", None), "device_info", None)
        names = [proxy.name for proxy in getattr(info, "serial_proxies", [])]
        if node is None:
            return url.hostname or "", url.port or DEFAULT_PORT, 0, name
        return (
            node.data.get(CONF_HOST, ""),
            node.data.get(CONF_PORT, DEFAULT_PORT),
            names.index(name) if name in names else 0,
            name,
        )

    async def async_step_identify(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the controller which controller it is, so a catalog can be built for it.

        A catalog is built for a system id, and without a catalog there is no way to learn that
        id from the integration -- the one thing a new user needs before anything else works.
        The read needs no catalog, only the port, so it happens here: four bytes of DeviceIdent
        and, where the identification extension calls for it, one more register.

        Skipping is allowed throughout. Someone who already knows their id, or whose node is not
        one Home Assistant holds, goes straight on to the catalog.
        """
        if user_input is not None:
            device = str(user_input.get(CONF_DEVICE) or "").strip()
            if device and "://" in device:
                self._device = device
                self._ident = await self._async_read_ident(device)
            return await self.async_step_catalog()

        return self.async_show_form(
            step_id="identify",
            data_schema=vol.Schema({vol.Optional(CONF_DEVICE): SerialPortSelector()}),
        )

    async def _async_read_ident(self, device: str) -> str:
        """The controller's identification as a line of text, or why it could not be read.

        Opened and closed for this one question: the entry does not exist yet, so there is no
        connection to borrow, and leaving one behind would hold the port against the setup that
        follows.
        """
        host = self._proxy_of(device)[0]
        client = OptolinkClient(device, host)
        try:
            await client.connect()
            ident = await client.read_raw(IDENT_ADDRESS, IDENT_LENGTH)
            sys_id = int.from_bytes(ident[0:2], "big")
            hw_index, sw_index = ident[2], ident[3]
            f0 = None
            if (sys_id & 0xFF) in range(0xC0, 0xCC) and sw_index >= 200:
                # The same narrow guard the coordinator uses: within this range the system id
                # alone does not separate the variants, and a catalog built without it can be
                # built for the wrong one.
                with contextlib.suppress(Exception):
                    f0 = int.from_bytes(await client.read_raw(0x00F0, 2), "little")
        except Exception as err:  # noqa: BLE001 - any failure is reported, none is fatal
            _LOGGER.debug("Could not identify the controller on %s: %s", host, err)
            return await async_ui_text(self.hass, "identify_failed")
        finally:
            with contextlib.suppress(Exception):
                await client.disconnect()
        # Also in the log, because the catalog it is needed for is built on a computer with the
        # service software on it. Someone setting up from a phone reads the id here, cannot
        # upload anything, and leaves; without this the id would leave with the dialog.
        _LOGGER.info(
            "Controller on %s reports system id 0x%04X (hardware 0x%02X, software 0x%02X%s)",
            host,
            sys_id,
            hw_index,
            sw_index,
            f", identification extension {f0}" if f0 is not None else "",
        )
        if sys_id in optolink.FOREIGN_PROTOCOL_IDS:
            # Identified, but speaking something this integration does not drive. Saying only
            # the id here would send someone off to build a catalog that can never be used.
            return await async_ui_text(
                self.hass, "identify_foreign", system_id=f"0x{sys_id:04X}"
            )
        key = "identify_found_f0" if f0 is not None else "identify_found"
        return await async_ui_text(
            self.hass,
            key,
            system_id=f"0x{sys_id:04X}",
            hardware=f"0x{hw_index:02X}",
            software=f"0x{sw_index:02X}",
            f0=f0,
        )

    async def async_step_catalog(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Settle which catalog this controller uses, uploading one if there is none.

        One catalog describes every controller, so the usual case is a single file and no
        question asked. Someone who built one catalog per controller, which is smaller, ends
        up with several, and then the choice belongs to them: the entry records the name, so
        each hub keeps its own.
        """
        return await self._async_catalog_step("catalog", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point this entry at another catalog, upload a newer build, or delete catalogs.

        One screen, because a flow has no way back from a second one. Offered from the entry's
        menu, and available for an entry whose setup failed as well, which is exactly the state
        an outdated, missing or unreadable catalog leaves it in.
        """
        return await self._async_catalog_step("reconfigure", user_input)

    @property
    def _reconfiguring(self) -> bool:
        return self.source == SOURCE_RECONFIGURE

    @callback
    def _catalog_users(self) -> dict[str, list[str]]:
        """Each catalog file an entry records, with the titles of the entries recording it."""
        users: dict[str, list[str]] = {}
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if name := entry.data.get(CONF_CATALOG):
                users.setdefault(name, []).append(entry.title)
        return users

    async def _async_catalog_step(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Choose a catalog that is already here or upload one, checked either way.

        When reconfiguring, the form also carries a checklist of catalogs to delete. Every
        catalog is listed, since the list selector cannot grey an entry out; the ones an entry
        records, in any state, are labelled as in use and refused if ticked. The entry being
        reconfigured counts too: to delete its catalog, point it elsewhere first.
        """
        config_dir = self.hass.config.path(DOMAIN)
        catalogs = await self.hass.async_add_executor_job(
            catalog_db.list_catalogs, config_dir
        )
        users = self._catalog_users() if self._reconfiguring else {}
        errors: dict[str, str] = {}
        # The structure version this integration was built against, so that the version shown
        # against each catalog can be compared with what is wanted without leaving the form.
        # It is a property of the integration alone, known before any controller is reachable.
        placeholders = {
            "entries": "",
            "needed": str(catalog_db.CATALOG_SCHEMA_VERSION),
            "oldest": str(catalog_db.CATALOG_SCHEMA_MIN),
            # What the controller said stands on its own, above the text that follows it; a
            # blank one must leave no gap at all.
            "identified": f"{self._ident}\n\n" if self._ident else "",
        }

        if user_input is not None:
            # Deletions first, so that a catalog picked from the list is one still there, and
            # nothing is deleted while something in the choice is wrong.
            error = await self._async_delete_ticked(user_input, users, placeholders)
            catalogs = [c for c in catalogs if c["name"] not in self._deleted]
            error = error or await self._async_take_choice(user_input, catalogs)
            if error is None:
                return await self._async_catalog_settled()
            errors["base"] = error

        return self.async_show_form(
            step_id=step_id,
            data_schema=await self._async_catalog_schema(catalogs, users),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def _async_delete_ticked(
        self,
        user_input: dict[str, Any],
        users: dict[str, list[str]],
        placeholders: dict[str, str],
    ) -> str | None:
        """Delete the catalogs ticked for it; an error key if any is still in use."""
        to_delete = user_input.get(CONF_DELETE) or []
        if not to_delete:
            return None
        holders = {name: set(titles) for name, titles in users.items()}
        if not user_input.get(CONF_CATALOG_FILE) and (
            kept := user_input.get(CONF_CATALOG)
        ):
            # The catalog being picked for this entry is about to be in use as well.
            holders.setdefault(kept, set()).add(self._get_reconfigure_entry().title)
        if in_use := [name for name in to_delete if name in holders]:
            placeholders["entries"] = ", ".join(
                f"{name} ({', '.join(sorted(holders[name]))})" for name in in_use
            )
            return "catalog_in_use"
        for name in to_delete:
            await self.hass.async_add_executor_job(
                catalog_db.remove_catalog, self.hass.config.path(DOMAIN), name
            )
            self._deleted.append(name)
        return None

    async def _async_take_choice(
        self, user_input: dict[str, Any], catalogs: list[dict[str, Any]]
    ) -> str | None:
        """Settle self._catalog from an upload or a pick; an error key if neither works."""
        if file_id := user_input.get(CONF_CATALOG_FILE):
            try:
                self._catalog = await self.hass.async_add_executor_job(
                    _install_upload,
                    self.hass,
                    file_id,
                    partial(_install_catalog, self.hass.config.path(DOMAIN)),
                )
            except catalog_db.CatalogSchemaError as err:
                return "catalog_outdated" if err.outdated else "catalog_too_new"
            except ValueError:
                return "catalog_invalid"
            except Exception:
                return "catalog_failed"
            self._uploaded = True
            self._async_reload_entries_using(self._catalog)
            return None
        if name := user_input.get(CONF_CATALOG):
            # Picked from the list rather than uploaded, so it has not been through the checks
            # the upload does.
            chosen = next((c for c in catalogs if c["name"] == name), None)
            if chosen is None:
                return "catalog_missing"
            if not chosen["usable"]:
                return f"catalog_{self._behind(chosen)}"
            self._catalog = name
            return None
        if self._deleted:
            # Only deletions asked for; the entry keeps the catalog it has.
            self._catalog = self._get_reconfigure_entry().data.get(CONF_CATALOG)
            return None
        return "catalog_missing"

    async def _async_catalog_schema(
        self, catalogs: list[dict[str, Any]], users: dict[str, list[str]]
    ) -> vol.Schema:
        """The form: pick from what is here, upload, and when reconfiguring, tick to delete."""
        schema: dict[Any, Any] = {}
        if catalogs:
            names = [c["name"] for c in catalogs]
            current = (
                self._get_reconfigure_entry().data.get(CONF_CATALOG)
                if self._reconfiguring
                else None
            )
            options = [
                SelectOptionDict(
                    value=c["name"], label=await self._async_catalog_label(c)
                )
                for c in catalogs
            ]
            default = current if current in names else names[0]
            schema[vol.Optional(CONF_CATALOG, default=default)] = SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
            )
        schema[vol.Optional(CONF_CATALOG_FILE)] = FileSelector(
            # A catalog as the compiler leaves it. `.xz` is still taken because an older
            # one may be compressed; it is unpacked once, on the way in.
            FileSelectorConfig(accept=".db,.xz")
        )
        if self._reconfiguring and catalogs:
            deletable = []
            for c in catalogs:
                label = await self._async_catalog_label(c)
                if c["name"] in users:
                    in_use = await async_ui_text(
                        self.hass,
                        "catalog_option_in_use",
                        entries=", ".join(sorted(users[c["name"]])),
                    )
                    label = f"{label} – {in_use}"
                deletable.append(SelectOptionDict(value=c["name"], label=label))
            schema[vol.Optional(CONF_DELETE, default=[])] = SelectSelector(
                SelectSelectorConfig(
                    options=deletable, multiple=True, mode=SelectSelectorMode.LIST
                )
            )
        return vol.Schema(schema)

    @staticmethod
    def _behind(catalog: dict[str, Any]) -> str:
        """Which side is behind for a catalog this integration cannot read.

        Rebuilding a catalog that is ahead of the integration would not help, so the two get
        different advice.
        """
        version = catalog["schema_version"]
        if version is not None and version > catalog_db.CATALOG_SCHEMA_VERSION:
            return "too_new"
        return "outdated"

    async def _async_catalog_label(self, catalog: dict[str, Any]) -> str:
        """How a catalog is offered: its file, structure version, controllers and languages.

        A catalog this integration cannot read is still offered, so it can be seen and
        replaced, and it says which side is behind.
        """
        version = catalog["schema_version"]
        label = await async_ui_text(
            self.hass,
            "catalog_option_one" if catalog["devices"] == 1 else "catalog_option",
            name=catalog["name"],
            version=version if version is not None else "–",
            controllers=catalog["devices"],
            languages="/".join(catalog["languages"]),
        )
        if catalog["usable"]:
            if not catalog["behind"]:
                return label
            note = await async_ui_text(self.hass, "catalog_option_behind")
        else:
            note = await async_ui_text(
                self.hass, f"catalog_option_{self._behind(catalog)}"
            )
        return f"{label} – {note}"

    async def _async_catalog_settled(self) -> ConfigFlowResult:
        """Go on once the catalog is settled.

        Adding a controller continues with the serial port. Reconfiguring ends here: the entry
        is updated and reloaded with the catalog. A loaded entry reloads from its own update
        listener, which compares the catalog it started with; one whose setup failed has no
        listener, and a re-upload under the same name changes nothing the listener can see, so
        those two are reloaded explicitly. Home Assistant's combined update-and-reload helper
        is not used, since it warns about, and is set to refuse, entries with an update listener.
        """
        if not self._reconfiguring:
            return await self.async_step_user()

        entry = self._get_reconfigure_entry()
        options = dict(entry.options)
        languages = await self._async_languages()
        language = option(entry, CONF_LANGUAGE, DEFAULT_LANGUAGE)
        if language not in languages:
            # The new catalog does not carry the language in use. Take one it does, so the
            # options page offers a valid choice rather than failing to save.
            options[CONF_LANGUAGE] = (
                DEFAULT_LANGUAGE if DEFAULT_LANGUAGE in languages else languages[0]
            )
        changed = self._catalog != entry.data.get(CONF_CATALOG) or options != dict(
            entry.options
        )
        self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_CATALOG: self._catalog}, options=options
        )
        if entry.state is not ConfigEntryState.LOADED or (
            self._uploaded and not changed
        ):
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        if self._deleted:
            return self.async_abort(
                reason="catalog_removed",
                description_placeholders={"name": ", ".join(self._deleted)},
            )
        return self.async_abort(reason="reconfigure_successful")

    @callback
    def _async_reload_entries_using(self, name: str) -> None:
        """Reload the other entries that read a catalog file an upload just replaced.

        An upload keeps the name it arrived with, so a newer build of a catalog in use replaces
        that file in place. An entry reading it would otherwise go on with an entity set built
        from the file that is gone. That includes one still starting, which a reload waits for
        and then restarts, and one whose setup failed or is waiting to retry, very possibly
        because of the file just replaced. Disabled and unloaded entries are left alone. The
        entry being reconfigured is left to _async_catalog_settled().
        """
        current = (
            self._get_reconfigure_entry().entry_id if self._reconfiguring else None
        )
        active = (
            ConfigEntryState.LOADED,
            ConfigEntryState.SETUP_IN_PROGRESS,
            ConfigEntryState.SETUP_RETRY,
            ConfigEntryState.SETUP_ERROR,
        )
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if (
                entry.entry_id != current
                and entry.disabled_by is None
                and entry.state in active
                and entry.data.get(CONF_CATALOG) == name
            ):
                self.hass.config_entries.async_schedule_reload(entry.entry_id)

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Type the connection details."""
        if user_input is not None and user_input.get(CONF_ENCRYPTION_KEY):
            host = user_input[CONF_HOST]
            instance = int(user_input.get(CONF_INSTANCE, 0))
            proxy_name = user_input.get(CONF_PROXY_NAME, "").strip()
            # This node has no connection of Home Assistant's to travel on, so the URL carries
            # its own: the proxy by name where there is one, by position otherwise.
            query = urlencode(
                {
                    "port_name": proxy_name or instance,
                    "key": user_input[CONF_ENCRYPTION_KEY],
                }
            )
            device = f"esphome://{host}:{user_input[CONF_PORT]}/?{query}"
            return await self._async_create(
                device,
                host,
                int(user_input[CONF_PORT]),
                instance,
                proxy_name,
                user_input,
            )

        given = user_input or {}
        languages = await self._async_languages()
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=given.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_PORT, default=given.get(CONF_PORT, DEFAULT_PORT)
                ): int,
                vol.Required(CONF_ENCRYPTION_KEY, default=""): str,
                vol.Optional(
                    CONF_INSTANCE, default=given.get(CONF_INSTANCE, 0)
                ): vol.All(int, vol.Range(min=0, max=7)),
                vol.Optional(
                    CONF_PROXY_NAME, default=given.get(CONF_PROXY_NAME, "")
                ): str,
                vol.Optional(
                    CONF_LANGUAGE, default=given.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
                ): vol.In(languages),
                vol.Optional(
                    CONF_SCAN_INTERVAL,
                    default=given.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                ): vol.All(int, vol.Range(min=5, max=600)),
            }
        )
        return self.async_show_form(step_id="manual", data_schema=schema)

    async def _async_create(
        self,
        device: str,
        host: str,
        port: int,
        instance: int,
        proxy_name: str,
        user_input: dict[str, Any],
    ) -> ConfigFlowResult:
        """Record the port and go.

        The address and the proxy travel with the URL because the port is looked up by them at
        every start: a URL of a node Home Assistant has names that node's config entry, and
        that name changes when the node is set up again.
        """
        # Identified by the port, not the node: one node can carry several, and each serves
        # exactly one client. The node's API port is part of it because it always was: an id
        # that does not match an existing entry's would let the same port be added twice.
        await self.async_set_unique_id(f"optov_{host}_{port}_{instance}")
        self._abort_if_unique_id_configured()
        data = {
            CONF_DEVICE: device,
            CONF_HOST: host,
            CONF_INSTANCE: instance,
            CONF_CATALOG: self._catalog,
        }
        if proxy_name:
            data[CONF_PROXY_NAME] = proxy_name
        return self.async_create_entry(
            title=f"{host} · {proxy_name}" if proxy_name else host,
            data=data,
            options={
                CONF_LANGUAGE: user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                CONF_SCAN_INTERVAL: user_input.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
            },
        )


class OptoVOptionsFlow(OptionsFlow):
    """Settings, and the custom entities the user builds from the controller's values."""

    def __init__(self) -> None:
        self._device: str | None = None
        self._source: str | None = None
        self._edit: str | None = None
        # A thermostat or water heater being added or edited.
        self._section: str | None = None
        self._draft: dict[str, Any] = {}
        self._editing: str | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "custom"])

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options
        # Entries created before language and interval moved into options carry them in data.
        language = option(self.config_entry, CONF_LANGUAGE, DEFAULT_LANGUAGE)
        interval = option(self.config_entry, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        languages = await self.hass.async_add_executor_job(
            get_available_languages,
            os.path.join(
                self.hass.config.path(DOMAIN),
                self.config_entry.data.get(CONF_CATALOG, ""),
            ),
        )
        schema = vol.Schema(
            {
                vol.Optional(CONF_LANGUAGE, default=language): vol.In(languages),
                vol.Optional(
                    CONF_ENABLE_DIAGNOSTICS,
                    default=current.get(
                        CONF_ENABLE_DIAGNOSTICS, DEFAULT_ENABLE_DIAGNOSTICS
                    ),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_COMMISSIONING,
                    default=current.get(
                        CONF_ENABLE_COMMISSIONING, DEFAULT_ENABLE_COMMISSIONING
                    ),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_CODING2,
                    default=current.get(CONF_ENABLE_CODING2, DEFAULT_ENABLE_CODING2),
                ): bool,
                vol.Optional(
                    CONF_ENABLE_EXPERT,
                    default=current.get(CONF_ENABLE_EXPERT, DEFAULT_ENABLE_EXPERT),
                ): bool,
                vol.Optional(CONF_SCAN_INTERVAL, default=interval): vol.All(
                    int, vol.Range(min=5, max=600)
                ),
                vol.Optional(
                    CONF_SYNC_CLOCK,
                    default=current.get(CONF_SYNC_CLOCK, DEFAULT_SYNC_CLOCK),
                ): bool,
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    # --- Custom entities (custom_entities.py). They live in a file next to the catalog; every
    # change here rewrites that file and reloads the entry, which builds the entities from it.

    def _coordinator(self):
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return None
        return self.config_entry.runtime_data.coordinator

    def _path(self) -> str:
        return custom_file.custom_file_path(self.hass, self._coordinator().stable_id)

    async def async_step_custom(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """What can be done with custom entities, and how many there are."""
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="custom_not_loaded")
        return self.async_show_menu(
            step_id="custom",
            menu_options=[
                "custom_onoff_add",
                "custom_climate_add",
                "custom_water_heater_add",
                "custom_edit",
                "custom_backup",
            ],
            description_placeholders={
                "count": str(self._count()),
                "path": self._path(),
            },
        )

    def _label(self, item: dict[str, Any]) -> str:
        states = " / ".join(item["options"][v] for v in sorted(item["options"]))
        return f"{item['name']}: {states}"

    def _onoff_candidates(self) -> dict[str, tuple[str, str | None]]:
        """Enabled two-state entities without an on/off entity: entity_id -> (datapoint, device).

        Only enabled ones, because those are what the entity picker can show; a disabled value
        can be enabled first, or named in the file.
        """
        coordinator = self._coordinator()
        eligible = {item["id"] for _, item in two_state_items(coordinator.profile)}
        prefix = f"{coordinator.stable_id}_"
        found: dict[str, tuple[str, str | None]] = {}
        registry = er.async_get(self.hass)
        for reg_entry in er.async_entries_for_config_entry(
            registry, self.config_entry.entry_id
        ):
            if reg_entry.domain not in ("sensor", "select") or reg_entry.disabled_by:
                continue
            if not reg_entry.unique_id.startswith(prefix):
                continue
            item_id = reg_entry.unique_id[len(prefix) :]
            if item_id in eligible and item_id not in coordinator.custom_sources:
                found[reg_entry.entity_id] = (item_id, reg_entry.device_id)
        return found

    def _count(self) -> int:
        coordinator = self._coordinator()
        return len(coordinator.custom_unique_ids())

    def _entities(self, section: str) -> list[dict[str, Any]]:
        return list(self._coordinator().custom[section])

    async def _async_save(
        self, section: str, entities: list[dict[str, Any]]
    ) -> ConfigFlowResult:
        """Write the file with one section changed, the rest as they are, and reload."""
        coordinator = self._coordinator()
        custom = {**coordinator.custom, section: entities}
        await custom_file.async_save(self.hass, coordinator, custom)
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return self.async_abort(reason="custom_saved")

    def _onoff_schema(
        self, platform: str, item: dict[str, Any], on: int, device_class: str, name: str
    ) -> dict:
        return {
            vol.Required(KEY_ON, default=str(on)): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=str(v), label=item["options"][v])
                        for v in sorted(item["options"])
                    ],
                    mode=SelectSelectorMode.LIST,
                )
            ),
            vol.Required(KEY_DEVICE_CLASS, default=device_class): SelectSelector(
                SelectSelectorConfig(
                    options=device_classes(platform),
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="onoff_device_class",
                )
            ),
            vol.Required(KEY_NAME, default=name): TextSelector(),
        }

    async def async_step_custom_onoff_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First the device, so the next list holds only that device's values."""
        if self._coordinator() is None:
            return self.async_abort(reason="custom_not_loaded")
        candidates = self._onoff_candidates()
        if not candidates:
            return self.async_abort(reason="custom_onoff_none")
        devices: dict[str, int] = {}
        for _, device_id in candidates.values():
            if device_id:
                devices[device_id] = devices.get(device_id, 0) + 1
        if user_input is not None or len(devices) <= 1:
            self._device = (user_input or {}).get("device") or next(iter(devices), None)
            return await self.async_step_custom_onoff_value()
        registry = dr.async_get(self.hass)
        options = []
        for device_id, count in devices.items():
            device = registry.async_get(device_id)
            name = (device.name_by_user or device.name) if device else device_id
            options.append(SelectOptionDict(value=device_id, label=f"{name} ({count})"))
        options.sort(key=lambda o: o["label"].casefold())
        return self.async_show_form(
            step_id="custom_onoff_add",
            data_schema=vol.Schema(
                {
                    vol.Required("device"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    async def async_step_custom_onoff_value(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The value, from Home Assistant's own entity picker, which can be searched."""
        candidates = self._onoff_candidates()
        if user_input is not None and user_input.get("entity") in candidates:
            self._source = user_input["entity"]
            return await self.async_step_custom_onoff_details()
        on_device = sorted(
            entity_id
            for entity_id, (_, device_id) in candidates.items()
            if self._device is None or device_id == self._device
        )
        if not on_device:
            return self.async_abort(reason="custom_onoff_none")
        return self.async_show_form(
            step_id="custom_onoff_value",
            data_schema=vol.Schema(
                {
                    vol.Required("entity"): EntitySelector(
                        EntitySelectorConfig(include_entities=on_device)
                    )
                }
            ),
        )

    async def async_step_custom_onoff_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Everything is preselected, so in the simplest case this is one click."""
        coordinator = self._coordinator()
        candidates = self._onoff_candidates() if coordinator else {}
        if self._source not in candidates:
            return self.async_abort(reason="custom_not_loaded")
        platform, item = find_item(coordinator.profile, candidates[self._source][0])
        if user_input is not None:
            entity = onoff_entity(
                platform,
                item,
                self._source,
                int(user_input[KEY_ON]),
                user_input[KEY_DEVICE_CLASS],
                user_input[KEY_NAME],
            )
            return await self._async_save(
                SECTION_ONOFF, [*self._entities(SECTION_ONOFF), entity]
            )
        return self.async_show_form(
            step_id="custom_onoff_details",
            data_schema=vol.Schema(
                self._onoff_schema(
                    platform,
                    item,
                    default_on_value(item),
                    check_device_class(platform, None),
                    item["name"],
                )
            ),
            description_placeholders={"source": self._label(item)},
        )

    async def async_step_custom_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick a custom entity to change or remove."""
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="custom_not_loaded")
        if not self._count():
            return self.async_abort(reason="custom_empty")
        if user_input is not None:
            section, _, key = user_input["entity"].partition("|")
            if section == SECTION_ONOFF:
                self._edit = key
                return await self.async_step_custom_edit_onoff()
            current = next(
                e for e in coordinator.custom[section] if e["unique_id"] == key
            )
            self._section, self._editing = section, key
            self._draft = {
                k: current.get(k)
                for k in (KEY_NAME, KEY_TARGET, KEY_CURRENT, KEY_MODE, KEY_HEATING)
            }
            self._draft[KEY_PRESETS] = dict(current.get(KEY_PRESETS) or {})
            self._draft[KEY_MODES] = dict(current[KEY_MODES])
            self._draft[KEY_WRITE] = dict(current[KEY_WRITE])
            return await self.async_step_custom_assembled()
        kinds = await self._async_kind_names()
        # An on/off entity is known by its datapoint, the others by their unique id.
        options = [
            SelectOptionDict(
                value=f"{section}|{entity['source' if section == SECTION_ONOFF else 'unique_id']}",
                label=f"{entity[KEY_NAME]} ({kinds[section]})",
            )
            for section, entities in coordinator.custom.items()
            for entity in entities
        ]
        options.sort(key=lambda o: o["label"].casefold())
        return self.async_show_form(
            step_id="custom_edit",
            data_schema=vol.Schema(
                {
                    vol.Required("entity"): SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    async def _async_kind_names(self) -> dict[str, str]:
        """What each kind is called in the edit list, in the Home Assistant language."""
        return {
            section: await async_ui_text(self.hass, f"custom_kind_{section}")
            for section in SECTIONS
        }

    async def async_step_custom_edit_onoff(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        coordinator = self._coordinator()
        onoff = self._entities(SECTION_ONOFF) if coordinator else []
        current = next((e for e in onoff if e["source"] == self._edit), None)
        if current is None:
            return self.async_abort(reason="custom_not_loaded")
        item = current["item"]
        if user_input is not None:
            rest = [e for e in onoff if e["source"] != self._edit]
            if user_input.get("remove"):
                return await self._async_save(SECTION_ONOFF, rest)
            changed = {
                **current,
                KEY_ON: int(user_input[KEY_ON]),
                KEY_DEVICE_CLASS: user_input[KEY_DEVICE_CLASS],
                KEY_NAME: user_input[KEY_NAME].strip() or item["name"],
            }
            return await self._async_save(SECTION_ONOFF, [*rest, changed])
        schema = self._onoff_schema(
            current["platform"],
            item,
            current[KEY_ON],
            current[KEY_DEVICE_CLASS],
            current[KEY_NAME],
        )
        schema[vol.Optional("remove", default=False)] = bool
        return self.async_show_form(
            step_id="custom_edit_onoff",
            data_schema=vol.Schema(schema),
            description_placeholders={"source": self._label(item)},
        )

    # --- Thermostat and water heater: entities, then modes, then (if needed) what is written.

    async def async_step_custom_climate_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_start_assembled(SECTION_CLIMATE)

    async def async_step_custom_water_heater_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_start_assembled(SECTION_WATER_HEATER)

    async def _async_start_assembled(self, section: str) -> ConfigFlowResult:
        if self._coordinator() is None:
            return self.async_abort(reason="custom_not_loaded")
        self._section, self._editing, self._draft = section, None, {KEY_MODES: {}}
        return await self.async_step_custom_assembled()

    def _mode_item(self, entity_id: str | None) -> dict[str, Any] | None:
        """The select of this controller behind an entity id, or None."""
        if not entity_id:
            return None
        coordinator = self._coordinator()
        item_id = custom_file.own_item_id(
            self.hass, coordinator, self.config_entry.entry_id, entity_id
        )
        return (
            custom_file.find_select(coordinator.profile, item_id) if item_id else None
        )

    async def async_step_custom_assembled(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Which entities the thermostat or water heater is built from."""
        section, draft = self._section, self._draft
        climate = section == SECTION_CLIMATE
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("remove") and self._editing:
                rest = [
                    e
                    for e in self._entities(section)
                    if e["unique_id"] != self._editing
                ]
                return await self._async_save(section, rest)
            for key in (KEY_NAME, KEY_TARGET, KEY_CURRENT, KEY_MODE, KEY_HEATING):
                draft[key] = user_input.get(key) or None
            if draft[KEY_MODE] and self._mode_item(draft[KEY_MODE]) is None:
                errors[KEY_MODE] = "custom_mode_foreign"
            if not errors:
                if draft[KEY_MODE]:
                    return await self.async_step_custom_modes()
                draft[KEY_MODES], draft[KEY_WRITE] = {}, {}
                return await self._async_after_modes()
        entity = EntitySelector
        schema: dict = {
            vol.Required(KEY_NAME, default=draft.get(KEY_NAME) or ""): TextSelector(),
            vol.Required(
                KEY_TARGET, description={"suggested_value": draft.get(KEY_TARGET)}
            ): entity(EntitySelectorConfig(domain=["number", "input_number"])),
        }
        mode_key = vol.Required if climate else vol.Optional
        schema[
            mode_key(KEY_MODE, description={"suggested_value": draft.get(KEY_MODE)})
        ] = entity(EntitySelectorConfig(integration=DOMAIN, domain="select"))
        schema[
            vol.Optional(
                KEY_CURRENT, description={"suggested_value": draft.get(KEY_CURRENT)}
            )
        ] = entity(EntitySelectorConfig(domain="sensor", device_class="temperature"))
        if climate:
            schema[
                vol.Optional(
                    KEY_HEATING, description={"suggested_value": draft.get(KEY_HEATING)}
                )
            ] = entity(EntitySelectorConfig(domain="binary_sensor"))
        if self._editing:
            schema[vol.Optional("remove", default=False)] = bool
        return self.async_show_form(
            step_id="custom_climate" if climate else "custom_water_heater",
            data_schema=vol.Schema(schema),
            errors=errors,
        )

    async def async_step_custom_climate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_custom_assembled(user_input)

    async def async_step_custom_water_heater(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self.async_step_custom_assembled(user_input)

    async def async_step_custom_modes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """One choice per value of the mode select: which Home Assistant mode it is."""
        draft = self._draft
        options = self._mode_item(draft[KEY_MODE])["options"]
        errors: dict[str, str] = {}
        if user_input is not None:
            modes = {
                raw: user_input[label]
                for raw, label in options.items()
                if user_input.get(label, "unused") != "unused"
            }
            if not modes:
                errors["base"] = "custom_modes_empty"
            else:
                draft[KEY_MODES] = modes
                draft[KEY_WRITE] = {
                    m: v
                    for m, v in (draft.get(KEY_WRITE) or {}).items()
                    if modes.get(v) == m
                }
                if shared_modes(modes):
                    return await self.async_step_custom_write()
                draft[KEY_WRITE] = write_values(modes, {})
                return await self._async_after_modes()
        choices = ["unused", *MODES[self._section]]
        current = draft.get(KEY_MODES) or {}
        schema = {
            vol.Required(label, default=current.get(raw, "unused")): SelectSelector(
                SelectSelectorConfig(
                    options=choices,
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="custom_mode",
                )
            )
            for raw, label in sorted(options.items())
        }
        return self.async_show_form(
            step_id="custom_modes", data_schema=vol.Schema(schema), errors=errors
        )

    async def async_step_custom_write(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Where several controller modes share a Home Assistant mode: which one it writes."""
        draft = self._draft
        options = self._mode_item(draft[KEY_MODE])["options"]
        shared = shared_modes(draft[KEY_MODES])
        if user_input is not None:
            chosen = {mode: int(user_input[f"write_{mode}"]) for mode in shared}
            draft[KEY_WRITE] = write_values(draft[KEY_MODES], chosen)
            return await self._async_after_modes()
        before = draft.get(KEY_WRITE) or {}
        schema = {
            vol.Required(f"write_{mode}", default=str(before.get(mode, values[0]))): (
                SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=str(v), label=options[v])
                            for v in values
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            )
            for mode, values in shared.items()
        }
        return self.async_show_form(
            step_id="custom_write", data_schema=vol.Schema(schema)
        )

    async def _async_after_modes(self) -> ConfigFlowResult:
        """A thermostat may still take presets; a water heater is complete."""
        if self._section == SECTION_CLIMATE:
            return await self.async_step_custom_presets()
        return await self._async_save_assembled()

    async def async_step_custom_presets(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optional: a switch or on/off setting that turns each preset on."""
        draft = self._draft
        before = dict(draft.get(KEY_PRESETS) or {})
        if user_input is not None:
            # Presets only the file can name (home, sleep, activity) stay as they are.
            draft[KEY_PRESETS] = {
                name: entity_id
                for name, entity_id in before.items()
                if name not in PRESETS_OFFERED
            }
            draft[KEY_PRESETS].update(
                {
                    name: user_input[name]
                    for name in PRESETS_OFFERED
                    if user_input.get(name)
                }
            )
            return await self._async_save_assembled()
        schema = {
            vol.Optional(
                name, description={"suggested_value": before.get(name)}
            ): EntitySelector(
                EntitySelectorConfig(integration=DOMAIN, domain=["switch", "select"])
            )
            for name in PRESETS_OFFERED
        }
        return self.async_show_form(
            step_id="custom_presets", data_schema=vol.Schema(schema)
        )

    async def _async_save_assembled(self) -> ConfigFlowResult:
        coordinator = self._coordinator()
        section, draft = self._section, self._draft
        entry = {
            KEY_NAME: (draft.get(KEY_NAME) or "").strip() or None,
            KEY_TARGET: draft[KEY_TARGET],
            KEY_CURRENT: draft.get(KEY_CURRENT),
            KEY_MODE: draft.get(KEY_MODE),
            KEY_MODES: draft.get(KEY_MODES) or {},
            KEY_WRITE: write_values(
                draft.get(KEY_MODES) or {}, draft.get(KEY_WRITE) or {}
            ),
        }
        if section == SECTION_CLIMATE:
            entry[KEY_HEATING] = draft.get(KEY_HEATING)
            entry[KEY_PRESETS] = draft.get(KEY_PRESETS) or {}
        resolved, skipped = custom_file.resolve_assembled(
            self.hass, coordinator, self.config_entry.entry_id, section, [entry]
        )
        if not resolved:
            return self.async_abort(
                reason="custom_rejected",
                description_placeholders={"reason": skipped[0]},
            )
        rest = [
            e
            for e in self._entities(section)
            if e["unique_id"] not in (self._editing, resolved[0]["unique_id"])
        ]
        return await self._async_save(section, [*rest, resolved[0]])

    async def async_step_custom_backup(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Download the file, restore one by uploading it, or apply it after editing."""
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="custom_not_loaded")
        path = self._path()
        errors: dict[str, str] = {}
        if user_input is not None:
            if file_id := user_input.get("upload"):
                try:
                    await self.hass.async_add_executor_job(
                        _install_upload,
                        self.hass,
                        file_id,
                        partial(custom_file.install_upload, target=path),
                    )
                except ValueError:
                    errors["upload"] = "custom_invalid"
            if not errors:
                sections, problems, _ = await custom_file.async_read(
                    self.hass, coordinator.stable_id
                )
                custom, skipped = custom_file.resolve_sections(
                    self.hass, coordinator, self.config_entry.entry_id, sections
                )
                count = sum(len(entities) for entities in custom.values())
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
                notes = problems + skipped
                if not notes:
                    return self.async_abort(
                        reason="custom_applied",
                        description_placeholders={"count": str(count)},
                    )
                return self.async_abort(
                    reason="custom_applied_notes",
                    description_placeholders={
                        "count": str(count),
                        "skipped": "\n".join(f"- {line}" for line in notes),
                    },
                )
        exists = await self.hass.async_add_executor_job(os.path.exists, path)
        link = (
            async_sign_path(
                self.hass,
                custom_file.DOWNLOAD_URL.format(entry_id=self.config_entry.entry_id),
                timedelta(minutes=10),
            )
            if exists
            else ""
        )
        return self.async_show_form(
            step_id="custom_backup",
            data_schema=vol.Schema(
                {
                    vol.Optional("upload"): FileSelector(
                        FileSelectorConfig(accept=".yaml,.yml")
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "path": path,
                # A plain link to Home Assistant's own address is taken over by the frontend's
                # router and lands on a dashboard. Opened in a new tab the browser fetches it
                # itself, and the attachment header makes that a download.
                "download": (
                    f'<a href="{link}" target="_blank">{os.path.basename(path)}</a>'
                    if link
                    else "-"
                ),
            },
        )
