"""Calendar dates both ways: the holiday programme's first and last day and their kind."""

from datetime import date

import pytest
from decode import DecodeError, decode_date, decode_value, encode_date, is_date


def test_only_pure_date_conversions_count_as_dates():
    assert is_date("DayToDate")
    assert is_date("DateBCD")
    # A timestamp with a time of day is the clock, not a date setting.
    assert not is_date("DateTimeBCD")
    assert not is_date("NoConversion")
    assert not is_date(None)


def test_day_count_round_trip_in_two_bytes():
    field = encode_date(date(2026, 12, 24), "DayToDate", 2)
    assert field == (date(2026, 12, 24) - date(1970, 1, 1)).days.to_bytes(2, "little")
    assert decode_date(field, "DayToDate") == date(2026, 12, 24)
    # The reading path agrees with the date path.
    assert decode_value(field, "DayToDate") == "2026-12-24"


def test_zero_means_not_set():
    # A holiday programme never entered reads as day 0, not as 1 January 1970.
    assert decode_date(b"\x00\x00", "DayToDate") is None
    assert decode_date(bytes(8), "DateBCD") is None


def test_day_count_refuses_what_does_not_fit():
    with pytest.raises(DecodeError):
        encode_date(date(1969, 12, 31), "DayToDate", 2)
    # Two bytes of days end on 2149-06-06.
    assert encode_date(date(2149, 6, 6), "DayToDate", 2) == b"\xff\xff"
    with pytest.raises(DecodeError):
        encode_date(date(2149, 6, 7), "DayToDate", 2)


def test_bcd_date_is_the_clock_format_at_midnight():
    field = encode_date(date(2026, 10, 8), "DateBCD", 8)
    # 8 October 2026 is a Thursday: weekday byte 4 counts Sunday as 0.
    assert field == bytes([0x20, 0x26, 0x10, 0x08, 0x04, 0x00, 0x00, 0x00])
    assert decode_date(field, "DateBCD") == date(2026, 10, 8)


def test_bcd_garbage_is_not_a_date():
    assert decode_date(b"\xff" * 8, "DateBCD") is None


def test_bcd_needs_eight_bytes():
    with pytest.raises(DecodeError):
        encode_date(date(2026, 10, 8), "DateBCD", 4)


def test_the_epoch_clears_the_date_in_both_forms():
    assert encode_date(date(1970, 1, 1), "DayToDate", 2) == b"\x00\x00"
    assert encode_date(date(1970, 1, 1), "DateBCD", 8) == bytes(8)
    assert decode_date(encode_date(date(1970, 1, 1), "DateBCD", 8), "DateBCD") is None


def test_two_dates_in_a_circuit_are_its_holiday_in_address_order():
    from catalog_db import _holidays

    profile = {
        "dates": [
            # Out of order on purpose; the return day sits after the departure in memory.
            {"id": "end1", "address": "0xB100", "byte_position": 2, "circuit": "HC1"},
            {"id": "start1", "address": "0xB100", "byte_position": 0, "circuit": "HC1"},
            {"id": "start2", "address": "0x3309", "byte_position": 0, "circuit": "HC2"},
            {"id": "end2", "address": "0x3311", "byte_position": 0, "circuit": "HC2"},
            # One date alone, and dates without a circuit, are not paired.
            {"id": "lone", "address": "0x2309", "byte_position": 0, "circuit": "HC3"},
            {"id": "a", "address": "0x2309", "byte_position": 0},
            {"id": "b", "address": "0x2311", "byte_position": 0},
        ]
    }
    assert _holidays(profile) == {
        "HC1": {"start": "start1", "end": "end1"},
        "HC2": {"start": "start2", "end": "end2"},
    }
