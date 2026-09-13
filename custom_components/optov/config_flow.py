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
    ConfigEntry,
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

MANUAL = "manual"
CONF_CATALOG_FILE = "catalog_file"


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
        # Which catalog this controller will use, settled before anything else happens.
        self._catalog: str | None = None

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
        errors: dict[str, str] = {}
        catalogs = await self.hass.async_add_executor_job(
            catalog_db.list_catalogs, self.hass.config.path(DOMAIN)
        )

        if user_input is not None:
            if user_input.get(CONF_CATALOG_FILE):
                try:
                    self._catalog = await self.hass.async_add_executor_job(
                        _install_uploaded_catalog,
                        self.hass,
                        user_input[CONF_CATALOG_FILE],
                        self.hass.config.path(DOMAIN),
                    )
                except catalog_db.CatalogSchemaError as err:
                    errors["base"] = (
                        "catalog_outdated" if err.outdated else "catalog_too_new"
                    )
                except ValueError:
                    errors["base"] = "catalog_invalid"
                except Exception:
                    errors["base"] = "catalog_failed"
                else:
                    return await self.async_step_user()
            elif user_input.get(CONF_CATALOG):
                # Chosen from the list rather than uploaded, so it has not been through the
                # checks the upload does.
                chosen = next(
                    (c for c in catalogs if c["name"] == user_input[CONF_CATALOG]), None
                )
                if chosen is None:
                    errors["base"] = "catalog_missing"
                elif not chosen["usable"]:
                    errors["base"] = (
                        "catalog_too_new"
                        if chosen["schema_version"] is not None
                        and chosen["schema_version"] > catalog_db.CATALOG_SCHEMA_VERSION
                        else "catalog_outdated"
                    )
                else:
                    self._catalog = chosen["name"]
                    return await self.async_step_user()
            else:
                errors["base"] = "catalog_missing"

        schema: dict[Any, Any] = {}
        if catalogs:
            # Choosing an existing one, with what is in each so they can be told apart.
            schema[vol.Optional(CONF_CATALOG, default=catalogs[0]["name"])] = (
                SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            SelectOptionDict(value=c["name"], label=c["label"])
                            for c in catalogs
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                )
            )
        schema[vol.Optional(CONF_CATALOG_FILE)] = FileSelector(
            # A catalog as the compiler leaves it. `.xz` is still taken because an older
            # one may be compressed; it is unpacked once, on the way in.
            FileSelectorConfig(accept=".db,.xz")
        )
        return self.async_show_form(
            step_id="catalog", data_schema=vol.Schema(schema), errors=errors
        )

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
