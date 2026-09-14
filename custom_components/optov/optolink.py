"""Asynchronous Optolink P300 Client communicating via ESPHome Native API Serial Proxy."""

import asyncio
import logging
from typing import Any

import aioesphomeapi

_LOGGER = logging.getLogger(__name__)

START_BYTE = 0x41
ACK = 0x06
NACK = 0x15
EOT = 0x04
ENQ = 0x05

# P300 function codes. These are the numeric meaning of the catalog's own FCRead/FCWrite
# string values:
FC_VIRTUAL_READ = 0x01
FC_VIRTUAL_WRITE = 0x02
FC_PHYSICAL_READ = 0x03
FC_PHYSICAL_WRITE = 0x04
FC_EEPROM_READ = 0x05
FC_EEPROM_WRITE = 0x06
FC_RPC = 0x07  # "Remote_Procedure_Call"
FC_GFA_READ = 0xC9  # 201
FC_GFA_WRITE = 0xCA  # 202

# Catalog FCRead/FCWrite strings -> wire function code. The catalog names the operation per
# datapoint and it is NOT always Virtual_READ: one boiler family uses GFA_READ for 94 of its
# datapoints, which would silently be read with the wrong function code if this were assumed.
FUNCTION_CODES = {
    "Virtual_READ": FC_VIRTUAL_READ,
    "Virtual_WRITE": FC_VIRTUAL_WRITE,
    "Physical_READ": FC_PHYSICAL_READ,
    "Physical_WRITE": FC_PHYSICAL_WRITE,
    "EEPROM_READ": FC_EEPROM_READ,
    "EEPROM_WRITE": FC_EEPROM_WRITE,
    "Remote_Procedure_Call": FC_RPC,
    "GFA_READ": FC_GFA_READ,
    "GFA_WRITE": FC_GFA_WRITE,
}


def function_code(name: str | None, default: int = FC_VIRTUAL_READ) -> int:
    """Resolve a catalog FCRead/FCWrite string to its wire function code."""
    if not name:
        return default
    code = FUNCTION_CODES.get(name)
    if code is None:
        _LOGGER.warning(
            "unknown catalog function code %r, falling back to 0x%02X", name, default
        )
        return default
    return code


# Message identifiers. Byte 0 of a telegram payload is
# (ProtocolIdentifier << 4) | MessageIdentifier and we only ever speak LDAP (PID 0),
# so byte 0 is the bare message identifier.
MSGID_RESPONSE = 0x01
MSGID_ERROR = 0x03

# Error codes carried in the last data byte of an MSGID_ERROR telegram. Undocumented;
# established by probing real hardware:
#   0x01  address not implemented on this hardware. Returned identically for a datapoint the
#         catalog lists but this unit does not have, and for a nonsense address. Permanent --
#         retrying can never succeed.
#   0x04  address exists but the requested range does not match the controller's own datapoint
#         layout: a misaligned length (for example a byte count that is not a whole number of
#         records) or one that overruns into a neighbouring region.
ERR_NOT_IMPLEMENTED = 0x01
ERR_BAD_RANGE = 0x04

# Largest payload the controller returns in one telegram. The wire format allows 255
# (DataLength is a single byte) but 56 is the measured ceiling across every region probed.
# Necessary but NOT sufficient: the controller validates the requested range against its
# datapoint layout, so reads must also respect record boundaries. Re-measure this when adding
# support for a controller family that has not been tried.
MAX_TELEGRAM_PAYLOAD = 56

# How long a failed reconnect holds off the next attempt. A node that is rebooting or off the
# network refuses every try, and a poll cycle asks for hundreds of telegrams: trying again for
# each of them would stretch the cycle by one connection timeout per datapoint.
RECONNECT_INTERVAL = 30.0

# Most incoming bytes kept while nothing reads them. A reply is consumed as it arrives and the
# queue is flushed before every telegram, so only idle chatter ever gets this far: a controller
# that is not being polled announces itself about twice a second, indefinitely.
RX_QUEUE_LIMIT = 1024


def calc_checksum(data: bytes) -> int:
    """Modulo-256 sum over the length byte and the payload.

    Covers neither the leading 0x41 nor the checksum slot itself.
    """
    return sum(data) & 0xFF


class OptolinkDeviceError(Exception):
    """The controller replied with an error telegram for a specific address.

    Distinct from transport failures (timeout, checksum, desync): the link is healthy and the
    answer is definitive, so callers should neither resync nor retry. Typically means the
    datapoint does not exist on this hardware variant -- the catalog covers a whole controller
    family, and not every address in it is implemented by every member.
    """

    def __init__(
        self, message: str, *, address: int | None = None, code: int | None = None
    ):
        super().__init__(message)
        self.address = address
        self.code = code

    @property
    def is_permanent(self) -> bool:
        """True when no amount of retrying can make this address readable.

        Callers should drop the datapoint from the poll set instead of re-asking every cycle.
        Anything else -- notably ERR_BAD_RANGE -- means our request was malformed, which is
        worth surfacing because the fix is on our side.
        """
        return self.code == ERR_NOT_IMPLEMENTED


class OptolinkProtocolError(Exception):
    """A response did not correlate with its request -- the byte stream is out of step.

    A response echoes the message identifier, the function code and the address it answers,
    so it can be matched against what was sent. Checking that matters: without it a late reply
    arriving after a timeout is silently attributed to whichever datapoint is read next, and
    one sensor reports another sensor's value with nothing logged anywhere.
    """


class OptolinkNotConnected(ConnectionError):
    """There is no connection to the node, and none is attempted right now.

    Either the client was closed on purpose, or the node is away and the next reconnect is not
    due yet. Raised at once, without touching the network, so a cycle that runs while the node
    is gone fails each read immediately instead of waiting out a timeout for each.
    """


class OptolinkClient:
    """Handles communication with OptoV controller over ESPHome serial_proxy."""

    def __init__(self, host: str, port: int, encryption_key: str, instance: int = 0):
        self.host = host
        self.port = port
        self.encryption_key = encryption_key
        # Which serial proxy of the node to talk to. A node can expose several; they are
        # numbered in the order device_info lists them, and every request and every incoming
        # chunk carries that number.
        self.instance = instance
        self.client: aioesphomeapi.APIClient | None = None
        self.rx_queue: asyncio.Queue[int] = asyncio.Queue(maxsize=RX_QUEUE_LIMIT)
        self._lock = asyncio.Lock()
        self._synced = False
        self._connected = False
        self.esphome_info: Any | None = None
        # Block datapoints whose records the controller insists on receiving one telegram at a
        # time. Learned from its own ERR_BAD_RANGE rather than assumed -- see write_day_schedule().
        self._no_chunked_write: set[int] = set()
        # When the next reconnect may be tried (event-loop time), and whether the client was
        # closed on purpose, after which it must never connect again by itself.
        self._reconnect_at = 0.0
        self._closed = False

    async def connect(self):
        """Connect to ESPHome Native API and subscribe to serial proxy."""
        async with self._lock:
            self._closed = False
            await self._connect_locked()

    async def _connect_locked(self):
        if self._connected:
            return
        if self.client is not None:
            # Left over from a connection that has since ended. Close it before opening another,
            # or its socket stays open for as long as this process runs.
            await self._disconnect_locked(force=True)

        _LOGGER.info("Connecting to ESPHome at %s:%s...", self.host, self.port)
        client = aioesphomeapi.APIClient(
            address=self.host,
            port=self.port,
            password="",
            noise_psk=self.encryption_key,
        )
        self.client = client

        async def on_stop(expected_disconnect: bool) -> None:
            # The node ended the connection: a reboot, a dropped network, a restart of its API.
            # Without this the link would still count as up, and every later telegram would
            # fail against a client that has no connection. Only the client in use may mark the
            # link down; one that was replaced and ends later must not.
            if self.client is client and not expected_disconnect:
                _LOGGER.warning("ESPHome at %s ended the connection", self.host)
                self._connected = False
                self._synced = False

        def on_data(msg):
            # A node with several proxies reports all of them on one subscription, so anything
            # from another port has to be dropped rather than fed into this port's byte stream.
            if getattr(msg, "instance", self.instance) != self.instance:
                return
            for b in msg.data:
                try:
                    self.rx_queue.put_nowait(b)
                except asyncio.QueueFull:
                    # Nothing is reading, so this is chatter the next telegram flushes anyway.
                    # Dropping it keeps a link that is not polled -- polling switched off for
                    # the entry -- from growing for as long as Home Assistant runs.
                    return

        try:
            await client.connect(on_stop=on_stop, login=True)
            try:
                self.esphome_info = await client.device_info()
                _LOGGER.info(
                    "Connected to ESPHome node '%s' (model: %s, version: %s)",
                    getattr(self.esphome_info, "name", "unknown"),
                    getattr(self.esphome_info, "model", "unknown"),
                    getattr(self.esphome_info, "esphome_version", "unknown"),
                )
            except Exception as e:
                _LOGGER.debug("Could not fetch ESPHome device_info: %s", e)
                self.esphome_info = None

            client.subscribe_serial_proxy_data(on_data)
            await asyncio.wait_for(
                client.serial_proxy_subscribe_await_response(self.instance),
                timeout=5.0,
            )
        except Exception:
            # A connection that got halfway would keep its socket, and keep feeding bytes into
            # a queue nobody reads.
            await self._disconnect_locked(force=True)
            raise
        self._connected = True
        self._synced = False
        _LOGGER.info("Connected to ESPHome serial proxy.")

    async def _reconnect_locked(self) -> None:
        """Connect again after the connection was lost, at most once per RECONNECT_INTERVAL."""
        if self._closed:
            raise OptolinkNotConnected(f"connection to ESPHome at {self.host} was closed")
        loop = asyncio.get_running_loop()
        if loop.time() < self._reconnect_at:
            raise OptolinkNotConnected(f"ESPHome at {self.host} is not connected")
        try:
            await self._connect_locked()
        except Exception as err:
            self._reconnect_at = loop.time() + RECONNECT_INTERVAL
            # A timeout carries no message of its own; its type is the useful part then.
            reason = str(err) or type(err).__name__
            _LOGGER.warning(
                "Could not reconnect to ESPHome at %s: %s; next attempt in %.0f s",
                self.host,
                reason,
                RECONNECT_INTERVAL,
            )
            raise OptolinkNotConnected(
                f"could not reconnect to ESPHome at {self.host}: {reason}"
            ) from err
        _LOGGER.info("Reconnected to ESPHome at %s", self.host)

    async def disconnect(self):
        """Cleanly disconnect. The client does not reconnect by itself afterwards."""
        async with self._lock:
            self._closed = True
            await self._disconnect_locked()

    async def _disconnect_locked(self, force: bool = False):
        """Close the client. `force` drops the socket at once instead of asking the node to
        close it, which is what a connection already known to be dead needs."""
        if self.client:
            try:
                if self._connected:
                    self.client.serial_proxy_write(self.instance, bytes([EOT]))
                await self.client.disconnect(force=force)
            except Exception as e:
                _LOGGER.debug("Error during disconnect: %s", e)
        self.client = None
        self._connected = False
        self._synced = False

    def _flush_rx(self):
        while not self.rx_queue.empty():
            self.rx_queue.get_nowait()

    async def _read_exact(self, count: int, timeout: float = 3.0) -> bytes:
        buf = bytearray()
        for _ in range(count):
            b = await asyncio.wait_for(self.rx_queue.get(), timeout=timeout)
            buf.append(b)
        return bytes(buf)

    async def _sync_p300(self, deadline: float = 4.0):
        """Bring the link into P300.

        Drain whatever is pending, send EOT, then wait and answer what the controller says.
        EOT first matters: it drops the controller out of P300 back to KW whatever state it was
        in, and in KW it announces itself with ENQ (0x05) roughly twice a second. The init
        `16 00 00` is a reply to that announcement, not an opening move, and sending it blind
        mostly lands between two ENQs and is ignored.

        Three replies move it forward: ENQ means "say it now", an ACK after EOT means the
        controller is already listening, and a NACK after EOT means the same. Controllers take
        a few seconds to come round, so the patience is in the deadline rather than in a number
        of tries.
        """
        _LOGGER.debug("Initiating Optolink P300 handshake...")
        self._flush_rx()
        self.client.serial_proxy_write(self.instance, bytes([EOT]))
        sent_init = False
        loop = asyncio.get_running_loop()
        ends_at = loop.time() + deadline

        while loop.time() < ends_at:
            try:
                byte = (await self._read_exact(1, timeout=0.4))[0]
            except (TimeoutError, ConnectionError):
                # Silence: the controller may not have reached its next announcement yet. Only
                # nudge it again if our init went unanswered.
                if sent_init:
                    self.client.serial_proxy_write(self.instance, bytes([EOT]))
                    sent_init = False
                continue

            if byte == ACK and sent_init:
                self._synced = True
                _LOGGER.debug("Optolink P300 protocol synchronized.")
                return
            if byte in (ENQ, ACK, NACK):
                # ENQ is the controller offering; ACK or NACK straight after our EOT means it is
                # listening already. Either way the init goes out now.
                self.client.serial_proxy_write(self.instance, bytes([0x16, 0x00, 0x00]))
                sent_init = True
                continue
            # Anything else is a leftover from a conversation that was interrupted.
            _LOGGER.debug("Ignoring 0x%02X while synchronizing", byte)

        raise ConnectionError(
            f"Failed to synchronize Optolink P300 protocol within {deadline:.0f}s"
        )

    async def _transact(
        self, function_code: int, address: int, data_length: int, data: bytes = b""
    ) -> bytes:
        """Send one LDAP telegram and return the correlated response payload.

        Caller must already hold self._lock and have ensured the link is synced. Returns the
        full response payload including its 5-byte header, so callers can inspect the echo.

        Wire format:

            0x41 | len | b0 | b1 | addrHi | addrLo | dataLen | data... | checksum

            b0 = (ProtocolIdentifier << 4) | MessageIdentifier   -> 0x00 for a request
            b1 = (MessageSequenzNumber << 5) | FunctionCode

        `data_length` is byte 4. Its meaning depends on direction, which is why it is passed
        separately rather than derived from `data`: for a Virtual_READ it is the number of
        bytes wanted back and no data follows, whereas for a write or an RPC it is the length
        of the outgoing data. In both cases it comes from the catalog's BlockLength.

        Raises OptolinkDeviceError for an error telegram (the link is fine, the controller
        refused) and OptolinkProtocolError when the response fails to correlate.
        """
        if not 0 <= data_length <= 0xFF:
            raise ValueError(f"DataLength {data_length} does not fit in one byte")

        payload = (
            bytes(
                [
                    0x00,
                    function_code,
                    (address >> 8) & 0xFF,
                    address & 0xFF,
                    data_length,
                ]
            )
            + data
        )
        t_len = len(payload)
        chk = calc_checksum(bytes([t_len]) + payload)
        tx = bytes([START_BYTE, t_len]) + payload + bytes([chk])

        self._flush_rx()
        self.client.serial_proxy_write(self.instance, tx)

        # The controller acknowledges the telegram before answering it. A NACK immediately
        # followed by an ACK is tolerated rather than treated as a hard failure -- a resync
        # costs seconds and is not warranted here.
        ack = await self._read_exact(1, timeout=2.5)
        if ack[0] == NACK:
            ack = await self._read_exact(1, timeout=2.5)
        if ack[0] != ACK:
            raise OptolinkProtocolError(
                f"controller did not ACK telegram: 0x{ack[0]:02X}"
            )

        hdr = await self._read_exact(2, timeout=2.5)
        if hdr[0] != START_BYTE:
            raise OptolinkProtocolError(f"invalid response header: 0x{hdr[0]:02X}")
        resp_len = hdr[1]

        body = await self._read_exact(resp_len + 1, timeout=2.5)
        resp_payload, resp_chk = body[:-1], body[-1]

        if resp_chk != calc_checksum(bytes([resp_len]) + resp_payload):
            self.client.serial_proxy_write(self.instance, bytes([NACK]))
            raise OptolinkProtocolError("telegram checksum mismatch")
        self.client.serial_proxy_write(self.instance, bytes([ACK]))

        if len(resp_payload) < 5:
            raise OptolinkProtocolError(f"runt response: {resp_payload.hex(' ')}")

        # Correlate before trusting anything in the payload. Verified against the live
        # controller: the echo is exact on both success and error telegrams.
        msgid = resp_payload[0] & 0x0F
        echoed_fc = resp_payload[1] & 0x1F
        echoed_addr = (resp_payload[2] << 8) | resp_payload[3]
        if echoed_addr != address or echoed_fc != function_code:
            raise OptolinkProtocolError(
                f"response does not match request: sent fc=0x{function_code:02X} "
                f"addr=0x{address:04X}, got fc=0x{echoed_fc:02X} addr=0x{echoed_addr:04X}"
            )

        if msgid == MSGID_ERROR:
            raise OptolinkDeviceError(
                f"controller returned error 0x{resp_payload[-1]:02X} for 0x{address:04X}",
                address=address,
                code=resp_payload[-1],
            )
        if msgid != MSGID_RESPONSE:
            raise OptolinkProtocolError(f"unexpected message identifier {msgid}")

        return resp_payload

    async def _transact_retry(
        self, function_code: int, address: int, data_length: int, data: bytes = b""
    ) -> bytes:
        """_transact() plus connect/sync management and one resync-and-retry.

        A lost API connection shows up either through the node's stop callback or as the client
        refusing to send, which raises APIConnectionError -- not a ConnectionError, so it needs
        its own clause. Both end the same way: the dead client is closed and the next attempt
        connects afresh, subject to RECONNECT_INTERVAL.
        """
        async with self._lock:
            for attempt in range(2):
                try:
                    if not self._connected:
                        await self._reconnect_locked()
                    if not self._synced:
                        await self._sync_p300()
                    return await self._transact(
                        function_code, address, data_length, data
                    )
                except OptolinkDeviceError:
                    # The controller answered correctly; it just refused this address. The
                    # link is healthy, so do NOT drop P300 sync and do NOT retry -- a resync
                    # costs several seconds and cannot change a definitive negative answer.
                    raise
                except OptolinkNotConnected:
                    # Closed on purpose, or the node is away and not due another attempt:
                    # asking again straight away cannot give a different answer.
                    raise
                except aioesphomeapi.APIConnectionError as err:
                    if self._connected:
                        _LOGGER.warning(
                            "Lost the connection to ESPHome at %s: %s", self.host, err
                        )
                    self._connected = False
                    await self._disconnect_locked(force=True)
                    if attempt == 1:
                        raise
                except (TimeoutError, ConnectionError, OptolinkProtocolError) as err:
                    _LOGGER.warning(
                        "fc=0x%02X 0x%04X attempt %s failed: %s",
                        function_code,
                        address,
                        attempt + 1,
                        err,
                    )
                    self._synced = False
                    if attempt == 1:
                        raise

    async def read_rpc(self, address: int, prefix: bytes) -> bytes:
        """Single Remote_Procedure_Call read (P300 function code 7).

        See read_rpc_block() for how an array datapoint is assembled from these. Wire format:

            b0 = (ProtocolIdentifier << 4) | MessageIdentifier   -> 0x00 for a request
            b1 = (MessageSequenzNumber << 5) | FunctionCode      -> 0x07 = Remote_Procedure_Call
            b2 = address high, b3 = address low
            b4 = DataLength   (= the catalog's BlockLength, which for an RPC is len(prefix))
            b5..= Data        (= the catalog's PrefixRead bytes, i.e. the entry index)

        Unlike a Virtual_READ the length byte is the size of the *outgoing* parameter, not the
        expected response length -- the controller decides how much it returns.
        """
        resp = await self._transact_retry(FC_RPC, address, len(prefix), prefix)
        return resp[5:]

    async def read_rpc_block(
        self,
        address: int,
        total_bytes: int,
        block_factor: int,
        base_prefix: bytes = b"",
    ) -> bytes:
        """Read an RPC array datapoint as `block_factor` indexed sub-reads and concatenate.

        An RPC array is NOT one large read. It is `BlockFactor` separate requests, each
        carrying the entry index as its read prefix and returning `ByteLength / BlockFactor`
        bytes. This is why plain chunked reads at address+offset are rejected with 0x01 -- the
        address is only valid as an RPC with an index parameter.

        All geometry (address, total_bytes, block_factor) comes from the catalog, so this works
        for any device family without hardcoding.
        """
        if block_factor < 1 or total_bytes < 1 or total_bytes % block_factor:
            raise ValueError(
                f"invalid RPC block geometry: {total_bytes} bytes / {block_factor} entries"
            )

        entry_len = total_bytes // block_factor
        result = bytearray()
        for index in range(block_factor):
            data = await self.read_rpc(address, base_prefix + bytes([index]))
            if len(data) != entry_len:
                _LOGGER.debug(
                    "RPC 0x%04X entry %d returned %d bytes, expected %d",
                    address,
                    index,
                    len(data),
                    entry_len,
                )
            result.extend(data)
        return bytes(result)

    async def read_raw(
        self, address: int, length: int, fc: int = FC_VIRTUAL_READ
    ) -> bytes:
        """Read `length` bytes from `address` in a single read telegram.

        `fc` is the datapoint's own FCRead, resolved via function_code(); it defaults to
        Virtual_READ because that covers ~97% of the catalog, but it must be passed for
        anything else -- notably the boiler family's GFA_READ datapoints.

        `length` must fit MAX_TELEGRAM_PAYLOAD and line up with the controller's own datapoint
        layout, otherwise the controller answers ERR_BAD_RANGE. Use read_block() for anything
        larger or array-shaped -- it handles the chunking and the alignment.
        """
        if not 1 <= length <= MAX_TELEGRAM_PAYLOAD:
            raise ValueError(
                f"read length {length} out of range 1..{MAX_TELEGRAM_PAYLOAD} "
                f"for 0x{address:04X}; use read_block() for larger datapoints"
            )
        resp = await self._transact_retry(fc, address, length)
        return resp[5:]

    async def write_raw(
        self, address: int, data: bytes, fc: int = FC_VIRTUAL_WRITE
    ) -> bool:
        """Write bytes to a memory address.

        The write function code is a separate catalog field from the read one and is not
        always the Virtual pair: a datapoint read over KBUS or EEPROM has to be written back
        the same way, and sending Virtual_WRITE to it is silently wrong rather than refused.
        """
        _LOGGER.debug("write 0x%04X fc=0x%02X data=%s", address, fc, data.hex(" "))
        await self._transact_retry(fc, address, len(data), data)
        return True

    async def read_block(
        self,
        address: int,
        block_length: int,
        block_factor: int,
        record_step: str = "record",
        fc: int = FC_VIRTUAL_READ,
    ) -> bytes:
        """Read a whole IdentGroup=Block datapoint and return its concatenated records.

        A datapoint with BlockFactor >= 1 is an array rather than a single value, so the
        catalog's `block_factor` column alone carries that distinction -- no extra field is
        needed. The array is read as BlockFactor sub-reads of BlockLength/BlockFactor bytes.

        Two things make this cheaper and more correct than the naive single big read:

        * The controller refuses a read whose range does not line up with its own record
          layout (ERR_BAD_RANGE), and caps a telegram at MAX_TELEGRAM_PAYLOAD. A 168-byte
          datapoint simply cannot be fetched in one go.
        * It does, however, happily return *many* records at once. Measured on a live
          controller, a 168-byte datapoint of 56 three-byte records accepts any multiple of 3
          up to 54,
          so the whole datapoint costs 4 telegrams instead of 56 single-record reads.

        `record_step` selects how the address advances between chunks, which is the one thing
        the catalog does not say:

          "record"  address advances by the number of records consumed. Verified against
                    hardware for the weekly-schedule datapoints: an address-stepped read
                    reproduces a contiguous multi-record read exactly, whereas byte-stepping
                    returns the same records in a different order.
          "byte"    address advances by the number of bytes consumed.

        The catalog does not record which of the two a given datapoint uses, so rather than
        guess, callers should use calibrate_record_step() once and pass the measured answer.
        """
        if block_factor < 1 or block_length < 1 or block_length % block_factor:
            raise ValueError(
                f"invalid block geometry for 0x{address:04X}: "
                f"{block_length} bytes / {block_factor} records"
            )
        if record_step not in ("record", "byte"):
            raise ValueError(f"unknown record_step {record_step!r}")

        record_size = block_length // block_factor
        if record_size > MAX_TELEGRAM_PAYLOAD:
            raise ValueError(
                f"record size {record_size} of 0x{address:04X} exceeds the "
                f"{MAX_TELEGRAM_PAYLOAD}-byte telegram limit"
            )
        per_telegram = (MAX_TELEGRAM_PAYLOAD // record_size) * record_size

        result = bytearray()
        done = 0  # records already fetched
        while done < block_factor:
            n = min(per_telegram // record_size, block_factor - done)
            offset = done if record_step == "record" else done * record_size
            chunk = await self.read_raw(address + offset, n * record_size, fc)
            if len(chunk) != n * record_size:
                raise OptolinkProtocolError(
                    f"0x{address:04X} record {done}: asked for {n * record_size} bytes, "
                    f"got {len(chunk)}"
                )
            result.extend(chunk)
            done += n
        return bytes(result)

    async def calibrate_record_step(
        self,
        address: int,
        block_length: int,
        block_factor: int,
        fc: int = FC_VIRTUAL_READ,
    ) -> str:
        """Ask the controller whether a block datapoint's address counts records or bytes.

        Reads the first two records in one telegram -- which is unambiguous, the controller
        returns them in order -- then reads one record at `address + 1` and at
        `address + record_size` and sees which reproduces record 1.

        The catalog does not record which scheme a datapoint uses (see read_block()), so this
        costs three telegrams once per block datapoint at startup and yields the device's own
        answer instead of an assumption.
        """
        record_size = block_length // block_factor
        if block_factor < 2 or record_size * 2 > MAX_TELEGRAM_PAYLOAD:
            return "record"

        reference = await self.read_raw(address, record_size * 2, fc)
        second = reference[record_size : record_size * 2]

        by_record = await self.read_raw(address + 1, record_size, fc)
        if by_record == second:
            # Ambiguous when record_size == 1, but then both schemes coincide anyway.
            return "record"

        by_byte = await self.read_raw(address + record_size, record_size, fc)
        if by_byte == second:
            return "byte"

        _LOGGER.warning(
            "0x%04X: neither address stepping reproduced record 1 "
            "(ref=%s, +1=%s, +%d=%s); assuming record stepping",
            address,
            second.hex(" "),
            by_record.hex(" "),
            record_size,
            by_byte.hex(" "),
        )
        return "record"

    async def read_circuit_schedule(
        self,
        base_address: int,
        day_bytes: int = 24,
        fmt: str = "phase3",
        default_level: int = 0,
        block_length: int | None = None,
        block_factor: int | None = None,
        record_step: str = "record",
        fc: int = FC_VIRTUAL_READ,
    ) -> dict[str, Any]:
        """Read all seven days of a weekly programme.

        With the catalog's block geometry the programme is one block datapoint and comes in
        through read_block(), record-aligned and in a few telegrams. Without it -- the classic
        boiler layout -- each day is its own block at base + day * day_bytes.
        """
        from .conversions import DAYS, decode_day_schedule

        schedule: dict[str, Any] = {}
        if block_length and block_factor:
            raw = await self.read_block(
                base_address, block_length, block_factor, record_step, fc
            )
            per_day = len(raw) // 7
            _LOGGER.debug(
                "Programme 0x%04X: %d bytes (%d records), %d bytes/day",
                base_address,
                len(raw),
                block_factor,
                per_day,
            )
            for day_idx, day in enumerate(DAYS):
                schedule[day] = decode_day_schedule(
                    raw[day_idx * per_day : (day_idx + 1) * per_day], fmt, default_level
                )
            return schedule

        for day_idx, day in enumerate(DAYS):
            addr = base_address + day_idx * day_bytes
            try:
                raw_day = await self.read_raw(addr, day_bytes, fc)
                schedule[day] = decode_day_schedule(raw_day, fmt, default_level)
            except Exception as err:
                _LOGGER.warning(
                    "Could not read programme day %s at 0x%04X: %s", day, addr, err
                )
                schedule[day] = []
        return schedule

    async def write_day_schedule(
        self,
        base_address: int,
        day_index: int,
        raw_data: bytes,
        day_bytes: int = 24,
        block_length: int | None = None,
        block_factor: int | None = None,
        record_step: str = "record",
        fc: int = FC_VIRTUAL_WRITE,
    ) -> bool:
        """Write one day of a weekly programme.

        A block datapoint is written the way it is read: record by record, at the record's
        own address, with the address advancing per `record_step` exactly as read_block()
        advances it -- the controller counts records or bytes and the calibrated answer
        applies to both directions. For the heat-pump programmes that is one telegram per
        switching window, eight per day. Writing a whole day as one telegram at the base
        address lands on day 0 only.

        Without block geometry each day is its own block at base + day * day_bytes and is
        written in one telegram.
        """
        if len(raw_data) != day_bytes:
            raise ValueError(
                f"Schedule data must be exactly {day_bytes} bytes, got {len(raw_data)}"
            )
        if not 0 <= day_index <= 6:
            raise ValueError(f"Day index must be 0-6, got {day_index}")

        if not (block_length and block_factor):
            return await self.write_raw(
                base_address + day_index * day_bytes, raw_data, fc
            )

        record_size = block_length // block_factor
        if record_size < 1 or day_bytes % record_size:
            raise ValueError(
                f"day of {day_bytes} bytes is not a whole number of {record_size}-byte records"
            )
        records_per_day = day_bytes // record_size
        first = day_index * records_per_day
        offset = first if record_step == "record" else first * record_size
        day_address = base_address + offset

        # A day is a run of consecutive records, and the controller reads many records in one
        # telegram, so try to write it in one too: eight telegrams per day become one, and the
        # day lands as a unit rather than as eight partial states. A controller that will not
        # take it answers ERR_BAD_RANGE, which is a definitive no for that datapoint, so it is
        # remembered and the slow path used from then on.
        if (
            base_address not in self._no_chunked_write
            and len(raw_data) <= MAX_TELEGRAM_PAYLOAD
        ):
            try:
                return await self.write_raw(day_address, raw_data, fc)
            except OptolinkDeviceError as err:
                if err.code != ERR_BAD_RANGE:
                    raise
                self._no_chunked_write.add(base_address)
                _LOGGER.info(
                    "0x%04X refuses a whole-day write; writing its records one at a time",
                    base_address,
                )

        for i in range(records_per_day):
            index = first + i
            record_offset = index if record_step == "record" else index * record_size
            await self.write_raw(
                base_address + record_offset,
                raw_data[i * record_size : (i + 1) * record_size],
                fc,
            )
        return True

    async def read_error_history(
        self,
        base_address: int,
        total_bytes: int,
        block_factor: int,
        function_code: int = FC_RPC,
        record_step: str = "auto",
    ) -> bytes:
        """Read a controller error-history buffer, honouring its catalog function code.

        Two shapes exist across the device families (all per the catalog, nothing hardcoded):

        * `FCRead = Remote_Procedure_Call` (heat pumps) -- the buffer is an RPC array and MUST
          be fetched as `block_factor` indexed sub-reads. Reading it as plain chunked memory
          gets 0x01 "not implemented" from the controller.
        * `FCRead = Virtual_READ` (boilers) -- an ordinary block datapoint, fetched by
          read_block() on its own record boundaries.
        """
        if function_code == FC_RPC:
            return await self.read_rpc_block(base_address, total_bytes, block_factor)
        if record_step == "auto":
            record_step = await self.calibrate_record_step(
                base_address, total_bytes, block_factor, function_code
            )
            _LOGGER.debug(
                "error history 0x%04X uses %s address stepping",
                base_address,
                record_step,
            )
        return await self.read_block(
            base_address, total_bytes, block_factor, record_step, function_code
        )
