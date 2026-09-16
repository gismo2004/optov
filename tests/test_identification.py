"""Which controller a reported identification resolves to.

The rows are shaped like the catalog's: a System ID with one row per variant, an
IdentificationExtension of <HardwareIndex><SoftwareIndex> and an optional range end.
"""

from catalog_db import _select_variant


def variant(model, ext, till="", f0=None, f0_till=None):
    return {
        "model": model,
        "ident_ext": ext,
        "ident_ext_till": till,
        "f0": f0,
        "f0_till": f0_till,
    }


# 0x2098, as the catalog holds it: one hardware index, software index 0x00..0x0F.
KW2 = [
    variant("V200KW2", "0100", "0103"),
    variant("V200KW2_4", "0104", "0104"),
    variant("V200KW2_5", "0105", "0105"),
    variant("V200KW2_6", "0106", "010F"),
]

# 0x2053, where the hardware index names the board and the software index repeats across them.
GWG = [
    variant("GWG_VBEM_35", "0135"),
    variant("GWG_VBES_35", "0235"),
    variant("GWG_VWMS_35", "0835"),
    variant("GWG_VBT2_35", "1035"),
]


def test_an_exact_extension_wins():
    assert _select_variant(KW2, 0x01, 0x04, None, 0x98)["model"] == "V200KW2_4"


def test_a_range_covers_the_software_indices_in_between():
    assert _select_variant(KW2, 0x01, 0x02, None, 0x98)["model"] == "V200KW2"
    assert _select_variant(KW2, 0x01, 0x0B, None, 0x98)["model"] == "V200KW2_6"


def test_an_unknown_hardware_index_still_resolves_by_software_index():
    # A controller that reports hardware index 0x00 where the catalog only ever declares 0x01:
    # the byte separates nothing here, and software index 0x01 belongs to one variant only.
    assert _select_variant(KW2, 0x00, 0x01, None, 0x98)["model"] == "V200KW2"


def test_a_hardware_index_that_names_the_board_is_not_guessed_away():
    assert _select_variant(GWG, 0x04, 0x35, None, 0x53) is None


def test_the_extensionless_variant_is_the_catch_all():
    candidates = [variant("V200KW2_6", "0106", "010F"), variant("V200WO1A", "")]
    assert _select_variant(candidates, 0x02, 0x99, None, 0x48)["model"] == "V200WO1A"


def test_f0_only_counts_for_the_devices_that_carry_it():
    # Device byte 0xC2 and software index >= 200 -- the only case where F0 is read.
    candidates = [variant("A", "01C8", f0=3, f0_till=3), variant("B", "01C8", f0=7, f0_till=9)]
    assert _select_variant(candidates, 0x01, 0xC8, 8, 0xC2)["model"] == "B"
    # Same rows, an ordinary device byte: F0 is ignored and the extension decides, first row.
    assert _select_variant(candidates, 0x01, 0xC8, 8, 0x98)["model"] == "A"
