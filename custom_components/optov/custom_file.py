"""The custom entities file of a controller: reading, resolving, writing and the download.

Rules and format are in custom_entities.py.
"""

import logging
import os
from collections.abc import Iterable
from typing import Any

import yaml
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .catalog_db import ENTITY_DOMAINS
from .const import CONF_CUSTOM_FILE, DOMAIN
from .custom_entities import (
    KEY_CONTROLLER,
    KEY_CURRENT,
    KEY_DEVICE_CLASS,
    KEY_HEATING,
    KEY_MODE,
    KEY_MODES,
    KEY_NAME,
    KEY_ON,
    KEY_PRESETS,
    KEY_PROGRAMMES,
    KEY_SAVED,
    KEY_SOFTWARE,
    KEY_TARGET,
    KEY_VALUE,
    KEY_VALUES,
    SECTION_CLIMATE,
    SECTION_ONOFF,
    SECTION_WATER_HEATER,
    assembled_unique_id,
    changed_settings,
    default_on_value,
    find_item,
    off_value,
    onoff_entity,
    parse_file,
    parse_settings,
    render_file,
    render_settings,
    replace_settings,
    restored_by_default,
    settings_items,
    two_state_items,
)

_LOGGER = logging.getLogger(__name__)

# Where the file can be downloaded; the link in the options dialog is signed for a few minutes.
DOWNLOAD_URL = "/api/optov/custom/{entry_id}"


def custom_file_name(stable_id: str) -> str:
    """`custom_<node>_<port>.yaml`: the controller's stable identity (coordinator.stable_id),
    made of the ESPHome node name and the serial proxy name from the node's configuration."""
    return f"custom_{stable_id}.yaml"


def custom_file_path(hass: HomeAssistant, stable_id: str) -> str:
    """Next to the catalog. Removed with the entry, like the catalog; back it up to keep it."""
    return os.path.join(hass.config.path(DOMAIN), custom_file_name(stable_id))


def read_text(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def install_upload(source: str, target: str) -> None:
    """Put an uploaded file in place of the custom entities file. Blocking I/O.

    Checked before it replaces anything, like a catalog: it has to be YAML holding sections.
    Anything else raises ValueError and leaves the current file as it is.
    """
    try:
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        raw = yaml.safe_load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        raise ValueError(f"not a custom entities file: {err}") from err
    if not isinstance(raw, dict):
        raise ValueError("not a custom entities file: no sections")
    write_text(target, text)


async def async_read(
    hass: HomeAssistant, stable_id: str
) -> tuple[dict[str, list[dict[str, Any]]], list[str], bool]:
    """The file's entries per section, unresolved; what was wrong; whether it was readable."""
    path = custom_file_path(hass, stable_id)
    try:
        text = await hass.async_add_executor_job(read_text, path)
        raw = yaml.safe_load(text) if text is not None else None
    except (OSError, yaml.YAMLError) as err:
        return {}, [f"{os.path.basename(path)} cannot be read: {err}"], False
    return parse_file(raw)


def own_item_id(
    hass: HomeAssistant,
    coordinator: Any,
    entry_id: str,
    entity_id: str,
    planned: dict[str, str] | None = None,
) -> str | None:
    """The datapoint id behind one of this controller's entities; None for any other entity.

    Right after the integration was added again, the values are not registered yet. Their
    entity ids are built from the model code (profiles.stable_object_id), so `planned` -- the
    entity id each datapoint will get -- names them just as well.
    """
    reg_entry = er.async_get(hass).async_get(entity_id)
    if reg_entry is None:
        return (planned or {}).get(entity_id)
    prefix = f"{coordinator.stable_id}_"
    if reg_entry.config_entry_id != entry_id or not reg_entry.unique_id.startswith(
        prefix
    ):
        return None
    return reg_entry.unique_id[len(prefix) :]


def _planned_ids(
    coordinator: Any, pairs: Iterable[tuple[str, dict[str, Any]]]
) -> dict[str, str]:
    """The entity id each (platform, datapoint) will get, to the datapoint's id."""
    return {
        f"{ENTITY_DOMAINS[platform]}."
        f"{coordinator.object_id(item.get('circuit'), item['name'])}": item["id"]
        for platform, item in pairs
    }


def resolve_onoff(
    hass: HomeAssistant, coordinator: Any, entry_id: str, entries: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn on/off entries into entities of this controller's datapoints.

    An entity is keyed on the datapoint id; the entity id in the file is only how a person
    names the value. Returns the resolved entries and what was skipped.
    """
    planned = _planned_ids(coordinator, two_state_items(coordinator.profile))
    resolved: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in entries:
        value = entry[KEY_VALUE]
        item_id = own_item_id(hass, coordinator, entry_id, value, planned)
        if item_id is None:
            skipped.append(f"{value}: not an entity of this controller")
            continue
        found = find_item(coordinator.profile, item_id)
        if found is None:
            skipped.append(f"{value}: has no two named states")
            continue
        platform, item = found
        if entry[KEY_ON] not in item["options"]:
            known = ", ".join(
                f"{v} = {item['options'][v]}" for v in sorted(item["options"])
            )
            skipped.append(f"{value}: 'on_value' must be one of {known}")
            continue
        resolved.append(
            onoff_entity(
                platform,
                item,
                value,
                entry[KEY_ON],
                entry[KEY_DEVICE_CLASS],
                entry[KEY_NAME],
            )
        )
    return resolved, skipped


def find_select(profile: Any, item_id: str) -> dict[str, Any] | None:
    """The writable enumerated datapoint (a select) with this id."""
    for item in getattr(profile, "selects", None) or []:
        if item["id"] == item_id:
            return item
    return None


def _entity_exists(hass: HomeAssistant, entity_id: str) -> bool:
    return (
        hass.states.get(entity_id) is not None
        or er.async_get(hass).async_get(entity_id) is not None
    )


def resolve_assembled(
    hass: HomeAssistant,
    coordinator: Any,
    entry_id: str,
    section: str,
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Check thermostat or water heater entries against what exists; resolve the mode select.

    The setpoint, current temperature and "heating" may be any entity of the right kind, since
    they are read and set through Home Assistant. The mode select has to be one of this
    controller's, because the mapping is by its raw values, which only the catalog knows.
    """
    planned = _planned_ids(
        coordinator, (("selects", item) for item in coordinator.profile.selects)
    )
    resolved: list[dict[str, Any]] = []
    skipped: list[str] = []
    for entry in entries:
        target = entry[KEY_TARGET]
        where = f"{section} {target}"
        if target.split(".")[0] not in ("number", "input_number"):
            skipped.append(f"{where}: 'target' must be a number entity")
            continue
        missing = [
            entity_id
            for entity_id in (
                target,
                entry.get(KEY_CURRENT),
                entry.get(KEY_HEATING),
            )
            if entity_id and not _entity_exists(hass, entity_id)
        ]
        if missing:
            skipped.append(f"{where}: {', '.join(missing)} not found")
            continue
        options: dict[int, str] = {}
        if mode := entry.get(KEY_MODE):
            item_id = own_item_id(hass, coordinator, entry_id, mode, planned)
            item = find_select(coordinator.profile, item_id) if item_id else None
            if item is None:
                skipped.append(f"{where}: 'mode' must be a select of this controller")
                continue
            options = item["options"]
            unknown = [v for v in entry[KEY_MODES] if v not in options]
            if unknown:
                known = ", ".join(f"{v} = {options[v]}" for v in sorted(options))
                skipped.append(
                    f"{where}: {', '.join(map(str, unknown))} not a value of {mode} ({known})"
                )
                continue
        preset_on, bad = _resolve_presets(
            hass, coordinator, entry_id, entry.get(KEY_PRESETS) or {}
        )
        if bad:
            skipped.append(f"{where}: {bad}")
            continue
        resolved.append(
            {
                **entry,
                "section": section,
                "unique_id": assembled_unique_id(
                    coordinator.stable_id, section, target
                ),
                "options": options,
                "preset_on": preset_on,
                KEY_NAME: entry.get(KEY_NAME) or target,
            }
        )
    return resolved, skipped


def _resolve_presets(
    hass: HomeAssistant, coordinator: Any, entry_id: str, presets: dict[str, str]
) -> tuple[dict[str, dict[str, Any]], str | None]:
    """For each preset, the entity that turns it on and, for a select, the option that is "on".

    A switch is simply on. A select must be a two-state setting of this controller; its "on"
    option is the one an on/off entity would preselect (raw 1, else the higher value).
    """
    result: dict[str, dict[str, Any]] = {}
    for name, entity_id in presets.items():
        if not _entity_exists(hass, entity_id):
            return {}, f"preset {name}: {entity_id} not found"
        on_label = None
        if entity_id.startswith("select."):
            item_id = own_item_id(hass, coordinator, entry_id, entity_id)
            found = find_item(coordinator.profile, item_id) if item_id else None
            if found is None:
                return (
                    {},
                    f"preset {name}: {entity_id} is not a two-state setting of this controller",
                )
            item = found[1]
            on_label = item["options"][default_on_value(item)]
        result[name] = {"entity": entity_id, "on_label": on_label}
    return result, None


async def async_load(
    hass: HomeAssistant, coordinator: Any, entry_id: str
) -> tuple[dict[str, list[dict[str, Any]]], list[str], bool]:
    """Read and resolve the file: the custom entities per kind, what could not be used, and
    whether the file was readable."""
    sections, problems, readable = await async_read(hass, coordinator.stable_id)
    custom, skipped = resolve_sections(hass, coordinator, entry_id, sections)
    for line in problems + skipped:
        _LOGGER.warning("Custom entities file: %s", line)
    return custom, problems + skipped, readable


def resolve_sections(
    hass: HomeAssistant,
    coordinator: Any,
    entry_id: str,
    sections: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Resolve every section; the custom entities per kind and what was skipped."""
    onoff, skipped = resolve_onoff(
        hass, coordinator, entry_id, sections.get(SECTION_ONOFF, [])
    )
    custom = {SECTION_ONOFF: onoff}
    for section in (SECTION_CLIMATE, SECTION_WATER_HEATER):
        custom[section], more = resolve_assembled(
            hass, coordinator, entry_id, section, sections.get(section, [])
        )
        skipped += more
    return custom, skipped


async def async_save(
    hass: HomeAssistant, coordinator: Any, custom: dict[str, list[dict[str, Any]]]
) -> None:
    """Write the file, with comments that say what the raw values mean."""
    onoff = []
    for entity in custom.get(SECTION_ONOFF, []):
        item = entity["item"]
        other = off_value(item, entity[KEY_ON])
        onoff.append(
            {
                KEY_VALUE: entity[KEY_VALUE],
                KEY_ON: entity[KEY_ON],
                KEY_DEVICE_CLASS: entity[KEY_DEVICE_CLASS],
                KEY_NAME: entity[KEY_NAME],
                "on_label": item["options"][entity[KEY_ON]],
                "other": (other, item["options"][other]),
            }
        )
    assembled = {
        section: [
            {**entity, "labels": entity.get("options") or {}}
            for entity in custom.get(section, [])
        ]
        for section in (SECTION_CLIMATE, SECTION_WATER_HEATER)
    }
    text = render_file(
        _title(coordinator),
        onoff,
        assembled[SECTION_CLIMATE],
        assembled[SECTION_WATER_HEATER],
        # The saved settings are not this dialog's, so they are written back as they were.
        saved := await async_read_settings(hass, coordinator.stable_id),
        _notes(coordinator, saved[KEY_VALUES]) if saved else None,
    )
    await hass.async_add_executor_job(
        write_text, custom_file_path(hass, coordinator.stable_id), text
    )


def _title(coordinator: Any) -> str:
    return f"{coordinator.profile.device_name} ({coordinator.stable_id})"


async def async_read_settings(
    hass: HomeAssistant, stable_id: str
) -> dict[str, Any] | None:
    """The settings saved in the controller's file; None without a file or a section."""
    path = custom_file_path(hass, stable_id)
    try:
        text = await hass.async_add_executor_job(read_text, path)
        return parse_settings(yaml.safe_load(text)) if text is not None else None
    except (OSError, yaml.YAMLError):
        return None


def _label(item: dict[str, Any], value: Any) -> str | None:
    """The catalog's name for a raw value of an enumerated setting."""
    try:
        return item.get("options", {}).get(int(value))
    except (TypeError, ValueError):
        return None


def _notes(coordinator: Any, values: dict[str, Any]) -> dict[str, str]:
    """What to write next to each saved value: the setting's name, and a raw value's own."""
    notes: dict[str, str] = {}
    for key, (_platform, item) in settings_items(coordinator.profile).items():
        if key in values:
            label = _label(item, values[key])
            notes[key] = item["name"] + (f": {label}" if label else "")
    return notes


def _programme_days(schedules: dict[str, Any]) -> dict[str, dict[str, list[list[Any]]]]:
    """The weekly programmes as the file holds them: [start, end, level] per window."""
    return {
        name: {
            day: [[w["start"], w["end"], w["mode"]] for w in windows]
            for day, windows in days.items()
        }
        for name, days in schedules.items()
        if days
    }


async def _async_current(
    coordinator: Any,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """What the controller holds now: values by datapoint, programmes, what it refused."""
    values, refused = await coordinator.async_read_settings()
    await coordinator.async_refresh_schedules()
    return values, _programme_days(coordinator.schedules), sorted(refused)


async def async_save_settings(hass: HomeAssistant, coordinator: Any) -> dict[str, Any]:
    """Read everything writable and the programmes and put them in the controller's file."""
    values, programmes, not_read = await _async_current(coordinator)
    settings = {
        KEY_CONTROLLER: coordinator.profile.model,
        KEY_SOFTWARE: coordinator.profile.sw_version,
        KEY_SAVED: dt_util.now().isoformat(timespec="seconds"),
        KEY_VALUES: values,
        KEY_PROGRAMMES: programmes,
    }
    section = render_settings(settings, _notes(coordinator, values))
    path = custom_file_path(hass, coordinator.stable_id)
    text = await hass.async_add_executor_job(read_text, path)
    if text is None:
        text = render_file(_title(coordinator), [])
    await hass.async_add_executor_job(write_text, path, replace_settings(text, section))
    return {
        "file": os.path.basename(path),
        "saved": settings[KEY_SAVED],
        "values": len(values),
        "programmes": len(programmes),
        "not_read": not_read,
    }


def programme_key(name: str, day: str) -> str:
    """How one day of a weekly programme is named among the differences."""
    return f"{name}/{day}"


async def async_compare_settings(
    hass: HomeAssistant, coordinator: Any
) -> dict[str, Any] | None:
    """What differs between the saved values and the controller now; None if none are saved.

    Nothing is written. A saved value this controller has no datapoint for, or did not answer
    for, is listed apart and not compared: that is what a file from another controller of the
    family looks like here.
    """
    saved = await async_read_settings(hass, coordinator.stable_id)
    if saved is None:
        return None
    values, programmes, not_read = await _async_current(coordinator)
    items = settings_items(coordinator.profile)
    differences = []
    for key, was, now in changed_settings(saved[KEY_VALUES], values):
        platform, item = items[key]
        difference = {
            "key": key,
            "name": item["name"],
            "saved": was,
            "now": now,
            "setting": restored_by_default(platform, item),
        }
        if (label := _label(item, was)) is not None:
            difference["saved_as"] = label
        if (label := _label(item, now)) is not None:
            difference["now_as"] = label
        differences.append(difference)
    days = [
        {
            "key": programme_key(name, day),
            "programme": name,
            "day": day,
            "saved": windows,
            "now": programmes[name].get(day),
        }
        for name, saved_days in sorted(saved[KEY_PROGRAMMES].items())
        if name in programmes
        for day, windows in saved_days.items()
        if [list(w) for w in windows] != programmes[name].get(day)
    ]
    return {
        "saved": saved[KEY_SAVED],
        "controller": saved[KEY_CONTROLLER],
        "same_controller": (saved[KEY_CONTROLLER], saved[KEY_SOFTWARE])
        == (coordinator.profile.model, coordinator.profile.sw_version),
        "compared": len(set(saved[KEY_VALUES]) & set(values)),
        "differences": differences,
        "programme_days": days,
        "not_compared": sorted(
            (set(saved[KEY_VALUES]) - set(values))
            | (set(saved[KEY_PROGRAMMES]) - set(programmes))
        ),
        "not_read": not_read,
    }


async def async_restore_settings(
    hass: HomeAssistant, coordinator: Any, keys: Iterable[str]
) -> dict[str, Any] | None:
    """Set the named values and programme days back to what was saved; None if none are saved.

    Each one is its own write, checked the way its entity checks it, and one the controller
    or the catalog refuses is reported and the rest carry on. A key is a datapoint's, or a
    programme and a day as compare names them.
    """
    saved = await async_read_settings(hass, coordinator.stable_id)
    if saved is None:
        return None
    written: list[str] = []
    refused: dict[str, str] = {}
    for key in dict.fromkeys(keys):
        name, _, day = key.partition("/")
        try:
            if key in saved[KEY_VALUES]:
                await coordinator.async_write_setting(key, saved[KEY_VALUES][key])
            elif day in saved[KEY_PROGRAMMES].get(name, {}):
                windows = [
                    {"start": start, "end": end, "mode": level}
                    for start, end, level in saved[KEY_PROGRAMMES][name][day]
                ]
                await coordinator.async_set_day_schedule(name, day, windows, force=True)
            else:
                raise ValueError("not among the saved values")
        except Exception as err:  # noqa: BLE001 - one refusal must not stop the rest
            refused[key] = str(err) or type(err).__name__
        else:
            written.append(key)
    await coordinator.async_request_refresh()
    return {"written": written, "refused": refused}


class CustomFileView(HomeAssistantView):
    """Hands out a controller's custom entities file, for the backup link in the options."""

    url = DOWNLOAD_URL
    name = "api:optov:custom"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.StreamResponse:
        hass = request.app["hass"]
        entry = hass.config_entries.async_get_entry(entry_id)
        name = (
            entry.data.get(CONF_CUSTOM_FILE)
            if entry and entry.domain == DOMAIN
            else None
        )
        if not name:
            return web.Response(status=404)
        text = await hass.async_add_executor_job(
            read_text, os.path.join(hass.config.path(DOMAIN), name)
        )
        if text is None:
            return web.Response(status=404)
        return web.Response(
            text=text,
            content_type="text/yaml",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )
