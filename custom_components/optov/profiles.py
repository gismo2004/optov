"""Data models and value helpers for OptoV integration."""

from typing import Any

from homeassistant.const import EntityCategory


def parse_address(addr: Any) -> int:
    """Parse address from hex string or int."""
    if isinstance(addr, int):
        return addr
    s = str(addr).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def stable_object_id(model: str, circuit_label: str | None, name: str) -> str:
    """The entity id an entity should get, independent of what the device is called.

    Home Assistant composes an entity id from the area, the device name and the entity name at
    the moment the entity is first created, and never revisits it. That makes the ids an
    accident of history: they were composed while the device was still named after its model
    code, so they read "v200wo1a_...", while a fresh install would now use the full product
    name from the catalog and produce something far longer. Renaming the device, moving it or
    improving the product name in the catalog would all shift them again, and long-term
    statistics are keyed by entity id, so every shift strands three years of history.

    The model code is the stable thing the catalog gives us: `V200WO1A` identifies the
    controller variant and does not change when its marketing name is tidied up. Composing the
    id from that, the circuit, and the datapoint keeps a reinstall landing on exactly the same
    ids -- which is the point, because that is what makes history survive one.

    Existing entities are unaffected: the id is only a suggestion, and Home Assistant uses it
    solely when it creates an entity. Whatever is already in the registry keeps its id.

    Two identical controllers therefore suggest identical ids. That does not collide: Home
    Assistant appends a numeric suffix, so the second one reads `..._2`. Which of the two gets
    the suffix depends on the order they were added, so with duplicate models it is worth
    renaming one of them yourself if the ids need to mean something.
    """
    parts = [model, circuit_label or "", name]
    return " ".join(p for p in parts if p)


def map_category(cat: str | None) -> EntityCategory | None:
    """Map string category to Home Assistant EntityCategory."""
    if not cat:
        return None
    c = str(cat).lower().strip()
    if c == "diagnostic":
        return EntityCategory.DIAGNOSTIC
    if c == "config":
        return EntityCategory.CONFIG
    return None


class DeviceProfile:
    """The entity set generated for one controller, as the platforms consume it."""

    PLATFORMS = ("sensors", "binary_sensors", "numbers", "selects", "switches")

    def __init__(
        self,
        sys_id: int,
        data: dict[str, Any],
        sw_version: str = "",
        model_name: str | None = None,
    ):
        self.sys_id = sys_id
        self.sw_version = sw_version
        # Both fall back to the system id: it is the only thing known about a controller
        # the catalog has no entry for, and inventing a product line for it would put a wrong
        # name on the device and into every entity id derived from it.
        self.device_name: str = data.get(
            "device_name", model_name or f"Controller 0x{sys_id:04X}"
        )
        self.model: str = model_name or data.get("model", f"0x{sys_id:04X}")
        self.circuits: dict[str, str] = data.get("circuits", {})
        self.groups: list[dict[str, Any]] = data.get("groups", [])
        # How this controller expresses its daylight-saving changeovers, if it has the
        # settings at all. Roles come from the catalog's value ranges -- see catalog_db.
        self.clock_dst: dict[str, Any] = data.get("clock_dst") or {}

        # Programmes are keyed per datapoint; a programme tied to a circuit this unit does
        # not have (already filtered by the catalog layer) would carry a circuit key that is
        # not in `circuits`, so that is the one thing checked here.
        raw_schedules = data.get("schedules", {})
        self.schedules: dict[str, Any] = {
            key: cfg
            for key, cfg in raw_schedules.items()
            if not cfg.get("circuit")
            or not self.circuits
            or cfg["circuit"] in self.circuits
        }

        self.sensors: list[dict[str, Any]] = []
        self.binary_sensors: list[dict[str, Any]] = []
        self.numbers: list[dict[str, Any]] = []
        self.selects: list[dict[str, Any]] = []
        self.switches: list[dict[str, Any]] = []
        for platform in self.PLATFORMS:
            items = getattr(self, platform)
            for raw in data.get(platform, []):
                item = dict(raw)
                item["address"] = parse_address(item["address"])
                item["entity_category"] = map_category(item.get("entity_category"))
                if isinstance(item.get("options"), dict):
                    item["options"] = {int(k): v for k, v in item["options"].items()}
                items.append(item)
