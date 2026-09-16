"""Raw bytes to values: the rules the catalog's own columns imply."""

import pytest
from decode import (
    DecodeError,
    decode_int,
    decode_value,
    decodes_to_integer,
    decodes_to_number,
    extract_bitfield,
    insert_bitfield,
    is_signed,
)


def test_signing_follows_the_declared_width_not_the_byte_count():
    # A one-byte SInt datapoint is 16-bit signed by declaration, so 0x80 stays positive.
    assert decode_int(b"\x80", "SInt") == 128
    assert decode_int(b"\x80", "SByte") == -128
    assert decode_int(b"\x9c\xff", "SInt") == -100


def test_unsigned_types_are_not_masked_to_their_name():
    # The LON setpoints are two-byte `Byte` datapoints; masking would turn 20.00 into 2.08.
    assert decode_int(b"\xd0\x07", "Byte") == 2000
    assert decode_value(b"\xd0\x07", "Div100", parameter_type="Byte") == 20.0


def test_high_byte_first_types_reverse_first():
    assert decode_int(b"\x07\xd0", "IntHighByteFirst") == 2000


def test_an_unknown_type_reads_unsigned_little_endian():
    assert decode_int(b"\x80", "Nonsense") == 128


def test_bit_fields_are_msb_first_within_each_byte():
    assert extract_bitfield(b"\x80", 0, 1) == 1
    assert extract_bitfield(b"\x01", 0, 1) == 0
    # Sensor health rides at bit 20 of a three-byte block: the low nibble of byte 2.
    assert extract_bitfield(b"\x00\x00\x06", 20, 4) == 6
    assert extract_bitfield(b"\x00\x00\x60", 16, 4) == 6


def test_writing_a_bit_field_leaves_its_neighbours_alone():
    block = b"\xab\xcd\xef"
    out = insert_bitfield(block, 20, 4, 0x9)
    assert extract_bitfield(out, 20, 4) == 0x9
    assert out[:2] == block[:2]
    assert out[2] >> 4 == block[2] >> 4


def test_a_bit_field_that_does_not_fit_is_refused():
    with pytest.raises(DecodeError):
        extract_bitfield(b"\x00", 4, 8)
    with pytest.raises(DecodeError):
        insert_bitfield(b"\x00", 0, 0, 1)


def test_divisors_and_enums():
    assert decode_value(b"\x8a\x00", "Div10", parameter_type="SInt") == 13.8
    assert decode_value(b"\x02", None, parameter_type="Byte", enum={2: "Normal"}) == "Normal"


def test_what_counts_as_a_number():
    assert decodes_to_number("Div10") and not decodes_to_integer("Div10")
    assert decodes_to_integer("NoConversion") and decodes_to_integer("Mult10")
    assert not decodes_to_number("DateTimeBCD")
    assert is_signed("SInt") and not is_signed("Byte")
