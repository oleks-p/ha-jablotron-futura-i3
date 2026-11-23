"""Coordinator: decodes the register words an i3 controller publishes.

The Modbus integration this is forked from polled the unit in blocks. Here the
words arrive on their own, one channel at a time, so there is nothing to poll:
every publish updates the client's word cache, and a short debounce later the
whole register map is decoded again from that cache -- with exactly the decoding
the Modbus integration uses, so every entity reads the same.

A periodic refresh remains, but it reads nothing. It re-checks the controller's
heartbeat so a silent controller does take the entities unavailable.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as ha_dt

from .const import (
    DEFAULT_NAME,
    DOMAIN,
    LIVENESS_INTERVAL,
    MIN_FIRMWARE,
    NTC_DISCONNECTED_RAW,
    PUBLISH_DEBOUNCE,
    STORE_SAVE_DELAY,
    STORE_VERSION,
)
from .i3_client import FuturaI3Client
from .registers import (
    BY_KEY,
    CONFIG_BITS,
    DIGITAL_INPUT_BITS,
    ERROR_BITS,
    EXT_SENSOR_INVALID_BITS,
    MODE_BITS,
    REGISTERS,
    WARNING_BITS,
    Kind,
    Reg,
    Table,
    decode_valve_topology,
)

_LOGGER = logging.getLogger(__name__)

#: Room-sensor families and how many slots each can hold. External sensors are
#: room readings too, just written into the unit rather than measured by it.
PERIPHERAL_FAMILIES: tuple[tuple[str, int], ...] = (
    ("wall_ui", 3),
    ("wall_sensor", 8),
    ("alfa", 8),
    ("ext_sensor", 8),
)

#: External slots, each declaring itself in use in its own first register.
EXTERNAL_SLOTS: tuple[str, ...] = ("ext_sensor", "ext_button")


def decode_u32(words: list[int], offset: int) -> int:
    """Futura uint32: the first register holds the low 16 bits."""
    return (words[offset + 1] << 16) | words[offset]


def decode_i16(word: int) -> int:
    return word - 0x10000 if word & 0x8000 else word


def decode_mac(words: list[int]) -> str:
    """Futura MAC array: the two bytes inside each register are swapped."""
    octets: list[int] = []
    for word in words:
        octets.append(word & 0xFF)
        octets.append(word >> 8)
    return ":".join(f"{octet:02x}" for octet in octets)


def decode_firmware(words: list[int]) -> str:
    """Futura firmware: the low register packs major and minor as bytes."""
    return f"FUA.P.{words[0] >> 8:02d}.{words[0] & 0xFF:02d}"


def firmware_tuple(words: list[int]) -> tuple[int, int]:
    return (words[0] >> 8, words[0] & 0xFF)


def encode_u32(value: int) -> list[int]:
    """Split a uint32 into [low, high] for an FC16 write."""
    return [value & 0xFFFF, (value >> 16) & 0xFFFF]


def decode_register(reg: Reg, words: list[int], offset: int) -> Any:
    """Turn the raw words at `offset` into the register's logical value."""
    if reg.kind is Kind.MAC:
        return decode_mac(words[offset : offset + 3])
    if reg.kind is Kind.FIRMWARE:
        return decode_firmware(words[offset : offset + 2])
    if reg.kind in (Kind.U32, Kind.BITS32):
        raw: int = decode_u32(words, offset)
    elif reg.kind is Kind.I16:
        raw = decode_i16(words[offset])
    else:
        raw = words[offset]

    if reg.kind in (Kind.BITS16, Kind.BITS32):
        return raw
    if reg.scale != 1.0:
        return round(raw * reg.scale, 3)
    return raw


def bits_to_flags(value: int, table: dict[int, str], prefix: str) -> dict[str, bool]:
    return {f"{prefix}{name}": bool(value >> bit & 1) for bit, name in table.items()}


@dataclass
class FuturaData:
    """One poll's worth of decoded state."""

    values: dict[str, Any] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    present: set[str] = field(default_factory=set)
    topology: dict[str, list[dict[str, int]]] = field(default_factory=dict)
    raw: dict[str, list[int]] = field(default_factory=dict)
    stale_blocks: list[str] = field(default_factory=list)

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


class FuturaCoordinator(DataUpdateCoordinator[FuturaData]):
    """Decodes the Futura's register map from the words its i3 publishes."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: FuturaI3Client,
        options: dict[str, Any] | None = None,
        device_name: str = DEFAULT_NAME,
        store_key: str | None = None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=LIVENESS_INTERVAL),
        )
        self.client = client
        self.device_name = device_name
        self._options = options or {}
        #: Registers every word of which some channel (or the configuration,
        #: or a kept static value) provides. Only these get entities, as only
        #: polled ones did before. Worked out again once the cache is loaded.
        self._registers: tuple[Reg, ...] = ()
        self.update_registers()
        self._store: Store[dict[str, Any]] | None = (
            Store(hass, STORE_VERSION, store_key) if store_key else None
        )
        self._pending: CALLBACK_TYPE | None = None
        self.data = FuturaData()
        client.add_listener(self._on_publish)

    def update_registers(self) -> None:
        covered = self.client.covered()
        self._registers = tuple(
            reg
            for reg in REGISTERS
            if all((reg.table, reg.address + i) in covered for i in range(reg.words))
        )

    @property
    def serial(self) -> int:
        return self.client.serial

    @property
    def registers(self) -> tuple[Reg, ...]:
        return self._registers

    @property
    def polled_keys(self) -> set[str]:
        """Keys of the registers that are bridged, which decides the entities."""
        return {reg.key for reg in self._registers}

    # -- lifecycle -------------------------------------------------------

    async def async_load(self) -> None:
        """Take the word cache back from the last run."""
        if self._store is None:
            return
        saved = await self._store.async_load()
        self.client.restore(saved)
        self.update_registers()

    async def async_save_now(self) -> None:
        if self._store is not None:
            await self._store.async_save(self.client.snapshot())

    def _schedule_save(self) -> None:
        if self._store is not None:
            self._store.async_delay_save(self.client.snapshot, STORE_SAVE_DELAY)

    async def async_shutdown(self) -> None:
        if self._pending is not None:
            self._pending()
            self._pending = None
        await self.async_save_now()
        await super().async_shutdown()

    # -- updates ---------------------------------------------------------

    @callback
    def _on_publish(self) -> None:
        """A channel moved: decode again once the burst it belongs to is over.

        The controller publishes a polling round as a run of messages a few
        milliseconds apart, and decoding the whole map for each would redo the
        same work dozens of times.
        """
        self._schedule_save()
        if self._pending is not None:
            return
        self._pending = async_call_later(self.hass, PUBLISH_DEBOUNCE, self._flush)

    @callback
    def _flush(self, _now: Any = None) -> None:
        self._pending = None
        problem = self.client.problem()
        if problem is not None:
            if self.last_update_success:
                # Let the periodic refresh report it, with the reason logged once.
                self.hass.async_create_task(self.async_request_refresh())
            return
        data = self._build()
        self._check_firmware(data)
        self.async_set_updated_data(data)

    async def _async_update_data(self) -> FuturaData:
        problem = self.client.problem()
        if problem is not None:
            raise UpdateFailed(problem)
        data = self._build()
        self._check_firmware(data)
        return data

    def _build(self) -> FuturaData:
        """Decode every bridged register currently known."""
        data = FuturaData(stale_blocks=self.client.failed_channels())
        for reg in self._registers:
            words = self.client.words_for(reg.table, reg.address, reg.words)
            if words is None:
                continue
            data.raw[f"{reg.table.value}:{reg.address}"] = words
            data.values[reg.key] = decode_register(reg, words, 0)
        self._decode(data)
        return data

    # -- decoding --------------------------------------------------------

    def _decode(self, data: FuturaData) -> None:
        self._decode_flags(data)
        self._decode_presence(data)
        self._decode_derived(data)

    @staticmethod
    def _decode_flags(data: FuturaData) -> None:
        for key, table, prefix in (
            ("mode_bits", MODE_BITS, "mode_"),
            ("error_bits", ERROR_BITS, "error_"),
            ("warning_bits", WARNING_BITS, "warning_"),
            ("config_bits", CONFIG_BITS, "config_"),
            ("digital_inputs", DIGITAL_INPUT_BITS, "input_"),
        ):
            value = data.values.get(key)
            if value is not None:
                data.flags.update(bits_to_flags(int(value), table, prefix))

        data.flags["any_error"] = bool(data.values.get("error_bits") or 0)
        data.flags["any_warning"] = bool(data.values.get("warning_bits") or 0)

    @staticmethod
    def _decode_presence(data: FuturaData) -> None:
        """Work out which peripherals exist and which features are available.

        A set bit maps to slot ``bit + 1``; the slots are not densely packed, so
        a unit with five sensors can occupy slots 2 to 6 and leave slot 1 empty.
        """
        def slots(mask: int | None, count: int, prefix: str) -> None:
            if mask is None:
                return
            for slot in range(1, count + 1):
                if int(mask) >> (slot - 1) & 1:
                    data.present.add(f"{prefix}_{slot}")

        slots(data.values.get("connected_wall_ui"), 3, "wall_ui_present")
        slots(data.values.get("connected_wall_sensors"), 8, "wall_sensor_present")
        slots(data.values.get("connected_alfa"), 8, "alfa_present")
        slots(data.values.get("connected_buttons"), 16, "button_present")

        for family in EXTERNAL_SLOTS:
            for slot in range(1, 9):
                if data.values.get(f"{family}_{slot}_present"):
                    data.present.add(f"{family}_present_{slot}")

        if data.flags.get("config_bypass"):
            data.present.add("has_bypass")
        if data.flags.get("config_internal_heating") or data.flags.get(
            "config_coolbreeze_heating"
        ):
            data.present.add("has_heating")
        if data.flags.get("config_coolbreeze_cooling"):
            data.present.add("has_cooling")

        supply = data.values.get("connected_valve_supply")
        exhaust = data.values.get("connected_valve_exhaust")
        if supply is not None:
            data.topology["supply"] = decode_valve_topology(int(supply), exhaust=False)
        if exhaust is not None:
            data.topology["exhaust"] = decode_valve_topology(int(exhaust), exhaust=True)

    @staticmethod
    def _decode_derived(data: FuturaData) -> None:  # noqa: C901
        values = data.values

        # The external NTC reports -99 C when nothing is plugged in. The `is not
        # None` has to come first: the comparison would raise on a missing reading.
        if (
            values.get("temp_outdoor_ntc") is not None
            and round(values["temp_outdoor_ntc"] * 10) == NTC_DISCONNECTED_RAW
        ):
            values["temp_outdoor_ntc"] = None

        # Whatever feeds an external sensor marks the values it has no reading
        # for, and the register then holds a meaningless number -- usually 0.
        for slot in range(1, 9):
            invalid = int(values.get(f"ext_sensor_{slot}_invalid") or 0)
            for bit, flag in EXT_SENSOR_INVALID_BITS.items():
                if invalid >> bit & 1:
                    values[f"ext_sensor_{slot}_{flag.removesuffix('_invalid')}"] = None

        power = values.get("power_consumption")
        recovered = values.get("heat_recovered")
        if power and recovered is not None:
            values["performance_factor"] = round(recovered / power, 2)
        elif recovered is not None:
            values["performance_factor"] = None

        # The app's headline indoor figures are the mean across the connected
        # room sensors, not the extract-air duct probe. Only present slots may be
        # counted: an unpopulated slot reads 0 and would drag the mean down, and
        # the ALFA's optional NTC probe reads 0 when nothing is wired to it.
        def peripheral_readings(metric: str) -> list[float]:
            found: list[float] = []
            for family, slots in PERIPHERAL_FAMILIES:
                for slot in range(1, slots + 1):
                    if f"{family}_present_{slot}" not in data.present:
                        continue
                    value = values.get(f"{family}_{slot}_{metric}")
                    if isinstance(value, (int, float)):
                        found.append(float(value))
            return found

        for metric, key in (("temperature", "temp_mean_indoor"),
                            ("humidity", "humidity_mean_indoor")):
            readings = peripheral_readings(metric)
            values[key] = (
                round(sum(readings) / len(readings), 1) if readings else None
            )

        co2_readings = peripheral_readings("co2")
        if co2_readings:
            worst = max(co2_readings)
            values["co2_max"] = worst
            values["air_quality"] = (
                "good" if worst < 800 else "moderate" if worst < 1200 else "poor"
            )

        ventilation = values.get("ventilation")
        if ventilation is not None:
            values["ventilation_text"] = (
                "off" if ventilation == 0
                else "auto" if ventilation == 6
                else str(ventilation)
            )

        variant = values.get("variant")
        if variant is not None:
            values["model"] = "Futura M" if variant == 2 else "Futura L"

        for key in ("away_begin", "away_end"):
            raw = values.get(key)
            if raw is None:
                continue
            values[f"{key}_set"] = bool(raw)
            # The unit stores local wall-clock seconds, so the offset has to come
            # back off before this is a real instant. 0 means "not set".
            if not raw:
                values[f"{key}_at"] = None
                continue
            naive = dt.datetime.fromtimestamp(int(raw), dt.UTC).replace(tzinfo=None)
            values[f"{key}_at"] = naive.replace(
                tzinfo=ha_dt.DEFAULT_TIME_ZONE
            )

    def _check_firmware(self, data: FuturaData) -> None:
        raw = data.raw.get("input:8")
        if not raw:
            return
        version = firmware_tuple(raw)
        if version < MIN_FIRMWARE and not getattr(self, "_warned_firmware", False):
            self._warned_firmware = True
            _LOGGER.warning(
                "Futura firmware FUA.P.%02d.%02d is older than FUA.P.%02d.%02d; "
                "parts of the register map may be missing",
                version[0], version[1], MIN_FIRMWARE[0], MIN_FIRMWARE[1],
            )

    # -- writing ---------------------------------------------------------

    def _after_write(self) -> None:
        """Show a write straight away; the controller's echo confirms it."""
        self._schedule_save()
        if self.client.problem() is None:
            self.async_set_updated_data(self._build())

    async def async_write(self, key: str, value: float) -> None:
        """Write a register by key, scaling and encoding as its type requires."""
        reg = BY_KEY[key]
        if not reg.writable or reg.table is not Table.HOLDING:
            raise ValueError(f"{key} is not writable")

        if reg.kind is Kind.U32:
            await self.client.write_multiple(reg.address, encode_u32(int(value)))
        else:
            raw = int(round(value / reg.scale)) if reg.scale != 1.0 else int(value)
            await self.client.write_single(reg.address, raw & 0xFFFF)
        self._after_write()

    async def async_write_raw(self, table: str, address: int, value: int) -> None:
        """Escape hatch for registers the map does not model."""
        if table != Table.HOLDING.value:
            raise ValueError("only holding registers are writable")
        await self.client.write_single(address, value)
        self._after_write()

    async def async_stop_all_functions(self) -> None:
        """Clear boost, circulation, overpressure, night and party at once."""
        await self.client.write_multiple(1, [0, 0, 0, 0, 0])
        self._after_write()
