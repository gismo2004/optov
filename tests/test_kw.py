"""The KW telegram: four bytes out, bare data back, and nothing to check it with."""

import asyncio

import optolink
from optolink import OptolinkClient, OptolinkDeviceError


class FakeTransport:
    """Collects what was written and hands back what the controller is meant to answer."""

    def __init__(self, client, answers=()):
        self.client = client
        self.written = bytearray()
        self.answers = list(answers)

    def write(self, data):
        self.written.extend(data)
        # One request, one answer, in the order the test lined them up. The single byte that
        # answers an announcement is not a request and is not answered.
        if len(data) > 1 and self.answers:
            for byte in self.answers.pop(0):
                self.client.rx_queue.put_nowait(byte)


def kw_client(answers=()):
    client = OptolinkClient("esphome://node", "node")
    client.protocol = optolink.PROTO_KW
    client._transport = FakeTransport(client, answers)
    client._synced = True
    return client


def run(coro):
    return asyncio.run(coro)


def test_a_read_is_four_bytes_and_the_answer_is_bare_data():
    async def go():
        client = kw_client([bytes([0x01, 0xF4])])
        client._last_kw_tx = asyncio.get_running_loop().time()
        payload = await client._transact_kw(optolink.FC_VIRTUAL_READ, 0x0800, 2)
        assert bytes(client._transport.written) == bytes([0xF7, 0x08, 0x00, 0x02])
        # Behind the header a P300 answer carries, so callers cannot tell the two apart.
        assert payload[:5] == bytes([0x01, 0x01, 0x08, 0x00, 0x02])
        assert payload[5:] == bytes([0x01, 0xF4])

    run(go())


def test_a_write_is_acknowledged_by_one_byte_of_any_value():
    async def go():
        client = kw_client([bytes([0x00])])
        client._last_kw_tx = asyncio.get_running_loop().time()
        payload = await client._transact_kw(
            optolink.FC_VIRTUAL_WRITE, 0x6300, 1, bytes([0x2D])
        )
        assert bytes(client._transport.written) == bytes([0xF4, 0x63, 0x00, 0x01, 0x2D])
        assert payload[5:] == b""

    run(go())


def test_the_read_function_code_comes_from_the_catalog_not_from_the_default():
    async def go():
        client = kw_client([bytes([0x07])])
        client._last_kw_tx = asyncio.get_running_loop().time()
        await client._transact_kw(optolink.FC_GFA_READ, 0x1234, 1)
        assert bytes(client._transport.written)[0] == 0x6B

    run(go())


def test_an_operation_kw_does_not_have_is_refused_like_a_missing_address():
    async def go():
        client = kw_client()
        client._last_kw_tx = asyncio.get_running_loop().time()
        try:
            await client._transact_kw(optolink.FC_RPC, 0xA801, 8)
        except OptolinkDeviceError as err:
            assert err.code == optolink.ERR_NOT_IMPLEMENTED
            assert err.address == 0xA801
        else:
            raise AssertionError("the RPC should not have been sent")
        assert not client._transport.written

    run(go())


def test_a_line_that_went_quiet_is_taken_again_before_the_telegram():
    async def go():
        client = kw_client([bytes([0x11, 0x22])])
        # Never used, so the controller is announcing itself rather than listening. The
        # announcement arrives while the handshake is already waiting for it, as it does on a
        # line that beacons about twice a second.
        client._last_kw_tx = 0.0

        async def announce():
            await asyncio.sleep(0.05)
            client.rx_queue.put_nowait(optolink.ENQ)

        asyncio.get_running_loop().create_task(announce())
        payload = await client._transact_kw(optolink.FC_VIRTUAL_READ, 0x0802, 2)
        # 0x01 answers the announcement, then the read goes out.
        assert bytes(client._transport.written) == bytes([0x01, 0xF7, 0x08, 0x02, 0x02])
        assert payload[5:] == bytes([0x11, 0x22])

    run(go())


def test_the_answer_must_be_as_long_as_it_was_asked_for():
    async def go():
        client = kw_client([bytes([0x11])])  # one byte where two were wanted
        client._last_kw_tx = asyncio.get_running_loop().time()
        try:
            await asyncio.wait_for(
                client._transact_kw(optolink.FC_VIRTUAL_READ, 0x0800, 2), timeout=3
            )
        except TimeoutError:
            return
        raise AssertionError("a short answer must not pass as a value")

    run(go())
