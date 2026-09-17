"""The fault buffer, whose texts are the catalog's and whose end is a zero code."""

from errors import decode_wp_error_history, get_error_description


def entry(index: int, epoch: int, code: int) -> bytes:
    return bytes([index]) + epoch.to_bytes(4, "little") + bytes([code, 0, 0])


def test_a_fault_text_comes_from_the_catalog_or_not_at_all():
    assert (
        get_error_description("ff", {"FF": "Controller restart"})
        == "Controller restart"
    )
    assert get_error_description("C9", {}) == "0xC9"


def test_the_first_empty_slot_ends_the_history():
    raw = (
        entry(1, 1_780_000_000, 0xC9)
        + entry(2, 1_770_000_000, 0xA9)
        + entry(3, 0, 0x00)
    )
    history = decode_wp_error_history(raw, 8, {"C9": "Refrigerant circuit"})
    assert [item["code"] for item in history] == ["C9", "A9"]
    assert history[0]["description"] == "Refrigerant circuit"
    assert history[1]["description"] == "0xA9"


def test_nothing_written_yet_is_an_empty_history():
    assert decode_wp_error_history(bytes(24), 8, {}) == []
