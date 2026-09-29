"""Reading an array datapoint: where each record sits, and how many fit in one telegram.

The addresses are the whole point. A block datapoint's address counts records for the
bitmap programmes and three-byte groups for the phase ones, while a telegram's length field
counts bytes, so the two only agree when a read covers exactly one record. Everything here
pins that arithmetic, and that every record is its own telegram.
"""

import asyncio

import optolink
from optolink import OptolinkClient, OptolinkDeviceError, address_step_bytes


class Recorder:
    """Stands in for the wire: notes every read and answers with a marked pattern.

    Byte i of the datapoint comes back as i & 0xFF, so a wrongly addressed or wrongly
    ordered read shows up as wrong content rather than merely a wrong call count.
    """

    def __init__(self, base, refuse_all=None, short=False):
        self.base = base
        self.refuse_all = refuse_all  # error code for every read; None answers normally
        self.short = short  # every answer comes back a byte short
        self.calls = []  # (address, length)

    async def read_raw(self, address, length, fc=optolink.FC_VIRTUAL_READ):
        self.calls.append((address, length))
        if self.refuse_all is not None:
            raise OptolinkDeviceError("refused", address=address, code=self.refuse_all)
        start = (address - self.base) * self.step_bytes
        if self.short:
            length -= 1
        return bytes((start + i) & 0xFF for i in range(length))


def client_with(recorder, record_size, step_bytes):
    recorder.record_size = record_size
    recorder.step_bytes = step_bytes
    client = OptolinkClient("esphome://node", "node")
    client.read_raw = recorder.read_raw
    return client


def run(coro):
    return asyncio.run(coro)


# -- the address rule ------------------------------------------------------------------


def test_a_bitmap_programme_steps_one_address_per_record():
    # Type 2: 168 bytes in 7 records of 24. The catalog's own spacing says the same thing --
    # heating circuit 1 at 0x9000, circuit 2 at 0x9007, seven addresses later.
    assert address_step_bytes(2, 24) == 24


def test_a_phase_programme_steps_one_address_per_three_bytes():
    for mapping_type in (5, 6, 7, 8):
        assert address_step_bytes(mapping_type, 3) == 3


def test_anything_else_steps_one_address_per_byte():
    assert address_step_bytes(None, 8) == 1
    assert address_step_bytes(0, 8) == 1
    assert address_step_bytes(3, 10) == 1


# -- one record per telegram ----------------------------------------------------------


def test_a_phase_programme_is_read_one_record_per_telegram():
    rec = Recorder(0x9200)
    client = client_with(rec, record_size=3, step_bytes=3)
    data = run(client.read_block(0x9200, 168, 56, step_bytes=3))

    assert data == bytes(i & 0xFF for i in range(168))
    assert rec.calls == [(0x9200 + i, 3) for i in range(56)]


def test_a_bitmap_programme_addresses_records_not_bytes():
    rec = Recorder(0x9000)
    client = client_with(rec, record_size=24, step_bytes=24)
    data = run(client.read_block(0x9000, 168, 7, step_bytes=24))

    assert data == bytes(i & 0xFF for i in range(168))
    assert rec.calls == [(0x9000 + i, 24) for i in range(7)]


def test_a_byte_counting_programme_steps_by_the_record_size():
    # A week of seven 8-byte days, type 1: day i at base + 8 i.
    rec = Recorder(0x2000)
    client = client_with(rec, record_size=8, step_bytes=1)
    data = run(client.read_block(0x2000, 56, 7, step_bytes=1))

    assert data == bytes(range(56))
    assert rec.calls == [(0x2000 + 8 * i, 8) for i in range(7)]


def test_a_boiler_error_buffer_is_addressed_by_its_type_not_probed():
    # Ten 9-byte records, type 3: record i at base + 9 i. Nothing asks for base + 1, which
    # such a controller refuses as an address it does not have.
    rec = Recorder(0x7507)
    client = client_with(rec, record_size=9, step_bytes=1)
    data = run(
        client.read_error_history(
            0x7507,
            total_bytes=90,
            block_factor=10,
            function_code=optolink.FC_VIRTUAL_READ,
            step_bytes=address_step_bytes(3, 9),
        )
    )

    assert data == bytes(range(90))
    assert rec.calls == [(0x7507 + 9 * i, 9) for i in range(10)]


def test_a_refused_record_reaches_the_caller():
    rec = Recorder(0x92E0, refuse_all=optolink.ERR_BAD_RANGE)
    client = client_with(rec, record_size=3, step_bytes=3)

    try:
        run(client.read_block(0x92E0, 168, 56, step_bytes=3))
    except OptolinkDeviceError as err:
        assert err.code == optolink.ERR_BAD_RANGE
    else:
        raise AssertionError("a refused record must not be swallowed")


def test_a_short_record_is_an_error_not_data():
    rec = Recorder(0x2000, short=True)
    client = client_with(rec, record_size=8, step_bytes=1)
    try:
        run(client.read_block(0x2000, 56, 7, step_bytes=1))
    except optolink.OptolinkProtocolError:
        pass
    else:
        raise AssertionError("a record of the wrong length must not pass as data")


class WriteRecorder:
    def __init__(self):
        self.calls = []

    async def write_raw(self, address, data, fc=optolink.FC_VIRTUAL_WRITE):
        self.calls.append((address, bytes(data)))
        return True


def test_a_programme_day_is_written_one_record_per_telegram():
    # Day 2 of a phase programme: eight 3-byte windows from record 16 on.
    rec = WriteRecorder()
    client = OptolinkClient("esphome://node", "node")
    client.write_raw = rec.write_raw
    day = bytes(range(24))
    run(client.write_day_schedule(0x9200, 2, day, 24, 168, 56, step_bytes=3))

    assert rec.calls == [(0x9200 + 16 + i, day[3 * i : 3 * i + 3]) for i in range(8)]


def test_out_of_range_refusal_does_not_retire_the_address():
    """0x21 refuses a value, not an address: the setting must stay in the profile."""
    err = OptolinkDeviceError("refused", address=0x7A07, code=optolink.ERR_OUT_OF_RANGE)
    assert not err.is_permanent
