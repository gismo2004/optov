"""Reading an array datapoint: where each record sits, and how many fit in one telegram.

The addresses are the whole point. A block datapoint's address counts records for the
bitmap programmes and three-byte groups for the phase ones, while a telegram's length field
counts bytes, so the two only agree when a read covers exactly one record. Everything here
pins that arithmetic, and pins the fallback that exists because some controllers accept a
read spanning several records and some refuse it.
"""

import asyncio

import optolink
from optolink import OptolinkClient, OptolinkDeviceError, address_step_bytes


class Recorder:
    """Stands in for the wire: notes every read and answers with a marked pattern.

    Byte i of the datapoint comes back as i & 0xFF, so a wrongly addressed or wrongly
    ordered read shows up as wrong content rather than merely a wrong call count.
    """

    def __init__(self, base, refuse_multi=None, refuse_all=None):
        self.base = base
        # error codes the fake controller answers with; None means it answers normally
        self.refuse_multi = refuse_multi  # for a read of more than one record
        self.refuse_all = refuse_all  # error code for every read, whatever its size
        self.calls = []  # (address, length)

    async def read_raw(self, address, length, fc=optolink.FC_VIRTUAL_READ):
        self.calls.append((address, length))
        if self.refuse_all is not None:
            raise OptolinkDeviceError("refused", address=address, code=self.refuse_all)
        if self.refuse_multi is not None and length > self.record_size:
            raise OptolinkDeviceError(
                "refused", address=address, code=self.refuse_multi
            )
        start = (address - self.base) * self.step_bytes
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


# -- chunking --------------------------------------------------------------------------


def test_a_phase_programme_is_read_in_four_telegrams_when_allowed():
    rec = Recorder(0x9200)
    client = client_with(rec, record_size=3, step_bytes=3)
    data = run(client.read_block(0x9200, 168, 56, step_bytes=3))

    assert len(data) == 168
    assert data == bytes(i & 0xFF for i in range(168))
    # 56 // 3 = 18 records per telegram, so 18 + 18 + 18 + 2.
    assert rec.calls == [
        (0x9200, 54),
        (0x9212, 54),
        (0x9224, 54),
        (0x9236, 6),
    ]


def test_a_bitmap_programme_addresses_records_not_bytes():
    rec = Recorder(0x9000)
    client = client_with(rec, record_size=24, step_bytes=24)
    data = run(client.read_block(0x9000, 168, 7, step_bytes=24))

    assert len(data) == 168
    # 56 // 24 = 2 records per telegram; the second starts two addresses on, not 48.
    assert rec.calls == [(0x9000, 48), (0x9002, 48), (0x9004, 48), (0x9006, 24)]


# -- the fallback ----------------------------------------------------------------------


def test_a_controller_that_refuses_a_chunk_gets_one_record_per_telegram():
    rec = Recorder(0x92E0, refuse_multi=optolink.ERR_BAD_RANGE)
    client = client_with(rec, record_size=3, step_bytes=3)
    data = run(client.read_block(0x92E0, 168, 56, step_bytes=3))

    assert data == bytes(i & 0xFF for i in range(168))
    assert rec.calls[0] == (0x92E0, 54)  # the one refused telegram
    assert rec.calls[1:] == [(0x92E0 + i, 3) for i in range(56)]


def test_the_refusal_may_carry_any_code():
    # Nothing documents what a controller answers to a read shape it does not recognise, so
    # the fallback may not depend on the code being the one seen so far.
    for code in (0x01, 0x03, 0x04, 0x05, 0x99):
        rec = Recorder(0x9200, refuse_multi=code)
        client = client_with(rec, record_size=3, step_bytes=3)
        data = run(client.read_block(0x9200, 168, 56, step_bytes=3))
        assert data == bytes(i & 0xFF for i in range(168))


def test_the_fallback_is_remembered_for_the_next_read():
    rec = Recorder(0x9200, refuse_multi=optolink.ERR_BAD_RANGE)
    client = client_with(rec, record_size=3, step_bytes=3)
    run(client.read_block(0x9200, 168, 56, step_bytes=3))
    first = len(rec.calls)
    run(client.read_block(0x9200, 168, 56, step_bytes=3))

    assert first == 57  # one refused, then 56
    assert len(rec.calls) - first == 56  # second pass does not repeat the refusal


def test_a_single_record_that_is_refused_reaches_the_caller():
    # This, and only this, means the datapoint is absent from this controller.
    rec = Recorder(0x92E0, refuse_all=optolink.ERR_BAD_RANGE)
    client = client_with(rec, record_size=3, step_bytes=3)

    try:
        run(client.read_block(0x92E0, 168, 56, step_bytes=3))
    except OptolinkDeviceError as err:
        assert err.code == optolink.ERR_BAD_RANGE
    else:
        raise AssertionError("a refused single record must not be swallowed")
