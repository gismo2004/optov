"""Setup and options.

Setup finds ESPHome nodes that expose a serial proxy for the Optolink port and offers them,
falling back to typing the connection details. Nothing is read from the controller during
setup: the first poll identifies it and builds the entity set, so setup stays fast and does not
touch a serial link that a previous instance may still be releasing.
"""

import json
import os
from typing import Any

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
)

from . import catalog_db
from .catalog_db import get_available_languages
from .const import (
    CONF_CATALOG,
    CONF_DEBUG_LOGGING,
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
    DEFAULT_DEBUG_LOGGING,
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

MANUAL = "manual"
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


def _read_json(path: str) -> dict[str, Any]:
    """A JSON file, or {} if it is missing or unreadable. Executor only."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


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

    async def _async_serial_proxies(self) -> dict[str, dict[str, Any]]:
        """Every serial proxy on every ESPHome node, keyed by "<entry id>:<instance>".

        No guessing about which port is wired to a controller. Nothing the API reports
        distinguishes one: a proxy carries a name, an electrical type and its modem pins, and
        the line settings that would actually identify the port -- 4800 baud, even parity, two
        stop bits -- are not advertised at all. Filtering on the name only hides correctly
        wired ports whose owner called them something else, so the list is complete and the
        choice is the user's.

        Ports already taken by another entry are left out, because a proxy serves exactly one
        client: a second one would break both.

        A loaded ESPHome entry reports its proxies in runtime data; one that is not loaded is
        read from its stored device info.
        """
        taken = {
            (entry.data.get(CONF_HOST), entry.data.get(CONF_INSTANCE, 0))
            for entry in self._async_current_entries()
        }
        found: dict[str, dict[str, Any]] = {}
        for entry in self.hass.config_entries.async_entries("esphome"):
            host = entry.data.get("host")
            if not host:
                continue
            runtime = getattr(entry, "runtime_data", None)
            info = getattr(runtime, "device_info", None) if runtime else None
            if info is not None:
                proxies = list(getattr(info, "serial_proxies", []) or [])
            else:
                stored = await self.hass.async_add_executor_job(
                    _read_json,
                    self.hass.config.path(".storage", f"esphome.{entry.entry_id}"),
                )
                proxies = (
                    stored.get("data", {})
                    .get("device_info", {})
                    .get("serial_proxies", [])
                    or []
                )

            node = entry.title or entry.data.get("device_name", host)
            for instance, proxy in enumerate(proxies):
                if (host, instance) in taken:
                    continue
                raw_name = (
                    proxy.get("name", "")
                    if isinstance(proxy, dict)
                    else getattr(proxy, "name", "")
                ) or ""
                proxy_name = raw_name.strip()
                name = proxy_name or f"port {instance}"
                found[f"{entry.entry_id}:{instance}"] = {
                    # One node with one port needs no port name unless explicitly named; several need telling apart.
                    "title": node
                    if len(proxies) == 1 and not proxy_name
                    else f"{node} · {name}",
                    "host": host,
                    "port": entry.data.get("port", DEFAULT_PORT),
                    "noise_psk": entry.data.get("noise_psk", ""),
                    "instance": instance,
                    "proxy_name": proxy_name,
                }
        return found

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the serial proxies on the network, or go to manual entry.

        A missing catalog is dealt with first: without one there is nothing to set up, and
        asking for it here is far kinder than letting setup fail afterwards.
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
        nodes = await self._async_serial_proxies()
        if not nodes:
            return await self.async_step_manual()

        if user_input is not None:
            chosen = user_input[CONF_DEVICE]
            if chosen == MANUAL:
                return await self.async_step_manual()
            node = nodes[chosen]
            if not node["noise_psk"]:
                # The node's key is not stored on its ESPHome entry; ask for it, with the rest
                # of the details filled in.
                return await self.async_step_manual(
                    {
                        CONF_HOST: node["host"],
                        CONF_PORT: node["port"],
                        CONF_INSTANCE: node["instance"],
                        CONF_PROXY_NAME: node.get("proxy_name", ""),
                        **user_input,
                    }
                )
            return await self._async_create(
                host=node["host"],
                port=node["port"],
                key=node["noise_psk"],
                title=node["title"],
                instance=node["instance"],
                proxy_name=node.get("proxy_name", ""),
                language=user_input[CONF_LANGUAGE],
                scan_interval=user_input[CONF_SCAN_INTERVAL],
            )

        options = [
            SelectOptionDict(value=entry_id, label=f"{node['title']} ({node['host']})")
            for entry_id, node in sorted(
                nodes.items(), key=lambda kv: kv[1]["title"].lower()
            )
        ] + [SelectOptionDict(value=MANUAL, label=MANUAL)]
        languages = await self._async_languages()
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE, default=options[0]["value"]): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                        translation_key="device",
                    )
                ),
                vol.Optional(CONF_LANGUAGE, default=DEFAULT_LANGUAGE): vol.In(
                    languages
                ),
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
                ): vol.All(int, vol.Range(min=5, max=600)),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

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
            proxy_name = user_input.get(CONF_PROXY_NAME, "").strip()
            title = user_input[CONF_HOST]
            if proxy_name:
                title = f"{title} · {proxy_name}"
            return await self._async_create(
                host=user_input[CONF_HOST],
                port=user_input[CONF_PORT],
                key=user_input[CONF_ENCRYPTION_KEY],
                title=title,
                instance=int(user_input.get(CONF_INSTANCE, 0)),
                proxy_name=proxy_name,
                language=user_input.get(CONF_LANGUAGE, DEFAULT_LANGUAGE),
                scan_interval=user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
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
        *,
        host: str,
        port: int,
        key: str,
        title: str,
        instance: int,
        language: str,
        scan_interval: int,
        proxy_name: str = "",
    ) -> ConfigFlowResult:
        # Identified by the port, not the node: one node can carry several, and each serves
        # exactly one client.
        await self.async_set_unique_id(f"optov_{host}_{port}_{instance}")
        self._abort_if_unique_id_configured()
        data = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_ENCRYPTION_KEY: key,
            CONF_INSTANCE: instance,
            CONF_CATALOG: self._catalog,
        }
        if proxy_name:
            data[CONF_PROXY_NAME] = proxy_name
        return self.async_create_entry(
            title=title,
            data=data,
            options={CONF_LANGUAGE: language, CONF_SCAN_INTERVAL: scan_interval},
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
                vol.Optional(
                    CONF_DEBUG_LOGGING,
                    default=current.get(CONF_DEBUG_LOGGING, DEFAULT_DEBUG_LOGGING),
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
