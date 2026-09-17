"""Schedules and timestamps: what goes to the controller comes back unchanged."""

from datetime import datetime

from conversions import (
    decode_datetime_bcd,
    decode_day_schedule,
    decode_time53,
    encode_datetime_bcd,
    encode_day_schedule,
    encode_time53,
)


def test_the_five_three_time_encoding():
    assert decode_time53(0xFF) == ""  # an unset slot, not 31:70
    assert decode_time53(0xC0) == "24:00"
    for text in ("00:00", "06:30", "13:20", "23:50"):
        assert decode_time53(encode_time53(text)) == text


def test_a_three_byte_day_survives_the_round_trip():
    windows = [
        {"window": 1, "start": "04:30", "end": "06:00", "mode": 2},
        {"window": 2, "start": "10:00", "end": "16:00", "mode": 1},
    ]
    raw = encode_day_schedule(windows, "phase3")
    assert len(raw) == 24
    assert decode_day_schedule(raw, "phase3") == windows


def test_windows_at_the_default_level_are_not_windows():
    raw = encode_day_schedule([{"start": "08:00", "end": "09:00", "mode": 0}], "phase3")
    assert raw == bytes(24)
    assert decode_day_schedule(raw, "phase3") == []


def test_a_two_byte_day_keeps_its_times():
    raw = encode_day_schedule([{"start": "05:00", "end": "22:00", "mode": 1}], "phase2")
    assert len(raw) == 8
    assert decode_day_schedule(raw, "phase2") == [
        {"window": 1, "start": "05:00", "end": "22:00", "mode": 1}
    ]


def test_the_bitmap_layout_keeps_quarter_hours_and_levels():
    raw = encode_day_schedule([{"start": "06:00", "end": "07:00", "mode": 2}], "bitmap")
    assert len(raw) == 24
    assert decode_day_schedule(raw, "bitmap") == [
        {"window": 1, "start": "06:00", "end": "07:00", "mode": 2}
    ]


def test_the_timestamp_is_binary_coded_decimal():
    when = datetime(2026, 9, 16, 7, 5, 3)
    raw = encode_datetime_bcd(when)
    assert raw[0:2] == b"\x20\x26"  # the year is 20 26, not 0x07EA
    assert decode_datetime_bcd(raw) == "2026-09-16 07:05:03"


def test_a_short_timestamp_is_not_guessed_at():
    assert decode_datetime_bcd(b"\x20\x26") is None


def test_snap_rounds_onto_grid_and_keeps_none():
    from conversions import snap

    assert snap(None, 5) is None
    assert snap(-11, 5) == -10
    assert snap(-13, 5) == -15
    assert snap(81.2, 1) == 81
    assert snap(6.78, 0.5) == 7.0
    assert snap(6.24, 0.5) == 6.0
    assert snap(65.5, 5) == 65
    # A whole-number step gives an int, so the state has no stray ".0".
    assert isinstance(snap(3.4, 1), int)
