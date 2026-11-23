"""The Futura's registers, as an i3 controller publishes them over MQTT.

The controller masters the unit's Modbus link. Each register it bridges is a
channel, and the controller publishes a channel's value on

    /devices/<client>/events/<DEV>/<type>/<n>/value

and takes a new one on

    /devices/<client>/commands/<DEV>/<type>/<n>/set

This client keeps the latest raw 16-bit word of every bridged register, which is
exactly what the Modbus client returned, so the register map and every entity
above it decode them unchanged.

Three properties of the controller shape everything here:

* **It publishes a channel only when the value changes, and retains nothing.**
  A register that holds still -- the serial number, a setpoint nobody touches --
  is published once, when the controller first reads it after a boot, and never
  again. So the word cache is persisted across Home Assistant restarts, and a
  word is kept until a newer one arrives. Without that, half the entities would
  read `unknown` after every restart, for as long as nothing changed.
* **Its 32-bit word order is its own.** An ``mb_u32`` channel publishes one
  number built from two registers, and which register it put on top is not
  documented. The Futura's firmware register carries a constant 9999 in its high
  word, so the order is learned from that channel the first time it is seen,
  and checked against the configured serial number when that arrives.
* **It reports liveness on its own uptime counter**, about once a minute, and
  says per channel whether its last read succeeded on ``.../available``. A
  controller that has gone quiet, or a link on which every read fails, takes
  the entities unavailable -- what the Modbus client did when the socket died.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from .i3_layout import Channel
from .registers import Table

_LOGGER = logging.getLogger(__name__)

#: The Futura's firmware register's high word, a constant on every firmware.
FIRMWARE_MARKER = 9999
FIRMWARE_ADDRESS = 8
SERIAL_ADDRESS = 1
MAC_ADDRESS = 3

#: Heartbeats arrive about every 60 s, round-robin with a per-controller phase,
#: so anything below 180 s flaps.
DEFAULT_HEARTBEAT_TIMEOUT = 240

#: How the controller builds an mb_u32 value from the unit's two registers.
#: ``lo_hi``: value = (r[1] << 16) | r[0], the Futura's own convention.
#: ``hi_lo``: value = (r[0] << 16) | r[1], plain big-endian Modbus.
#: The ``_swapped`` forms additionally swap the two bytes inside each word.
WORD_ORDERS: tuple[str, ...] = ("lo_hi", "hi_lo", "lo_hi_swapped", "hi_lo_swapped")
WORD_ORDER_AUTO = "auto"


class FuturaConnectionError(Exception):
    """The controller is silent, or cannot reach the unit."""


class FuturaTimeoutError(FuturaConnectionError):
    """Kept for interface compatibility; the push transport never times out."""


class FuturaModbusError(Exception):
    """The register has no channel that can carry the request."""


def _swap_bytes(word: int) -> int:
    return ((word & 0xFF) << 8) | (word >> 8)


def split_u32(value: int, order: str) -> tuple[int, int]:
    """The (r[0], r[1]) words a controller value stands for under `order`."""
    value &= 0xFFFFFFFF
    high, low = value >> 16, value & 0xFFFF
    if order.startswith("lo_hi"):
        first, second = low, high
    else:
        first, second = high, low
    if order.endswith("_swapped"):
        first, second = _swap_bytes(first), _swap_bytes(second)
    return first, second


def join_u32(first: int, second: int, order: str) -> int:
    """The controller value for words (r[0], r[1]) under `order`."""
    if order.endswith("_swapped"):
        first, second = _swap_bytes(first), _swap_bytes(second)
    if order.startswith("lo_hi"):
        return (second << 16) | first
    return (first << 16) | second


def detect_order_from_firmware(value: int) -> str | None:
    """The word order under which the firmware register's marker lands on top."""
    for order in WORD_ORDERS:
        if split_u32(value, order)[1] == FIRMWARE_MARKER:
            return order
    return None


def detect_order_from_serial(value: int, serial: int) -> str | None:
    for order in WORD_ORDERS:
        first, second = split_u32(value, order)
        if (second << 16) | first == serial:
            return order
    return None


def mac_words(mac: str) -> list[int]:
    """The three registers a MAC reads as: two bytes per word, low byte first."""
    octets = [int(part, 16) for part in mac.replace("-", ":").split(":")]
    if len(octets) != 6:
        raise ValueError(f"not a MAC address: {mac}")
    return [octets[i] | (octets[i + 1] << 8) for i in range(0, 6, 2)]


def parse_number(payload: Any) -> float | None:
    """A channel payload as a number, or None for silence.

    An empty payload is a re-announce request, and an unconfigured channel
    publishes ``-nan``; neither is a reading.
    """
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    text = str(payload).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def format_number(value: float) -> str:
    """A number as the controller expects it: a plain decimal, no exponent."""
    number = float(value)
    if number.is_integer():
        return str(int(number))
    return f"{number:.6f}".rstrip("0").rstrip(".")


class FuturaI3Client:
    """Register words for one Futura, fed by one i3 controller's channels."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        client_id: str,
        controller: str,
        channels: Iterable[Channel],
        serial: int,
        mac: str | None = None,
        word_order: str = WORD_ORDER_AUTO,
        heartbeat_timeout: int = DEFAULT_HEARTBEAT_TIMEOUT,
    ) -> None:
        self.hass = hass
        self.client_id = client_id
        self.controller = controller.upper()
        self.serial = int(serial)
        self.mac = mac
        self.heartbeat_timeout = heartbeat_timeout
        self.channels: list[Channel] = list(channels)
        self._by_point: dict[str, Channel] = {ch.point: ch for ch in self.channels}
        self._readers: dict[tuple[Table, int], Channel] = {}
        self._writers: dict[int, Channel] = {}
        for ch in self.channels:
            key = (ch.table, ch.address)
            if ch.readable and key not in self._readers:
                self._readers[key] = ch
            if ch.writable and ch.table is Table.HOLDING:
                # A read/write channel is preferred over a write-only one,
                # because its echo is a real read of the register.
                current = self._writers.get(ch.address)
                if current is None or (ch.readable and not current.readable):
                    self._writers[ch.address] = ch
        # A write-only channel still publishes the value it last wrote, which is
        # the register's value for as long as nothing else writes it. Better
        # than nothing where no channel reads the register back.
        for ch in self.channels:
            key = (ch.table, ch.address)
            if not ch.readable and key not in self._readers:
                self._readers[key] = ch

        self._order: str | None = None if word_order == WORD_ORDER_AUTO else word_order
        self._configured_order = word_order
        #: Raw register words, in the Futura's own order: (table, address) -> word.
        self._words: dict[tuple[Table, int], int] = {}
        #: The controller's last value per mb_u32 channel, kept so the words can
        #: be (re)derived once the word order is known.
        self._u32_values: dict[str, int] = {}
        #: Channels whose last read the controller reported as failed.
        self._failed: set[str] = set()
        self._last_beat: float | None = None
        self._started_at = time.monotonic()
        self._uptime: float | None = None
        self.restarts = 0
        self._unsubs: list[Callable[[], None]] = []
        self._listeners: list[Callable[[], None]] = []
        self._seed_static()

    # -- identity ------------------------------------------------------------

    @property
    def host(self) -> str:
        """What the entity layer shows where the Modbus client showed an IP."""
        return f"i3 {self.controller}"

    @property
    def configuration_url(self) -> str | None:
        return None

    @property
    def word_order(self) -> str | None:
        return self._order

    def _seed_static(self) -> None:
        """Values that come from configuration rather than from the unit."""
        low, high = self.serial & 0xFFFF, self.serial >> 16
        self._words.setdefault((Table.INPUT, SERIAL_ADDRESS), low)
        self._words.setdefault((Table.INPUT, SERIAL_ADDRESS + 1), high)
        if self.mac:
            for offset, word in enumerate(mac_words(self.mac)):
                self._words[(Table.INPUT, MAC_ADDRESS + offset)] = word

    def covered(self) -> set[tuple[Table, int]]:
        """Every register word this client can produce."""
        out: set[tuple[Table, int]] = set()
        for (table, address), ch in self._readers.items():
            out.update((table, address + i) for i in range(ch.words))
        out.add((Table.INPUT, SERIAL_ADDRESS))
        out.add((Table.INPUT, SERIAL_ADDRESS + 1))
        if self.mac:
            out.update((Table.INPUT, MAC_ADDRESS + i) for i in range(3))
        return out

    # -- persistence ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "words": {f"{t.value}:{a}": w for (t, a), w in sorted(self._words.items())},
            "u32": dict(self._u32_values),
            "order": self._order,
        }

    def restore(self, saved: dict[str, Any] | None) -> None:
        """Take back words from a previous run, keeping anything newer."""
        if not saved:
            return
        if self._order is None and saved.get("order") in WORD_ORDERS:
            self._order = saved["order"]
        for key, word in (saved.get("words") or {}).items():
            table, _, address = key.partition(":")
            try:
                slot = (Table(table), int(address))
            except ValueError:
                continue
            if slot in self.covered():
                self._words[slot] = int(word) & 0xFFFF
        for point, value in (saved.get("u32") or {}).items():
            if point in self._by_point:
                self._u32_values.setdefault(point, int(value))
        self._seed_static()

    def seed(self, words: dict[tuple[Table, int], int]) -> None:
        """Words known from elsewhere -- the Modbus integration being replaced."""
        covered = self.covered()
        for slot, word in words.items():
            if slot in covered:
                self._words[slot] = int(word) & 0xFFFF
        self._seed_static()

    # -- reading -------------------------------------------------------------

    def words_for(self, table: Table, address: int, count: int) -> list[int] | None:
        """The words of one register, or None if any of them is unknown."""
        out: list[int] = []
        for offset in range(count):
            slot = (table, address + offset)
            ch = self._channel_covering(slot)
            if ch is not None and ch.point in self._failed:
                return None
            word = self._words.get(slot)
            if word is None:
                return None
            out.append(word)
        return out

    def _channel_covering(self, slot: tuple[Table, int]) -> Channel | None:
        table, address = slot
        ch = self._readers.get(slot)
        if ch is None:
            prev = self._readers.get((table, address - 1))
            if prev is not None and prev.words == 2:
                return prev
        return ch

    async def read(self, table: str, address: int, count: int) -> list[int]:
        """Modbus-client compatible read, served from the word cache."""
        words = self.words_for(Table(table), address, count)
        if words is None:
            raise FuturaModbusError(f"{table} {address}..{address + count - 1} unknown")
        return words

    def problem(self) -> str | None:
        """Why the unit cannot be considered reachable right now, if it cannot."""
        now = time.monotonic()
        reference = self._last_beat if self._last_beat is not None else self._started_at
        if now - reference > self.heartbeat_timeout:
            return (
                f"controller {self.controller} has not reported for "
                f"{int(now - reference)} s"
            )
        readable = [ch for ch in self.channels if ch.readable]
        if readable and all(ch.point in self._failed for ch in readable):
            return f"controller {self.controller} cannot read the unit"
        return None

    def failed_channels(self) -> list[str]:
        return sorted(self._failed)

    # -- subscription --------------------------------------------------------

    def _topic(self, kind: str, point: str, leaf: str) -> str:
        return f"/devices/{self.client_id}/{kind}/{self.controller}/{point}/{leaf}"

    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def async_start(self) -> None:
        """Subscribe to every channel the layout names, and the heartbeat."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            raise FuturaConnectionError("MQTT is not available")
        for ch in self.channels:
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass,
                    self._topic("events", ch.point, "value"),
                    self._make_value_handler(ch),
                    qos=0,
                )
            )
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass,
                    self._topic("events", ch.point, "available"),
                    self._make_available_handler(ch),
                    qos=0,
                )
            )
        self._unsubs.append(
            await mqtt.async_subscribe(
                self.hass,
                self._topic("events", "system/1", "uptime"),
                self._on_uptime,
                qos=0,
            )
        )

    async def async_close(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    def _make_value_handler(self, ch: Channel) -> Callable[[Any], None]:
        @callback
        def handle(msg: Any) -> None:
            number = parse_number(msg.payload)
            if number is None:
                return
            if self.apply(ch, number):
                self._notify()

        return handle

    def _make_available_handler(self, ch: Channel) -> Callable[[Any], None]:
        @callback
        def handle(msg: Any) -> None:
            number = parse_number(msg.payload)
            if number is None:
                return
            before = ch.point in self._failed
            if number:
                self._failed.discard(ch.point)
            else:
                self._failed.add(ch.point)
            if before != (ch.point in self._failed):
                self._notify()

        return handle

    @callback
    def _on_uptime(self, msg: Any) -> None:
        uptime = parse_number(msg.payload)
        if uptime is None:
            return
        first = self._last_beat is None
        if self._uptime is not None and uptime < self._uptime:
            self.restarts += 1
            _LOGGER.info(
                "Controller %s restarted; it re-publishes every channel as it "
                "reads them again",
                self.controller,
            )
        self._uptime = uptime
        self._last_beat = time.monotonic()
        if first:
            self._notify()

    # -- decoding a publish ----------------------------------------------------

    def apply(self, ch: Channel, number: float) -> bool:
        """Fold one published value into the word cache. True if anything moved."""
        raw = round(number / ch.multiplier) if ch.multiplier not in (0, 1) else round(number)
        if ch.words == 1:
            return self._store(ch.table, ch.address, int(raw) & 0xFFFF)

        value = int(raw) & 0xFFFFFFFF
        self._u32_values[ch.point] = value
        if self._order is None and ch.table is Table.INPUT:
            if ch.address == FIRMWARE_ADDRESS:
                self._order = detect_order_from_firmware(value)
            elif ch.address == SERIAL_ADDRESS:
                self._order = detect_order_from_serial(value, self.serial)
            if self._order is not None:
                _LOGGER.info(
                    "Controller %s builds 32-bit values as %s", self.controller,
                    self._order,
                )
                return self._rederive_u32() or True
        if self._order is None:
            return False
        if ch.table is Table.INPUT and ch.address == SERIAL_ADDRESS:
            # The serial is the entities' identity: it stays as configured, and
            # a controller that disagrees is worth a line in the log.
            first, second = split_u32(value, self._order)
            if (second << 16) | first != self.serial:
                _LOGGER.warning(
                    "Controller %s reports serial %s, configured %s",
                    self.controller, (second << 16) | first, self.serial,
                )
            return False
        first, second = split_u32(value, self._order)
        moved = self._store(ch.table, ch.address, first)
        return self._store(ch.table, ch.address + 1, second) or moved

    def _rederive_u32(self) -> bool:
        assert self._order is not None
        moved = False
        for point, value in self._u32_values.items():
            ch = self._by_point[point]
            if ch.table is Table.INPUT and ch.address == SERIAL_ADDRESS:
                continue
            first, second = split_u32(value, self._order)
            moved = self._store(ch.table, ch.address, first) or moved
            moved = self._store(ch.table, ch.address + 1, second) or moved
        return moved

    def _store(self, table: Table, address: int, word: int) -> bool:
        slot = (table, address)
        if self._words.get(slot) == word:
            return False
        self._words[slot] = word
        return True

    # -- writing -------------------------------------------------------------

    def can_write(self, address: int) -> bool:
        return address in self._writers

    async def write_single(self, address: int, value: int) -> None:
        ch = self._writers.get(address)
        if ch is None or ch.words != 1:
            raise FuturaModbusError(f"no i3 channel writes holding {address}")
        word = int(value) & 0xFFFF
        await self._publish(ch, word * ch.multiplier if ch.multiplier != 1 else word)
        self._store(Table.HOLDING, address, word)

    async def write_multiple(self, address: int, words: list[int]) -> None:
        """Write consecutive registers, one channel at a time.

        The i3 has no multi-register write that spans channels, so a run of
        timers is written as one command per register, and a u32 as one command
        to its mb_u32 channel.
        """
        index = 0
        while index < len(words):
            current = address + index
            ch = self._writers.get(current)
            if ch is None:
                raise FuturaModbusError(f"no i3 channel writes holding {current}")
            if ch.words == 2:
                if index + 1 >= len(words):
                    raise FuturaModbusError(f"holding {current} is 32-bit")
                if self._order is None:
                    raise FuturaModbusError(
                        "the controller's 32-bit word order is not known yet"
                    )
                first, second = words[index] & 0xFFFF, words[index + 1] & 0xFFFF
                await self._publish(ch, join_u32(first, second, self._order))
                self._u32_values[ch.point] = join_u32(first, second, self._order)
                self._store(Table.HOLDING, current, first)
                self._store(Table.HOLDING, current + 1, second)
                index += 2
            else:
                await self.write_single(current, words[index])
                index += 1

    async def _publish(self, ch: Channel, value: float) -> None:
        await mqtt.async_publish(
            self.hass,
            self._topic("commands", ch.point, "set"),
            format_number(value),
            qos=0,
            retain=False,
        )
