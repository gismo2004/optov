"""Decoding of the controller's fault-history buffer.

Binary buffer layouts, as used by the controller families this integration supports:
1. Heat Pump WO1A / WO1H (Buffer 0xA801 'WPRError', 240 bytes = 30 entries x 8 bytes):
   - Entry Byte 0: Status / index flags
   - Entry Bytes 1..4: 32-bit unsigned little-endian integer -> Unix epoch timestamp
     (seconds since 1970-01-01 00:00:00 UTC)
   - Entry Byte 5: Error code byte in Hex (0x00 = empty slot; non-zero = active/historical fault)
   - Entry Bytes 6..7: Parameters / status flags

2. Heat Pump WO1C (Buffer 0xA801 'WPR3Error', 270 bytes = 30 entries x 9 bytes):
   - Same layout as WO1A, with 9 bytes per entry.

3. Classic Boiler (Buffer 0x7507 'Error', 90 bytes = 10 entries x 9 bytes):
   - Entry Byte 0: Error code byte in Hex (0x00 = empty slot)
   - Entry Bytes 1..8: BCD-encoded timestamp (YYYY MM DD hh mm ss)

4. Burner automat on a boiler ('FehlerHisFA01'..'20', twenty 9-byte datapoints):
   - Same layout as 3, decoded by the same function; the texts are the automat's own,
     keyed by the chip code the boiler reports (catalog table fa_error_codes).
"""

import logging
from datetime import UTC, datetime
from typing import Any

try:
    from .conversions import decode_datetime_bcd
except ImportError:  # imported directly by the tests, without a parent package
    from conversions import decode_datetime_bcd

_LOGGER = logging.getLogger(__name__)


def get_error_description(code_hex: str, error_codes: dict[str, str]) -> str:
    """Return the description for a 2-character hex fault code.

    `error_codes` is the device-specific map from `catalog_db.get_error_codes()`. The catalog
    files fault texts per controller family and that is the ONLY source: the texts genuinely
    differ between families, so a shared or guessed table is actively wrong. On a
    heat pump `FF` is "Neustart der Regelung" (a routine control restart), while the
    boiler-flavoured generic text for the same code is "Interner Fehler oder Reset-Taster
    blockiert" -- which would report dozens of routine restarts as internal faults.

    An unknown code is surfaced as the raw code rather than being invented.
    """
    code_clean = code_hex.upper().strip()
    desc = (error_codes or {}).get(code_clean)
    if desc:
        return desc
    _LOGGER.debug(
        "Fault code %s is not in this device's catalog error_codes", code_clean
    )
    return f"0x{code_clean}"


def decode_wp_error_history(
    raw_bytes: bytes,
    entry_bytes: int = 8,
    error_codes: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Decode a heat-pump error-history buffer (WPRError / WPR3Error @ 0xA801).

    Layout:
    - entry stride 8 or 9 bytes depending on family -- pass via `entry_bytes`
    - byte 0: entry index (echoes the requested RPC index)
    - bytes 1..4: little-endian seconds since 1970-01-01
    - byte 5: hex fault code
    - a code of 0x00 TERMINATES the list; it is not a skippable gap, so everything after the
      first empty slot is unwritten buffer, not more history.
    """
    entries: list[dict[str, Any]] = []
    if len(raw_bytes) < entry_bytes:
        return entries

    total_entries = len(raw_bytes) // entry_bytes
    for idx in range(total_entries):
        offset = idx * entry_bytes
        entry_raw = raw_bytes[offset : offset + entry_bytes]
        if len(entry_raw) < entry_bytes:
            break

        code_val = entry_raw[5]
        if code_val == 0:
            break  # end of written history

        code_hex = f"{code_val:02X}"
        ts_sec = int.from_bytes(entry_raw[1:5], "little", signed=False)

        timestamp_iso: str | None = None
        timestamp_formatted: str = "--"
        if ts_sec > 0:
            try:
                dt = datetime.fromtimestamp(ts_sec, tz=UTC)
                timestamp_iso = dt.isoformat()
                timestamp_formatted = dt.strftime("%d.%m.%Y %H:%M:%S")
            except (ValueError, OverflowError, OSError):
                timestamp_formatted = f"Raw TS: {ts_sec}"

        description = get_error_description(code_hex, error_codes)

        entries.append(
            {
                "index": idx + 1,
                "code": code_hex,
                "description": description,
                "timestamp": timestamp_iso,
                "timestamp_str": timestamp_formatted,
                "status_flag": int(entry_raw[0]),
                "raw_hex": entry_raw.hex(),
            }
        )

    return entries


def decode_boiler_error_history(
    raw_bytes: bytes,
    entry_bytes: int = 9,
    error_codes: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Decode a classic boiler error-history buffer (Error @ 0x7507, 90 bytes = 10 x 9).

    Note this differs from the heat-pump layout in BOTH the code position and the time
    encoding:
    - byte 0: hex fault code -- 0x00 TERMINATES the list (not a skippable gap)
    - bytes 1..8: BCD timestamp, where byte 5 is the weekday and is skipped
      (year = bytes 1+2, month = 3, day = 4, hour = 6, minute = 7, second = 8)
    """
    entries: list[dict[str, Any]] = []
    total_entries = len(raw_bytes) // entry_bytes

    for idx in range(total_entries):
        offset = idx * entry_bytes
        entry_raw = raw_bytes[offset : offset + entry_bytes]
        if len(entry_raw) < entry_bytes:
            break

        code_val = entry_raw[0]
        if code_val == 0:
            break  # end of written history

        code_hex = f"{code_val:02X}"
        dt_str = decode_datetime_bcd(entry_raw[1:9])
        description = get_error_description(code_hex, error_codes)

        entries.append(
            {
                "index": idx + 1,
                "code": code_hex,
                "description": description,
                "timestamp": dt_str,
                "timestamp_str": dt_str,
                "raw_hex": entry_raw.hex(),
            }
        )

    return entries
