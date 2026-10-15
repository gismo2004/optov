"""Custom entities: entities the user builds from the controller's values.

The catalog decides how each datapoint comes in, and for some that cannot be the whole story.
A datapoint whose enumeration names exactly two values -- Aus/Ein, Nein/Ja, inaktiv/aktiv --
comes in as a sensor (read-only) or a select (writable), because nothing in the catalog says
which of the two names means "on". Across controllers that is not even consistent: some pairs
put the active state on 0, some are not on/off at all (Celsius/Fahrenheit, stepped/variable
speed), and a few use raw values other than 0 and 1. So the integration does not convert them;
the user adds an on/off entity for the ones they want and says which state is "on". A reading
gets a binary sensor, a setting a switch.

A thermostat or water heater is assembled from existing entities the same way: a setpoint, an
operating-mode select whose values the user maps to Home Assistant's modes, and optionally a
current temperature from any sensor. The catalog cannot say which value plays which role or which
controller mode means "heat", so the user does.

A custom entity reads the value its source already publishes and writes through the same path,
so it costs no bus time. Choices are stored as raw values, not labels: labels follow the catalog
language, which can change.

All custom entities of a controller live in one YAML file next to the catalog, one section per
kind, so they can be backed up, edited and restored. The options flow edits the same file.

Kept free of Home Assistant imports so the rules can be tested on their own.
"""

from typing import Any

# The sub-device that holds a controller's custom entities, next to its circuits.
CUSTOM_DEVICE = "custom"
# Sections of the file, one per kind of custom entity.
SECTION_ONOFF = "onoff"
SECTION_CLIMATE = "climate"
SECTION_WATER_HEATER = "water_heater"
SECTIONS = (SECTION_ONOFF, SECTION_CLIMATE, SECTION_WATER_HEATER)

# Keys of an on/off entry.
KEY_VALUE = "value"
# Not "on": YAML 1.1 reads an unquoted on: key as the boolean true.
KEY_ON = "on_value"
KEY_DEVICE_CLASS = "device_class"
KEY_NAME = "name"

# What an on/off entity becomes: read-only readings binary sensors, writable settings switches.
ONOFF_PLATFORM = {"sensors": "binary_sensor", "selects": "switch"}

# Offered for a binary sensor; each decides how Home Assistant words the two states.
BINARY_DEVICE_CLASSES = [
    "none",
    "running",
    "power",
    "heat",
    "problem",
    "opening",
    "moisture",
    "connectivity",
]
SWITCH_DEVICE_CLASSES = ["switch", "outlet"]

# Keys of a thermostat or water heater entry.
KEY_TARGET = "target"
KEY_CURRENT = "current"
KEY_MODE = "mode"
KEY_MODES = "modes"
KEY_WRITE = "write"
KEY_HEATING = "heating"
KEY_PRESETS = "presets"

# Home Assistant's thermostat presets. Each can be given a switch or a two-state setting of the
# controller that turns it on; the dialog offers the common ones, the file takes them all.
PRESETS = ["eco", "comfort", "away", "boost", "home", "sleep", "activity"]
PRESETS_OFFERED = ["eco", "comfort", "away", "boost"]

# The modes Home Assistant knows for each kind that a controller mode can be mapped to.
MODES = {
    SECTION_CLIMATE: ["off", "heat", "cool", "heat_cool", "auto"],
    SECTION_WATER_HEATER: [
        "off",
        "eco",
        "electric",
        "gas",
        "heat_pump",
        "high_demand",
        "performance",
    ],
}


def two_state_items(profile: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every sensor and select whose catalog enumeration has exactly two values.

    Returns (platform, item) pairs, platform being "sensors" or "selects".
    """
    found = []
    for platform in ONOFF_PLATFORM:
        for item in getattr(profile, platform, None) or []:
            options = item.get("options")
            if isinstance(options, dict) and len(options) == 2:
                found.append((platform, item))
    return found


def find_item(profile: Any, item_id: str) -> tuple[str, dict[str, Any]] | None:
    """The two-state item with this id, with its platform, or None if the profile lacks it."""
    for platform, item in two_state_items(profile):
        if item["id"] == item_id:
            return platform, item
    return None


def default_on_value(item: dict[str, Any]) -> int:
    """The raw value preselected as "on": 1 where the pair has it, else the higher one.

    Only a preselection; whether it is right depends on the pair, so the user confirms it.
    """
    values = sorted(int(v) for v in item["options"])
    return 1 if 1 in values else values[-1]


def onoff_unique_id(stable_id: str, item_id: str) -> str:
    """An on/off entity's unique id: its source's, marked. One per source."""
    return f"{stable_id}_{item_id}_onoff"


def is_on(label: Any, item: dict[str, Any], on_value: int) -> bool | None:
    """Whether the published label is the state chosen as "on"; None before the first read."""
    if label is None:
        return None
    return label == item["options"].get(int(on_value))


def off_value(item: dict[str, Any], on_value: int) -> int:
    """The other of the two raw values."""
    return next(int(v) for v in item["options"] if int(v) != int(on_value))


def device_classes(platform: str) -> list[str]:
    """The device classes an on/off entity of this source platform can take; first the default."""
    if ONOFF_PLATFORM[platform] == "binary_sensor":
        return BINARY_DEVICE_CLASSES
    return SWITCH_DEVICE_CLASSES


def check_device_class(platform: str, device_class: str | None) -> str:
    """The device class to use: the given one if it fits the entity's kind, else the default."""
    allowed = device_classes(platform)
    return device_class if device_class in allowed else allowed[0]


def onoff_entity(
    platform: str,
    item: dict[str, Any],
    value: str,
    on: int,
    device_class: str | None,
    name: str | None,
) -> dict[str, Any]:
    """An on/off entity of a two-state datapoint, as the platforms and the file take it."""
    return {
        "source": item["id"],
        "platform": platform,
        "item": item,
        KEY_VALUE: value,
        KEY_ON: on,
        KEY_DEVICE_CLASS: check_device_class(platform, device_class),
        KEY_NAME: (name or "").strip() or item["name"],
    }


def parse_file(raw: Any) -> tuple[dict[str, list[dict[str, Any]]], list[str], bool]:
    """Check what was read from the file.

    Returns the usable entries per section, what was wrong, and whether the file was readable
    at all. Unreadable means not a mapping of sections; that is not the same as empty, so the
    caller keeps what it has until the file is fixed. An unknown section is reported, not an
    error: a file from a newer version still loads.
    """
    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in SECTIONS}
    if raw is None:
        return sections, [], True
    if not isinstance(raw, dict):
        return sections, ["the file must have sections such as 'onoff:'"], False
    problems: list[str] = []
    for name, content in raw.items():
        if name not in SECTIONS:
            problems.append(f"unknown section '{name}', ignored")
            continue
        if content is None:
            continue
        if not isinstance(content, list):
            problems.append(f"{name}: must be a list of entries")
            continue
        if name == SECTION_ONOFF:
            entries, found = _parse_onoff(content)
        else:
            entries, found = _parse_assembled(name, content)
        sections[name] = entries
        problems += found
    return sections, problems, True


def _mode_name(value: Any) -> Any:
    """YAML 1.1 reads an unquoted off as false; in a mode position it can only mean "off"."""
    return "off" if value is False else value


def _entity_id(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and "." in value else None


def _parse_assembled(
    section: str, raw: list[Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The entries of a climate or water_heater section.

    `target` is required, and for a thermostat `mode` with `modes` too. `modes` maps a raw value
    of the mode select to one of Home Assistant's modes; `write` says which raw value a mode
    writes when several map to it, otherwise the first one listed is written.
    """
    allowed = MODES[section]
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[str] = set()
    for pos, item in enumerate(raw, start=1):
        where = f"{section} entry {pos}"
        if not isinstance(item, dict):
            problems.append(f"{where}: not a mapping")
            continue
        target = _entity_id(item.get(KEY_TARGET))
        if target is None:
            problems.append(f"{where}: 'target' must be an entity id")
            continue
        where = f"{where} ({target})"
        if target in seen:
            problems.append(f"{where}: listed twice, the first one is used")
            continue
        mode = _entity_id(item.get(KEY_MODE))
        if item.get(KEY_MODE) and mode is None:
            problems.append(f"{where}: 'mode' must be an entity id")
            continue
        if section == SECTION_CLIMATE and mode is None:
            problems.append(f"{where}: a thermostat needs 'mode'")
            continue
        modes: dict[int, str] = {}
        bad = None
        for raw_value, ha_mode in (item.get(KEY_MODES) or {}).items():
            ha_mode = _mode_name(ha_mode)
            if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                bad = f"'modes' keys must be the select's raw values, not {raw_value!r}"
                break
            if ha_mode not in allowed:
                bad = f"mode {ha_mode!r} is not one of {', '.join(allowed)}"
                break
            modes[raw_value] = ha_mode
        if bad is None and mode is not None and not modes:
            bad = "'modes' must map at least one value"
        write: dict[str, int] = {}
        if bad is None:
            for ha_mode, raw_value in (item.get(KEY_WRITE) or {}).items():
                ha_mode = _mode_name(ha_mode)
                if modes.get(raw_value) != ha_mode:
                    bad = f"'write' {ha_mode}: {raw_value} is not mapped to {ha_mode}"
                    break
                write[ha_mode] = raw_value
        if bad is not None:
            problems.append(f"{where}: {bad}")
            continue
        seen.add(target)
        name = item.get(KEY_NAME)
        entry = {
            KEY_TARGET: target,
            KEY_CURRENT: _entity_id(item.get(KEY_CURRENT)),
            KEY_MODE: mode,
            KEY_MODES: modes,
            KEY_WRITE: write_values(modes, write),
            KEY_NAME: str(name).strip() if name else None,
        }
        if section == SECTION_CLIMATE:
            entry[KEY_HEATING] = _entity_id(item.get(KEY_HEATING))
            presets, bad = _parse_presets(item.get(KEY_PRESETS))
            if bad:
                problems.append(f"{where}: {bad}")
                continue
            entry[KEY_PRESETS] = presets
        entries.append(entry)
    return entries, problems


def _parse_presets(raw: Any) -> tuple[dict[str, str], str | None]:
    """`presets:` maps a preset name to the entity that turns it on."""
    if not raw:
        return {}, None
    if not isinstance(raw, dict):
        return {}, "'presets' must map a preset to an entity id"
    presets: dict[str, str] = {}
    for name, entity in raw.items():
        if name not in PRESETS:
            return {}, f"preset {name!r} is not one of {', '.join(PRESETS)}"
        entity_id = _entity_id(entity)
        if entity_id is None or entity_id.split(".")[0] not in ("switch", "select"):
            return {}, f"preset {name}: must be a switch or a select"
        presets[name] = entity_id
    return presets, None


def write_values(modes: dict[int, str], chosen: dict[str, int]) -> dict[str, int]:
    """For every mapped Home Assistant mode, the raw value it writes.

    What the user chose where several values share a mode, otherwise the first one listed.
    """
    result: dict[str, int] = {}
    for raw_value, ha_mode in modes.items():
        result.setdefault(ha_mode, raw_value)
    result.update(chosen)
    return result


def shared_modes(modes: dict[int, str]) -> dict[str, list[int]]:
    """The Home Assistant modes more than one raw value maps to: the ones that need a choice."""
    by_mode: dict[str, list[int]] = {}
    for raw_value, ha_mode in modes.items():
        by_mode.setdefault(ha_mode, []).append(raw_value)
    return {m: values for m, values in by_mode.items() if len(values) > 1}


def is_custom_unique_id(stable_id: str, unique_id: str) -> bool:
    """Whether a unique id belongs to one of this controller's custom entities, of any kind."""
    if not unique_id.startswith(f"{stable_id}_"):
        return False
    rest = unique_id[len(stable_id) + 1 :]
    return rest.endswith("_onoff") or any(
        rest.startswith(f"{section}_")
        for section in (SECTION_CLIMATE, SECTION_WATER_HEATER)
    )


def assembled_unique_id(stable_id: str, section: str, target: str) -> str:
    """A thermostat's or water heater's unique id: one per setpoint it controls."""
    slug = "".join(c if c.isalnum() else "_" for c in target.lower())
    return f"{stable_id}_{section}_{slug}"


def _parse_onoff(raw: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """The entries of the onoff section. Problems name the entry by position, from 1."""
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[str] = set()
    for pos, item in enumerate(raw, start=1):
        where = f"onoff entry {pos}"
        if not isinstance(item, dict):
            problems.append(f"{where}: not a mapping")
            continue
        value = item.get(KEY_VALUE)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{where}: 'value' is missing")
            continue
        value = value.strip()
        on = item.get(KEY_ON)
        if isinstance(on, bool) or not isinstance(on, int):
            problems.append(f"{where} ({value}): 'on_value' must be a whole number")
            continue
        if value in seen:
            problems.append(f"{where} ({value}): listed twice, the first one is used")
            continue
        seen.add(value)
        device_class = item.get(KEY_DEVICE_CLASS)
        name = item.get(KEY_NAME)
        entries.append(
            {
                KEY_VALUE: value,
                KEY_ON: on,
                KEY_DEVICE_CLASS: str(device_class) if device_class else None,
                KEY_NAME: str(name).strip() if name else None,
            }
        )
    return entries, problems


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_file(
    title: str,
    onoff: list[dict[str, Any]],
    climate: list[dict[str, Any]] | None = None,
    water_heater: list[dict[str, Any]] | None = None,
) -> str:
    """The file's text: a header that explains the format, then each section.

    An on/off entry may carry `on_label` and `other` (the other raw value and its label) for
    the comment that says what the numbers mean.
    """
    lines = [
        f"# OptoV custom entities for {title}.",
        "# Edit, then OptoV options -> Custom entities -> Backup and restore -> Submit.",
        "#",
        "# onoff: an on/off entity for a value with two named states",
        "#   value:        entity id of the value (a sensor or a select)",
        "#   on_value:     the raw value that counts as on",
        "#   device_class: " + ", ".join(BINARY_DEVICE_CLASSES) + " (readings)",
        "#                 " + ", ".join(SWITCH_DEVICE_CLASSES) + " (settings)",
        "#   name:         name of the new entity (optional)",
        "#",
        "# climate: a thermostat, water_heater: a water heater, assembled from entities",
        "#   name:    name of the new entity",
        "#   target:  the setpoint (a number entity)",
        "#   current: the current temperature, any temperature sensor (optional)",
        "#   heating: shows 'heating' while this binary sensor is on (climate, optional)",
        "#   mode:    the operating-mode select (required for climate)",
        "#   modes:   select raw value -> mode; climate: "
        + ", ".join(MODES[SECTION_CLIMATE]),
        "#            water_heater: " + ", ".join(MODES[SECTION_WATER_HEATER]),
        "#   write:   which raw value a mode writes when several map to it",
        "#   presets: preset -> the switch or two-state select that turns it on (climate,",
        "#            optional; a select's on value is ON/1): " + ", ".join(PRESETS),
        "",
        f"{SECTION_ONOFF}:" + ("" if onoff else " []"),
    ]
    for entry in onoff:
        comment = ""
        if entry.get("on_label") is not None:
            comment = f"  # {entry['on_label']}"
            if entry.get("other") is not None:
                other_value, other_label = entry["other"]
                comment += f" (other: {other_value} = {other_label})"
        lines.append(f"  - {KEY_VALUE}: {entry[KEY_VALUE]}")
        lines.append(f"    {KEY_ON}: {entry[KEY_ON]}{comment}")
        if entry.get(KEY_DEVICE_CLASS):
            lines.append(f"    {KEY_DEVICE_CLASS}: {entry[KEY_DEVICE_CLASS]}")
        if entry.get(KEY_NAME):
            lines.append(f"    {KEY_NAME}: {_quote(entry[KEY_NAME])}")
    for section, entries in (
        (SECTION_CLIMATE, climate or []),
        (SECTION_WATER_HEATER, water_heater or []),
    ):
        lines.append("")
        lines.append(f"{section}:" + ("" if entries else " []"))
        for entry in entries:
            lines.extend(_render_assembled(section, entry))
    return "\n".join(lines) + "\n"


def _render_assembled(section: str, entry: dict[str, Any]) -> list[str]:
    labels = entry.get("labels") or {}
    lines = [f"  - {KEY_NAME}: {_quote(entry.get(KEY_NAME) or '')}"]
    lines.append(f"    {KEY_TARGET}: {entry[KEY_TARGET]}")
    if entry.get(KEY_CURRENT):
        lines.append(f"    {KEY_CURRENT}: {entry[KEY_CURRENT]}")
    if section == SECTION_CLIMATE and entry.get(KEY_HEATING):
        lines.append(f"    {KEY_HEATING}: {entry[KEY_HEATING]}")
    if section == SECTION_CLIMATE and entry.get(KEY_PRESETS):
        lines.append(f"    {KEY_PRESETS}:")
        for name in PRESETS:
            if name in entry[KEY_PRESETS]:
                lines.append(f"      {name}: {entry[KEY_PRESETS][name]}")
    if entry.get(KEY_MODE):
        lines.append(f"    {KEY_MODE}: {entry[KEY_MODE]}")
        lines.append(f"    {KEY_MODES}:")
        for raw_value, ha_mode in sorted(entry[KEY_MODES].items()):
            note = f"  # {labels[raw_value]}" if raw_value in labels else ""
            lines.append(f'      {raw_value}: "{ha_mode}"{note}')
        shared = shared_modes(entry[KEY_MODES])
        if shared:
            lines.append(f"    {KEY_WRITE}:")
            for ha_mode in shared:
                lines.append(f'      "{ha_mode}": {entry[KEY_WRITE][ha_mode]}')
    return lines
