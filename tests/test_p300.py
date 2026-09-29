"""The P300 telegram: what goes out, and how an answer is matched to it."""

import asyncio

import optolink
from optolink import OptolinkClient, calc_checksum


class FakeTransport:
    """Collects what was written and answers each request with the next canned reply."""

    def __init__(self, client, answers=()):
        self.client = client
        self.written = bytearray()
        self.answers = list(answers)

    def write(self, data):
        self.written.extend(data)
        # The single ACK the client sends after an answer is not a request.
        if len(data) > 1 and self.answers:
            for byte in self.answers.pop(0):
                self.client.rx_queue.put_nowait(byte)


def answer(payload):
    """ACK, then the framed response telegram."""
    body = bytes([len(payload)]) + payload
    return (
        bytes([optolink.ACK, optolink.START_BYTE]) + body + bytes([calc_checksum(body)])
    )


def p300_client(answers):
    client = OptolinkClient("esphome://node", "node")
    client._transport = FakeTransport(client, answers)
    client._link = object()  # connected, as far as the retry wrapper is concerned
    client._synced = True
    return client


def test_a_function_above_five_bits_is_matched_on_its_five_bits():
    # The process read goes out as the byte 0x7B; the function field is its low five bits.
    async def go():
        reply = bytes([0x01, 0x7B, 0x12, 0x34, 0x01, 0x2A])
        client = p300_client([answer(reply)])
        payload = await client._transact(optolink.FC_PROCESS_READ, 0x1234, 1)
        assert bytes(client._transport.written)[3] == 0x7B
        assert payload[5:] == bytes([0x2A])

    asyncio.run(go())


def test_a_short_answer_is_not_taken_for_a_value():
    # Two bytes asked for, one delivered: decoding it would give a wrong reading.
    async def go():
        reply = bytes([0x01, 0x01, 0x08, 0x00, 0x02, 0x2A])
        client = p300_client([answer(reply)])
        try:
            await client.read_raw(0x0800, 2)
        except optolink.OptolinkProtocolError:
            return
        raise AssertionError("a short answer must not pass as a value")

    asyncio.run(go())


def test_a_longer_answer_yields_the_bytes_asked_for():
    async def go():
        reply = bytes([0x01, 0x01, 0x08, 0x00, 0x02, 0x2A, 0x01, 0x99])
        client = p300_client([answer(reply)])
        return await client.read_raw(0x0800, 2)

    assert asyncio.run(go()) == bytes([0x2A, 0x01])
