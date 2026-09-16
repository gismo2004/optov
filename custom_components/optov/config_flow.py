"""Setup and options.

Setup offers the serial ports Home Assistant knows, which include the proxies of every
ESPHome node, falling back to typing the connection of a node it does not have. Nothing is read from the controller during
setup: the first poll identifies it and builds the entity set, so setup stays fast and does not
touch a serial link that a previous instance may still be releasing.
"""

import os
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    SerialPortSelector,
)

from . import catalog_db
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
)
from .translate import async_ui_text

CONF_CATALOG_FILE = "catalog_file"
CONF_DELETE = "delete"


def _install_uploaded_catalog(
    hass: HomeAssistant, file_id: str, config_dir: str
) -> str:
    """Move an uploaded file into place as a catalog and return its name. Executor only.

    `process_uploaded_file` hands over a path and deletes the file when the block ends, so the
    verification and the copy both happen inside it. The name the file arrived with is kept,
    so several catalogs can sit side by side under names their owner recognises.
    """
    with process_uploaded_file(hass, file_id) as path:
        installed = catalog_db.install_catalog(str(path), config_dir, path.name)
    return os.path.basename(installed)


class OptoVConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a controller."""

    VERSION = 1

    def __init__(self) -> None:
        # Which catalog this controller will use, settled before anything else happens, and
        # whether it arrived as an upload, which may have replaced a file of the same name.
        self._catalog: str | None = None
        self._uploaded = False
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
            else:
                # None to use, one that cannot be read, or several with no way to guess which.
                return await self.async_step_catalog()

        if user_input is not None:
            device = str(user_input[CONF_DEVICE]).strip()
            if "://" not in device:
                return await self.async_step_manual({CONF_HOST: device, **user_input})
            return await self._async_create(device, *self._proxy_of(device), user_input)

        languages = await self._async_languages()
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE): SerialPortSelector(),
                vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): vol.In(languages),
                vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                    int, vol.Range(min=5, max=600)
                ),
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
        placeholders = {"entries": ""}

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
        if not user_input.get(CONF_CATALOG_FILE) and (kept := user_input.get(CONF_CATALOG)):
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
                    _install_uploaded_catalog, self.hass, file_id, self.hass.config.path(DOMAIN)
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
                SelectOptionDict(value=c["name"], label=await self._async_catalog_label(c))
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
            return label
        note = await async_ui_text(self.hass, f"catalog_option_{self._behind(catalog)}")
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
        language = options.get(
            CONF_LANGUAGE, entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        )
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
        if entry.state is not ConfigEntryState.LOADED or (self._uploaded and not changed):
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
        current = self._get_reconfigure_entry().entry_id if self._reconfiguring else None
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
                device, host, int(user_input[CONF_PORT]), instance, proxy_name, user_input
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
    """Language, entity tiers, polling and housekeeping."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options
        # Entries created before language and interval moved into options carry them in data.
        language = current.get(
            CONF_LANGUAGE, self.config_entry.data.get(CONF_LANGUAGE, DEFAULT_LANGUAGE)
        )
        interval = current.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
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
        return self.async_show_form(step_id="init", data_schema=schema)
