"""Catalog-driven decoding of raw Optolink datapoint values.

Single source of truth for turning the raw bytes returned by `OptolinkClient.read_raw()`
into a usable value, driven by the catalog's own `conversion` and `parameter_type` columns
rather than by a hand-written `div_ratio`.

Deliberately free of Home Assistant imports so the same logic can be exercised standalone
against real hardware.
"""

from datetime import date, datetime
from typing import Any

try:
    # Real package context (Home Assistant importing custom_components.optov.decode).
    from .conversions import (
        decode_datetime_bcd,
        decode_time53,
        encode_datetime_bcd,
    )
except ImportError:
    # Standalone context: a script adds the integration directory to sys.path and imports
    # this module directly, with no parent package.
    from conversions import decode_datetime_bcd, decode_time53, encode_datetime_bcd

# Integer ParameterType values as name -> (width_in_bits, signed, high_byte_first).
#
# The width is the one the ParameterType *declares*, independently of how many bytes the
# datapoint actually occupies, and that distinction is load-bearing: a datapoint can be `SInt`
# (16-bit signed) while occupying a single byte, and widening a one-byte value to 16 bits
# leaves it positive, whereas signing it at its own width flips everything >= 128 negative.
# Signing by byte length instead of declared width is therefore wrong.
_PARAM_TYPES = {
    "byte": (8, False, False),
    "sbyte": (8, True, False),
    "int": (16, False, False),
    "sint": (16, True, False),
    "int4": (32, False, False),
    "sint4": (32, True, False),
    "inthighbytefirst": (16, False, True),
    "sinthighbytefirst": (16, True, True),
    "int4highbytefirst": (32, False, True),
    "sint4highbytefirst": (32, True, True),
}

# Conversions that are a plain divisor. Everything else needs real logic.
DIVISORS = {
    "div2": 2.0,
    "div10": 10.0,
    "div100": 100.0,
    "div1000": 1000.0,
    "sec2minute": 60.0,
    "sec2hour": 3600.0,
    "sec2day": 86400.0,
    "sec2week": 604800.0,
}

# Rounding per conversion. The Sec2* family rounds to 2 decimals; the Div* family needs no
# rounding, since an integer divided by a power of ten is already exact to that many places --
# it is applied only to suppress floating-point noise.
_PRECISION = {
    "div2": 1,
    "div10": 1,
    "div100": 2,
    "div1000": 3,
    "sec2minute": 2,
    "sec2hour": 2,
    "sec2day": 2,
    "sec2week": 2,
}

# Plain multiplication, no rounding.
_MULTIPLIERS = {"mult2": 2, "mult5": 5, "mult10": 10, "mult100": 100}

# Named in the catalog but no transformation of the value: the integer is the reading (hours
# since, months since the last service).
_PLAIN = {"", "noconversion", "lastburnercheck", "lastcheckinterval"}

# Everything decode_value() turns into something other than the plain integer.
_DECODED = {
    "datetimebcd",
    "datetime_bcd",
    "datebcd",
    "rotatebytes",
    "hexbyte2decimalbyte",
    "time53",
    "ipaddress",
    "convert4bytestofloat",
    "hexbyte2asciibyte",
    "hexbyte2utf16byte",
    "daytodate",
    "multoffset",
}


# Conversions that hold a calendar date and nothing else, so a date can be written back into
# them: DayToDate counts days since 1970-01-01, DateBCD is the eight-byte BCD timestamp with
# the time left at midnight. DateTimeBCD carries a time of day as well and is not one of them.
_DATES = {"daytodate", "datebcd"}

_EPOCH = date(1970, 1, 1)


class DecodeError(ValueError):
    """Raised when raw bytes cannot be decoded as the catalog describes."""


def is_signed(parameter_type: str | None) -> bool:
    """Whether a catalog ParameterType denotes a signed integer."""
    spec = _PARAM_TYPES.get((parameter_type or "").strip().lower())
    return bool(spec and spec[1])


def byte_order(parameter_type: str | None) -> str:
    """The order a value of this parameter type is stored in: "big" for *HighByteFirst."""
    spec = _PARAM_TYPES.get((parameter_type or "").strip().lower())
    return "big" if spec and spec[2] else "little"


def raw_bounds(parameter_type: str | None) -> tuple[int, int]:
    """The range the declared parameter type can hold, as raw integers.

    For a setting the catalog states no limits for: what the wire allows is a better answer
    than a guessed one. The controller refuses whatever it does not want, and says so.
    """
    spec = _PARAM_TYPES.get((parameter_type or "").strip().lower())
    width, signed = (spec[0], spec[1]) if spec else (16, False)
    if signed:
        return -(1 << (width - 1)), (1 << (width - 1)) - 1
    return 0, (1 << width) - 1


def is_supported(conversion: str | None) -> bool:
    """Whether decode_value() can turn this catalog conversion into a value at all.

    A datapoint with any other conversion would fail on every read, so it is not made an entity.
    """
    conv = (conversion or "").strip().lower()
    return (
        conv in _PLAIN or conv in _DECODED or conv in DIVISORS or conv in _MULTIPLIERS
    )


def is_encodable(conversion: str | None) -> bool:
    """Whether a value in this conversion's unit can be turned back into the raw integer.

    Only then can a setting be offered as a number: the plain integer, a divisor, a multiplier
    and MultOffset. Anything else -- dates, addresses, byte strings -- stays a reading.
    """
    conv = (conversion or "").strip().lower()
    return (
        conv in _PLAIN
        or conv in DIVISORS
        or conv in _MULTIPLIERS
        or conv == "multoffset"
    )


def scale(
    raw: int,
    conversion: str | None,
    factor: float | None = None,
    offset: float | None = None,
) -> float:
    """A raw integer in the unit its conversion gives it; see is_encodable()."""
    conv = (conversion or "").strip().lower()
    if conv in DIVISORS:
        return round(raw / DIVISORS[conv], 6)
    if conv in _MULTIPLIERS:
        return raw * _MULTIPLIERS[conv]
    if conv == "multoffset":
        return round(
            raw * (factor if factor is not None else 1.0)
            + (offset if offset is not None else 0.0),
            6,
        )
    return raw


def unscale(
    value: float,
    conversion: str | None,
    factor: float | None = None,
    offset: float | None = None,
) -> int:
    """The raw integer that scale() turns into `value`, rounded to the controller's grid."""
    conv = (conversion or "").strip().lower()
    if conv in DIVISORS:
        return round(value * DIVISORS[conv])
    if conv in _MULTIPLIERS:
        return round(value / _MULTIPLIERS[conv])
    if conv == "multoffset":
        return round(
            (value - (offset if offset is not None else 0.0))
            / (factor if factor else 1.0)
        )
    if conv in _PLAIN:
        return round(value)
    raise DecodeError(f"conversion {conversion!r} cannot be written as a number")


def decodes_to_number(conversion: str | None) -> bool:
    """Whether decode_value() turns this conversion into a number rather than text or a date.

    The integer path -- no conversion, a divisor, a multiplier, MultOffset -- and the four-byte
    float are numbers. The BCD timestamps, dates, byte dumps, addresses and strings are not, and
    neither is a conversion this module does not implement, which never yields a value at all.
    """
    conv = (conversion or "").strip().lower()
    return (
        conv in _PLAIN
        or conv in ("multoffset", "convert4bytestofloat")
        or conv in DIVISORS
        or conv in _MULTIPLIERS
    )


def decodes_to_integer(conversion: str | None) -> bool:
    """Whether the number decode_value() produces for this conversion is always whole."""
    conv = (conversion or "").strip().lower()
    return conv in _PLAIN or conv in _MULTIPLIERS


def decode_int(raw: bytes, parameter_type: str | None) -> int:
    """Turn raw field bytes into an integer.

    The rules, in order:

    1. `*HighByteFirst` parameter types reverse the byte array first.
    2. The value is assembled from **at most four bytes**, little-endian, unsigned.
    3. Only the *signed* parameter types then cast, to the width the ParameterType declares --
       `(sbyte)`, `(short)`, `(int)`. `Byte`, `Int`, `Int4` and the unsigned HighByteFirst
       variants fall straight through to the conversion with no cast at all, so the assembled
       value is used as-is even when it is wider than the type name suggests.

    Both halves of that matter. Signing at the declared width rather than the datapoint's own
    byte length keeps the one-byte `SInt` datapoints positive, as the catalog intends. And
    *not* masking unsigned types keeps the two-byte `Byte` datapoints (0xA303, 0xA403 and
    friends -- LON network variables) at their real 16-bit value: masking 0x07D0 to one byte
    would turn a 20.0 degC flow setpoint into 2.08.

    An unknown or non-integer ParameterType falls back to plain unsigned little-endian.
    """
    spec = _PARAM_TYPES.get((parameter_type or "").strip().lower())
    if spec is None:
        return int.from_bytes(raw[:4], "little", signed=False)

    width, signed, high_byte_first = spec
    data = raw[:4]
    if high_byte_first:
        data = data[::-1]
    value = int.from_bytes(data, "little", signed=False)

    if signed:
        value &= (1 << width) - 1
        if value >= 1 << (width - 1):
            value -= 1 << width
    return value


def extract_bitfield(raw: bytes, bit_start: int, bit_length: int) -> int:
    """Extract a bit-field from the raw block.

    **Bit numbering is MSB-first within each byte, bytes in order** -- not little-endian. The
    block is flattened into a bit sequence that starts at the most significant bit of byte 0;
    `BitLength` bits are taken from `BitPosition` and read most-significant-first.

    This was previously implemented as a little-endian shift-and-mask, which silently produced
    the wrong nibble. Measured against hardware across the sensor-status fields (all of which
    sit at `BitStartPos=20, BitLength=4` over a 3-byte block, so the two schemes select
    opposite nibbles of byte 2): MSB-first reports a realistic mix of "ok", "open circuit"
    and "not present" matching the hardware actually installed, while the little-endian
    reading reported "ok" for every sensor -- including ones that do not exist and whose
    values are plainly bogus.

    `raw` must be the whole block (`BlockLength` bytes), not the sliced field.
    """
    if bit_length <= 0:
        raise DecodeError("bit_length must be positive")
    if bit_start + bit_length > len(raw) * 8:
        raise DecodeError(
            f"bit-field {bit_start}+{bit_length} does not fit in {len(raw)} bytes"
        )

    value = 0
    for k in range(bit_start, bit_start + bit_length):
        bit = (raw[k // 8] >> (7 - (k % 8))) & 1
        value = (value << 1) | bit
    return value


def insert_bitfield(raw: bytes, bit_start: int, bit_length: int, value: int) -> bytes:
    """Place a bit-field back into its block. Inverse of extract_bitfield().

    Uses the same MSB-first numbering, which is what makes a partial write safe: the bits
    around the field keep their current contents. Several settings share one byte -- operating
    mode, party and eco all live in the same register -- so writing a bare value to that
    address would put the field in the wrong bits and clear its neighbours.
    """
    if bit_length <= 0:
        raise DecodeError("bit_length must be positive")
    if bit_start + bit_length > len(raw) * 8:
        raise DecodeError(
            f"bit-field {bit_start}+{bit_length} does not fit in {len(raw)} bytes"
        )

    out = bytearray(raw)
    for k in range(bit_length):
        bit = (value >> (bit_length - 1 - k)) & 1
        index = bit_start + k
        mask = 1 << (7 - (index % 8))
        if bit:
            out[index // 8] |= mask
        else:
            out[index // 8] &= 0xFF ^ mask
    return bytes(out)


def decode_value(
    raw: bytes,
    conversion: str | None = None,
    *,
    parameter_type: str | None = None,
    bit_start: int = 0,
    bit_length: int = 0,
    enum: dict[Any, str] | None = None,
    factor: float | None = None,
    offset: float | None = None,
) -> Any:
    """Decode raw datapoint bytes into a Python value.

    `conversion` is the catalog's own Conversion name (case-insensitive). `enum`, when given,
    maps integer values to display strings and is applied after numeric decoding.
    `factor`/`offset` are the catalog's ConversionFactor/ConversionOffset, needed only by
    the MultOffset conversion.
    """
    if not raw:
        raise DecodeError("empty payload")

    conv = (conversion or "NoConversion").strip().lower()

    # Bit-fields are always unsigned integers carved out of the telegram.
    if bit_length:
        value: Any = extract_bitfield(raw, bit_start, bit_length)
        return _apply_enum(value, enum)

    # DateBCD is identical to DateTimeBCD -- both read the same 8 fields including
    # hour/minute/second -- so merging them is deliberate, not an oversight.
    if conv in ("datetimebcd", "datetime_bcd", "datebcd"):
        return decode_datetime_bcd(raw)

    if conv == "rotatebytes":
        # RotateBytes: reverses the byte array, ByteArray types only.
        return raw[::-1].hex(" ")

    if conv == "hexbyte2decimalbyte":
        # The array passes straight through; the conversion name means "render each byte as
        # its decimal value".
        return " ".join(str(b) for b in raw)

    if conv == "time53":
        return decode_time53(raw[0])

    if conv == "ipaddress":
        # IPAddress: printed in reverse byte order.
        if len(raw) < 4:
            raise DecodeError("IPAddress needs 4 bytes")
        return f"{raw[3]}.{raw[2]}.{raw[1]}.{raw[0]}"

    if conv == "convert4bytestofloat":
        import struct

        if len(raw) < 4:
            raise DecodeError("Convert4BytesToFloat needs 4 bytes")
        return round(struct.unpack("<f", raw[:4])[0], 3)

    if conv in ("hexbyte2asciibyte", "hexbyte2utf16byte"):
        if conv == "hexbyte2asciibyte":
            # Unprogrammed EEPROM / Flash memory is filled with 0xFF.
            # Truncate at first NUL (0x00) or unwritten byte (0xFF).
            clean_bytes = bytearray()
            for b in raw:
                if b in (0x00, 0xFF):
                    break
                if 0x20 <= b <= 0x7E:
                    clean_bytes.append(b)
                else:
                    break
            decoded = clean_bytes.decode("ascii").strip()
        else:
            # hexbyte2utf16byte: 2-byte little-endian characters.
            # Truncate at first NUL (b'\x00\x00') or unwritten EEPROM word (b'\xff\xff').
            chars = []
            for i in range(0, len(raw) - 1, 2):
                chunk = raw[i : i + 2]
                if chunk in (b"\x00\x00", b"\xff\xff"):
                    break
                try:
                    ch = chunk.decode("utf-16-le")
                    # Stop on noncharacter, replacement, or control characters
                    if ch in ("\uffff", "\ufffe", "\ufffd") or (
                        ord(ch) < 32 and ch not in "\t\n\r"
                    ):
                        break
                    chars.append(ch)
                except Exception:
                    break
            decoded = "".join(chars).strip()
        return decoded if decoded else None

    if conv == "daytodate":
        # DayToDate: zero-pad to 8 bytes, read as a 64-bit integer, add days to
        # 1970-01-01. The result is a date, not a scaled number.
        from datetime import date, timedelta

        days = int.from_bytes(raw[:8], "little", signed=False)
        return (date(1970, 1, 1) + timedelta(days=days)).isoformat()

    if len(raw) > 4 and conv in _PLAIN:
        # Wider than any integer type: an identifier, shown as its bytes.
        return raw.hex(" ").upper()

    value: Any = decode_int(raw, parameter_type)

    if conv in DIVISORS:
        value = round(value / DIVISORS[conv], _PRECISION.get(conv, 2))
    elif conv in _MULTIPLIERS:
        value = value * _MULTIPLIERS[conv]
    elif conv == "multoffset":
        # MultOffset: value * ConversionFactor + ConversionOffset. Both
        # factors come from the catalog; without them the result would be a silent lie.
        if factor is None and offset is None:
            raise DecodeError(
                "MultOffset needs ConversionFactor/ConversionOffset from the catalog"
            )
        value = round(
            value * (factor if factor is not None else 1.0)
            + (offset if offset is not None else 0.0),
            3,
        )
    elif conv not in _PLAIN:
        # Unknown conversion: do not silently pass the raw integer off as a real value.
        raise DecodeError(f"unsupported conversion {conversion!r}")

    return _apply_enum(value, enum)


def _apply_enum(value: Any, enum: dict[Any, str] | None) -> Any:
    if not enum:
        return value
    return enum.get(value, enum.get(str(value), value))


def is_date(conversion: str | None) -> bool:
    """Whether this conversion holds a calendar date that can be read and written as one."""
    return (conversion or "").strip().lower() in _DATES


def decode_date(field: bytes, conversion: str | None) -> date | None:
    """The date a DayToDate or DateBCD field holds, or None when it holds none.

    A field of zeros is how the controller stores "not set": a holiday programme that was
    never entered reads as day 0, which would otherwise show as 1 January 1970. A BCD field
    that is not a real date (an unprogrammed 0xFF, a month 13) is not set either.
    """
    if not field or not any(field):
        return None
    conv = (conversion or "").strip().lower()
    if conv == "daytodate":
        days = int.from_bytes(field[:8], "little", signed=False)
        try:
            return date.fromordinal(_EPOCH.toordinal() + days)
        except (OverflowError, ValueError):
            return None
    if conv == "datebcd":
        text = decode_datetime_bcd(field)
        try:
            return date.fromisoformat(text[:10]) if text else None
        except ValueError:
            return None
    raise DecodeError(f"conversion {conversion!r} does not hold a date")


def encode_date(value: date, conversion: str | None, width: int) -> bytes:
    """The field bytes that store `value` in a DayToDate or DateBCD datapoint of `width` bytes.

    DayToDate is the day count since 1970-01-01, least significant byte first, in as many
    bytes as the datapoint has; a date that does not fit (before 1970, or past what two bytes
    can count) is refused rather than wrapped. DateBCD is the same eight bytes the clock is
    set with, at midnight.

    1 January 1970 is the date that reads back as "not set" (see decode_date), so it is
    written as not set too -- all zeros, in either form. That is how a holiday is cleared.
    """
    conv = (conversion or "").strip().lower()
    if conv in _DATES and value == _EPOCH:
        return bytes(width)
    if conv == "daytodate":
        days = (value - _EPOCH).days
        if days < 0 or days >= 1 << (8 * width):
            raise DecodeError(f"{value.isoformat()} does not fit {width} bytes of days")
        return days.to_bytes(width, "little")
    if conv == "datebcd":
        if width != 8:
            raise DecodeError(f"DateBCD needs 8 bytes, the datapoint has {width}")
        return encode_datetime_bcd(datetime(value.year, value.month, value.day))
    raise DecodeError(f"conversion {conversion!r} cannot be written as a date")
