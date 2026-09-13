"""Decoders and encoders for the datapoints whose bytes are a structure, not a value.

- Weekly programmes in the three layouts the controllers use
- Time53, the 5-bit hour + 3-bit ten-minute packing the programmes are built from
- BCD timestamps (controller clock, fault history)
"""

from datetime import datetime
from typing import Any

# Days are keyed by language-neutral tokens, Monday first, matching the controller's own day
# order in the weekly programme. Display names are the frontend's job (it knows the user's
# locale); the tokens are what services accept and what the schedule attribute is keyed by.
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_INDEX = {day: index for index, day in enumerate(DAYS)}

MINUTES_PER_DAY = 24 * 60

# The catalog classifies every weekly programme with a mapping type (datapoint_defs
# .mapping_type). This table says what each type means: the wire layout, the levels a window may
# carry, which level means "nothing scheduled", how many windows a day holds, the time grid, and
# a colour per level. All of it is protocol, established against real controllers.
#
# What a level is *called* is not here and cannot be: it depends on the programme, and the names
# are catalog text. Each programme datapoint carries the text key to look them up under, so this
# table is keyed by number and names nothing.
#
#   phase2   7 days x 4 windows x (start, end); a window is simply "on"
#   phase3   7 days x 8 windows x (start, end, level)
#   bitmap   7 days x 24 bytes, four 2-bit levels per byte, one per quarter hour
#
# `colors` runs parallel to `levels`; the names are CSS colours.
SCHEDULE_TYPES: dict[int, dict[str, Any]] = {
    1: {
        "format": "phase2",
        "levels": [0, 1],
        "colors": ["gray", "darkred"],
        "default": 0,
        "windows": 4,
        "step": 10,
    },
    9: {
        "format": "phase2",
        "levels": [0, 1],
        "colors": ["gray", "darkred"],
        "default": 0,
        "windows": 4,
        "step": 10,
    },
    2: {
        "format": "bitmap",
        "levels": [0, 1, 2, 3],
        "colors": ["gray", "darkblue", "darkred", "darkorange"],
        "default": 0,
        "windows": 8,
        "step": 15,
    },
    5: {
        "format": "phase3",
        "levels": [0, 1, 2, 3],
        "colors": ["gray", "darkblue", "darkred", "darkorange"],
        "default": 0,
        "windows": 8,
        "step": 10,
    },
    6: {
        "format": "phase3",
        "levels": [0, 1, 2, 3],
        "colors": ["gray", "darkblue", "darkred", "darkorange"],
        "default": 0,
        "windows": 8,
        "step": 10,
    },
    7: {
        "format": "phase3",
        "levels": [0, 1, 2, 3],
        "colors": ["gray", "darkblue", "darkred", "darkorange"],
        "default": 0,
        "windows": 8,
        "step": 10,
    },
    8: {
        "format": "phase3",
        "levels": [0, 1, 2, 3],
        "colors": ["gray", "darkblue", "darkred", "darkorange"],
        "default": 0,
        "windows": 8,
        "step": 10,
    },
    10: {
        "format": "phase3",
        "levels": [2, 3, 4],
        "colors": ["darkblue", "darkred", "darkorange"],
        "default": 3,
        "windows": 8,
        "step": 10,
    },
}


def schedule_type(mapping_type: int | None) -> dict[str, Any] | None:
    """The programme definition for a catalog mapping_type, or None if it is not a programme."""
    return SCHEDULE_TYPES.get(int(mapping_type or 0))


def parse_level(value: Any, levels: list[int], default: int) -> int:
    """Coerce a level from a service call to one the programme allows.

    Levels are numbers, not words: what a level is called depends on the programme (level 1 is
    "Reduziert" on a heating circuit and "Niveau Oben" on hot water) and those names live in
    the catalog. A caller reads them from the sensor's `modes` attribute.
    """
    try:
        level = int(value)
    except (TypeError, ValueError):
        return default
    return level if level in levels else default


def decode_time53(b: int) -> str:
    """Decode 1 byte into HH:MM using the 5+3 encoding.

        if byte != 0xFF:  hh = byte >> 3,  mm = (byte & 7) * 10
        else:             ""   (empty slot)

    0xFF marks an unset slot and must not be decoded -- the arithmetic would
    otherwise yield "31:70". 0xC0 needs no special case: 0xC0 >> 3 is 24 and 0xC0 & 7 is 0,
    so the formula already produces "24:00".
    """
    if b == 0xFF:
        return ""
    hour = (b >> 3) & 0x1F
    minute = (b & 0x07) * 10
    return f"{hour:02d}:{minute:02d}"


def encode_time53(t_str: str | None) -> int:
    """Encode HH:MM string into 1 byte using 5+3 bit encoding."""
    if not t_str:
        return 0x00
    t_clean = str(t_str).strip()
    if t_clean in ("24:00", "24:0"):
        return 0xC0
    try:
        parts = t_clean.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        step = min(5, round(minute / 10.0))
        if hour >= 24:
            return 0xC0
        return ((hour & 0x1F) << 3) | (step & 0x07)
    except Exception:
        return 0x00


def _to_minutes(t_str: str | None) -> int:
    if not t_str:
        return 0
    parts = str(t_str).strip().split(":")
    try:
        return int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
    except ValueError:
        return 0


def _from_minutes(total: int) -> str:
    total = max(0, min(MINUTES_PER_DAY, total))
    return f"{total // 60:02d}:{total % 60:02d}"


def decode_day_schedule(
    raw: bytes, fmt: str = "phase3", default_level: int = 0
) -> list[dict[str, Any]]:
    """Decode one day of a programme into windows of {window, start, end, mode}.

    `mode` is the level number; the level's name comes from the catalog and is not this
    layer's business. Empty slots (all 0x00 or all 0xFF, depending on the controller) are
    skipped rather than decoded into nonsense times.
    """
    windows: list[dict[str, Any]] = []
    if fmt == "phase2":
        for i in range(min(4, len(raw) // 2)):
            b1, b2 = raw[i * 2], raw[i * 2 + 1]
            if (b1 == 0 and b2 == 0) or (b1 == 0xFF and b2 == 0xFF):
                continue
            start, end = decode_time53(b1), decode_time53(b2)
            if start and end:
                windows.append({"window": i + 1, "start": start, "end": end, "mode": 1})
        return windows

    if fmt == "bitmap":
        # Four 2-bit levels per byte, most significant pair first, one per quarter hour.
        level_at = []
        for b in raw[:24]:
            level_at.extend([(b >> 6) & 3, (b >> 4) & 3, (b >> 2) & 3, b & 3])
        run_start, run_level, index = 0, None, 0
        for slot, level in enumerate([*level_at, None]):
            if level != run_level:
                if run_level is not None and run_level != default_level:
                    index += 1
                    windows.append(
                        {
                            "window": index,
                            "start": _from_minutes(run_start * 15),
                            "end": _from_minutes(slot * 15),
                            "mode": run_level,
                        }
                    )
                run_start, run_level = slot, level
        return windows

    for i in range(min(8, len(raw) // 3)):
        b1, b2, level = raw[i * 3], raw[i * 3 + 1], raw[i * 3 + 2]
        if (b1 == 0 and b2 == 0) or (b1 == 0xFF and b2 == 0xFF):
            continue
        start, end = decode_time53(b1), decode_time53(b2)
        if start and end:
            windows.append({"window": i + 1, "start": start, "end": end, "mode": level})
    return windows


def encode_day_schedule(
    windows: list[dict[str, Any]],
    fmt: str = "phase3",
    default_level: int = 0,
    levels: list[int] | None = None,
) -> bytes:
    """Encode one day's windows into the controller's layout.

    Unused slots are filled the way the controller expects them: 0xFF pairs for the two-byte
    layout, zero triplets for the three-byte one (the ventilation programme, whose default
    level is not zero, uses FF FF 00), and the default level for every quarter hour of the
    bitmap that no window covers. Windows sitting at the default level are not switching
    windows at all and are left out.
    """
    allowed = levels or [0, 1, 2, 3]
    ordered = sorted(windows, key=lambda w: _to_minutes(w.get("start")))

    if fmt == "phase2":
        buf = bytearray([0xFF] * 8)
        for i, w in enumerate(ordered[:4]):
            buf[i * 2] = encode_time53(w.get("start"))
            buf[i * 2 + 1] = encode_time53(w.get("end"))
        return bytes(buf)

    if fmt == "bitmap":
        packed = default_level & 3
        packed |= packed << 2 | packed << 4 | packed << 6
        slots = [default_level] * 96
        for w in ordered:
            level = parse_level(w.get("mode"), allowed, default_level)
            first = _to_minutes(w.get("start")) // 15
            last = min(96, (_to_minutes(w.get("end")) + 14) // 15)
            for s in range(first, last):
                slots[s] = level
        out = bytearray(24)
        for i in range(24):
            a, b, c, d = slots[i * 4 : i * 4 + 4]
            out[i] = (a << 6) | (b << 4) | (c << 2) | d
        return bytes(out)

    empty = bytes([0xFF, 0xFF, 0x00]) if default_level != 0 else bytes(3)
    buf = bytearray(empty * 8)
    active = [
        w
        for w in ordered
        if parse_level(w.get("mode"), allowed, default_level) != default_level
    ]
    for i, w in enumerate(active[:8]):
        buf[i * 3] = encode_time53(w.get("start"))
        buf[i * 3 + 1] = encode_time53(w.get("end"))
        buf[i * 3 + 2] = parse_level(w.get("mode"), allowed, default_level)
    return bytes(buf)


def encode_datetime_bcd(when: "datetime") -> bytes:
    """Build the 8-byte timestamp the controller expects.

    Every field is packed as binary-coded decimal -- 2026 becomes 0x20 0x26, not 0x07 0xEA --
    except the weekday in byte 4, which is a plain number counting Sunday as 0 through
    Saturday as 6. The controller ignores that byte when reporting the time back, but it is
    part of the write, so it is filled in the form the controller is known to accept.
    """

    def bcd(value: int) -> int:
        return ((value // 10) << 4) | (value % 10)

    return bytes(
        (
            bcd(when.year // 100),
            bcd(when.year % 100),
            bcd(when.month),
            bcd(when.day),
            (when.weekday() + 1) % 7,
            bcd(when.hour),
            bcd(when.minute),
            bcd(when.second),
        )
    )


def decode_datetime_bcd(raw_bytes: bytes) -> str | None:
    """
    Decode 8-byte BCD timestamp:
    Byte 0..1: Year (e.g. 0x20 0x26 -> 2026)
    Byte 2: Month (0x09 -> 9)
    Byte 3: Day (0x09 -> 9)
    Byte 4: Day of week (0x03)
    Byte 5: Hour (0x14)
    Byte 6: Minute (0x15)
    Byte 7: Second (0x00)
    """
    if len(raw_bytes) < 8:
        return None
    try:

        def bcd(val):
            return ((val >> 4) * 10) + (val & 0x0F)

        year = (bcd(raw_bytes[0]) * 100) + bcd(raw_bytes[1])
        month = bcd(raw_bytes[2])
        day = bcd(raw_bytes[3])
        hour = bcd(raw_bytes[5])
        minute = bcd(raw_bytes[6])
        second = bcd(raw_bytes[7])
        return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:{second:02d}"
    except Exception:
        return raw_bytes.hex(" ")
