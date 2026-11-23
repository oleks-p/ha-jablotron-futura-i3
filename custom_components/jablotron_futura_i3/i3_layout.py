"""Which i3 channel carries which Futura register.

The integration never talks to the Futura. An i3 controller masters the unit's
Modbus link, polls the registers it has a channel for, and publishes each
channel's value over MQTT. So the integration needs a map from a channel
(`mb_u16/23`) to the register behind it (holding 0), and that map lives in one
place only: the controller's own configuration, as the i3 app exports it in a
backup. This module reads it from there.

What a channel's configuration says, as established on a working site:

* ``dev_id`` is the Modbus slave the channel addresses, ``start_addr`` the first
  register.
* ``register_type`` is the function the controller uses. Three are relied on
  here, each observed against a Futura and the result confirmed on the unit:
  ``1`` reads an input register (FC4), ``8`` reads a holding register and writes
  it when the channel is commanded (FC3 + FC6), ``7`` only writes one. The app
  offers others -- coils, discrete inputs, multiple-register writes -- that a
  Futura has no use for, so a channel of any other type is reported and left
  out rather than guessed at.
* A 32-bit register is one channel reading both words in one request. That
  matters: the unit refuses a read that starts or ends inside a 32-bit value, so
  a u32 must never be two 16-bit channels. On a Hydrogen N the working form is
  an ``mb_u16`` channel with ``data_type`` 3: ``mb_u32`` channels were accepted
  by the app there but never read, while the Futura saw no request for them.
  ``mb_u32`` is still understood, since other controllers may read it.
* ``multiplier`` scales the published value. The channels this integration
  adds use 1, so a channel publishes the raw register; an existing channel with
  another multiplier (a setpoint already shown in the app in degrees) is
  divided back out.

Nothing here touches Home Assistant, so the tool that edits a backup and the
integration that reads one share this module.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

from .registers import REGISTERS, Kind, Reg, Table

#: i3 `register_type` values, as observed on a Futura.
RTYPE_INPUT = 1
RTYPE_HOLDING_WRITE = 7
RTYPE_HOLDING = 8

#: Channel types that bridge a Modbus register, and how many words each covers
#: unless its data type says otherwise.
CHANNEL_WORDS: dict[str, int] = {"mb_u16": 1, "mb_float": 1, "mb_u32": 2}

#: i3 `data_type` of a 32-bit unsigned value, which makes a channel read two
#: registers in one request whatever its channel type.
DATA_TYPE_U32 = 3

#: A 16-bit channel type with the 32-bit data type reads both registers of a
#: pair -- which the unit requires -- and assembles them in its byte order. It
#: then holds the result as a 32-bit FLOAT, so anything wider than 24 bits loses
#: its lowest bits to rounding, and publishes the low 16 bits of that. Measured
#: on a Hydrogen N, every byte order on three registers, all twelve readings as
#: that model predicts (DCBA on the supply valves showed 768 where an exact
#: value would have been 650).
#:
#: So each such channel delivers one word of the pair, possibly byte-swapped:
#:
#:   order   assembles                        keeps
#:   ABCD    low << 16 | high                 high
#:   CDAB    high << 16 | low                 low
#:   BADC    swap(low) << 16 | swap(high)     swap(high)
#:   DCBA    swap(high) << 16 | swap(low)     swap(low)
#:
#: and it is exact while the assembled value fits 24 bits. For a register whose
#: high word stays below 256 two channels are exact whatever the low word holds:
#: CDAB (below 2**24) for the low word, BADC (every set bit within bits 8-31)
#: for the high one. That covers the mode, error and warning bitfields, the wall
#: sensor mask and the hardware revision. Nothing reads a wider register exactly.
HALF_CHANNEL_TYPES = frozenset({"mb_u16"})

#: Byte order name -> (word of the pair it keeps, whether its bytes are swapped).
BYTE_ORDERS: dict[str, tuple[int, bool]] = {
    "ABCD": (1, False),
    "CDAB": (0, False),
    "BADC": (1, True),
    "DCBA": (0, True),
}
#: The app's `byte_swap` code for each order. 0 is ABCD; the others are read
#: from a backup that has channels in them.
BYTE_SWAP_CODES: dict[int, str] = {0: "ABCD"}
#: The order each word of an exact pair is read in.
EXACT_ORDER_FOR_WORD: dict[int, str] = {0: "CDAB", 1: "BADC"}


def byte_swap_code(order: str) -> int | None:
    for code, name in BYTE_SWAP_CODES.items():
        if name == order:
            return code
    return None


@dataclass(frozen=True, slots=True)
class Channel:
    """One i3 channel bridging one Futura register (or a 32-bit pair)."""

    type: str
    n: int
    table: Table
    address: int
    readable: bool
    writable: bool
    multiplier: float = 1.0
    name: str = ""
    #: Registers the channel reads in one request: 1, or 2 for a 32-bit value.
    span: int = 0
    #: For a channel that reads a pair but delivers one word of it: which word
    #: (0 = the first). -1 for a channel that delivers everything it reads.
    half: int = -1
    #: Whether that word arrives with its two bytes swapped.
    swapped: bool = False

    @property
    def words(self) -> int:
        return self.span or CHANNEL_WORDS[self.type]

    @property
    def point(self) -> str:
        """The channel's address within its controller, as topics spell it."""
        return f"{self.type}/{self.n}"

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["table"] = self.table.value
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Channel:
        return cls(
            type=str(data["type"]),
            n=int(data["n"]),
            table=Table(data["table"]),
            address=int(data["address"]),
            readable=bool(data["readable"]),
            writable=bool(data["writable"]),
            multiplier=float(data.get("multiplier", 1.0)),
            name=str(data.get("name", "")),
            span=int(data.get("span", 0)),
            half=int(data.get("half", -1)),
            swapped=bool(data.get("swapped", False)),
        )

    @property
    def delivers(self) -> tuple[int, ...]:
        """Offsets, from `address`, of the register words a publish carries."""
        if self.half >= 0:
            return (self.half,)
        return tuple(range(self.words))


class LayoutError(ValueError):
    """The backup cannot be read as a layout for this unit."""


def _units(backup: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    try:
        units = backup["configurations"]["unitBackup"]
    except (KeyError, TypeError) as err:
        raise LayoutError("not an i3 app backup") from err
    if not isinstance(units, list):
        raise LayoutError("not an i3 app backup")
    return units


def controllers(backup: Mapping[str, Any]) -> dict[str, str]:
    """Controller id -> model, for every unit in a backup."""
    return {
        str(unit.get("unitId")): str(unit.get("unitModel") or unit.get("unitName"))
        for unit in _units(backup)
    }


def unit_config(backup: Mapping[str, Any], controller: str) -> Mapping[str, Any]:
    for unit in _units(backup):
        if str(unit.get("unitId", "")).upper() == controller.upper():
            return unit.get("config") or {}
    raise LayoutError(f"controller {controller} is not in the backup")


def _channel_config(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """The backup nests each channel's settings one level down."""
    inner = raw.get("config")
    return inner if isinstance(inner, Mapping) else raw


def channels_from_backup(
    backup: Mapping[str, Any], controller: str, slave: int
) -> tuple[list[Channel], list[str]]:
    """Every channel on `controller` that bridges a register of `slave`.

    Returns the channels and a list of human-readable reasons for each channel
    that addresses the slave but was left out.
    """
    config = unit_config(backup, controller)
    found: list[Channel] = []
    skipped: list[str] = []
    for ctype in CHANNEL_WORDS:
        for raw in config.get(ctype) or []:
            cfg = _channel_config(raw)
            if int(cfg.get("dev_id", -1)) != slave:
                continue
            n = int(cfg["id"])
            rtype = int(cfg.get("register_type", -1))
            name = str(cfg.get("name") or "")
            if rtype == RTYPE_INPUT:
                table, readable, writable = Table.INPUT, True, False
            elif rtype == RTYPE_HOLDING:
                table, readable, writable = Table.HOLDING, True, True
            elif rtype == RTYPE_HOLDING_WRITE:
                table, readable, writable = Table.HOLDING, False, True
            else:
                skipped.append(f"{ctype}/{n} {name!r}: register type {rtype}")
                continue
            multiplier = float(cfg.get("multiplier") or 1.0)
            span = 2 if int(cfg.get("data_type", -1)) == DATA_TYPE_U32 else CHANNEL_WORDS[ctype]
            half, swapped = -1, False
            if span == 2 and ctype in HALF_CHANNEL_TYPES:
                swap = int(cfg.get("byte_swap", -1))
                order = BYTE_SWAP_CODES.get(swap)
                if order is None:
                    skipped.append(
                        f"{ctype}/{n} {name!r}: a 32-bit read kept to 16 bits, "
                        f"with a byte order ({swap}) whose half is not known"
                    )
                    continue
                half, swapped = BYTE_ORDERS[order]
            found.append(
                Channel(
                    type=ctype,
                    n=n,
                    table=table,
                    address=int(cfg["start_addr"]),
                    readable=readable,
                    writable=writable,
                    multiplier=round(multiplier, 6),
                    name=name,
                    span=span,
                    half=half,
                    swapped=swapped,
                )
            )
    return found, skipped


def check_layout(channels: Iterable[Channel]) -> list[str]:
    """Problems with a layout that would make registers decode wrongly.

    A u32 must sit on an ``mb_u32`` channel at its first word, and nothing may
    cover only half of one. Two readable channels on the same register are not
    an error -- the first one wins -- but they are reported.
    """
    problems: list[str] = []
    composite = {
        (reg.table, reg.address): reg for reg in REGISTERS if reg.words == 2
    }
    inside = {
        (reg.table, reg.address + 1): reg for reg in REGISTERS if reg.words == 2
    }
    channels = list(channels)
    halves: dict[tuple[Table, int], set[int]] = {}
    for ch in channels:
        if ch.half >= 0 and ch.readable:
            halves.setdefault((ch.table, ch.address), set()).add(ch.half)
    full = {(ch.table, ch.address) for ch in channels if ch.half < 0 and ch.words == 2}
    for key, got in halves.items():
        reg = composite.get(key)
        if reg is not None and got != {0, 1} and key not in full:
            missing = "first" if 0 not in got else "second"
            problems.append(f"{reg.key}: no channel carries its {missing} word")
    seen: dict[tuple[Table, int], Channel] = {}
    for ch in channels:
        key = (ch.table, ch.address)
        if (
            ch.readable and key in seen and seen[key].readable
            and ch.words == seen[key].words and ch.delivers == seen[key].delivers
        ):
            problems.append(
                f"{ch.point} reads {ch.table.value} {ch.address}, "
                f"which {seen[key].point} already reads"
            )
        seen.setdefault(key, ch)
        if key in inside:
            problems.append(
                f"{ch.point} starts inside {inside[key].key} "
                f"({ch.table.value} {inside[key].address}-{inside[key].end})"
            )
        reg = composite.get(key)
        if reg is not None and ch.words != 2:
            problems.append(
                f"{ch.point} reads half of {reg.key}; it needs an mb_u32 channel"
            )
        if reg is None and ch.words == 2 and key not in inside:
            # A 32-bit read over two separate u16 registers is legal Modbus but
            # is not something this map ever does.
            problems.append(f"{ch.point} reads two registers as one at {key[1]}")
    return problems


# ---------------------------------------------------------------------------
# What to bridge
# ---------------------------------------------------------------------------

#: 32-bit registers whose high word stays below 256, the only ones a pair of
#: 16-bit channels reads exactly (see BYTE_ORDERS).
EXACT_U32_KEYS: frozenset[str] = frozenset(
    {"hw_revision", "mode_bits", "error_bits", "warning_bits", "connected_wall_sensors"}
)

#: Not bridged by default. The MAC is three registers and an i3 channel reads
#: at most two, so it comes from configuration instead. The rest back entities
#: that are disabled by default and cost a channel each.
SKIP_KEYS: frozenset[str] = frozenset(
    {
        "mac_address",
        "build_number",
        "regmap_version",
        "stat_reads",
        "stat_writes",
        "stat_fails",
        "uin1_voltage",
        "uin2_voltage",
        "digital_inputs",
        "zone_identify",
        "access_code",
        "user_password",
        "password_timeout",
    }
)

#: Polling period per register, in seconds. The controller publishes a channel
#: only when its value changes, so this is how quickly a change shows up, not
#: how often Home Assistant hears from it.
PERIOD_COMMAND = 10      # what a button press changes: functions, level, mode
PERIOD_LIVE = 30         # measurements
PERIOD_SETTING = 60      # setpoints and feature switches
PERIOD_STATIC = 600      # identity, fitted peripherals, configuration

_COMMAND_KEYS = frozenset(
    {
        "ventilation",
        "boost_timer",
        "circulation_timer",
        "overpressure_timer",
        "night_timer",
        "party_timer",
        "mode_bits",
    }
)


def period_for(reg: Reg) -> int:
    if reg.key in _COMMAND_KEYS:
        return PERIOD_COMMAND
    if reg.table is Table.HOLDING:
        if reg.key.startswith(("ext_sensor_", "ext_button_")):
            if reg.key.endswith("_present"):
                return PERIOD_STATIC
            return PERIOD_LIVE
        return PERIOD_STATIC if reg.config else PERIOD_SETTING
    if reg.tier.value in ("once", "slow"):
        return PERIOD_STATIC
    if reg.key in ("config_bits", "rtc_battery_voltage", "variant"):
        return PERIOD_STATIC
    if reg.key in ("error_bits", "warning_bits", "filter_wear"):
        return PERIOD_SETTING
    return PERIOD_LIVE


@dataclass(frozen=True, slots=True)
class Fitted:
    """Which of the unit's slots are in use, so only those get channels."""

    wall_ui: tuple[int, ...] = ()
    wall_sensors: tuple[int, ...] = ()
    alfa: tuple[int, ...] = ()
    ext_sensors: tuple[int, ...] = ()
    ext_buttons: tuple[int, ...] = ()
    #: Read every external slot's in-use flag, not only the fitted ones, so a
    #: slot a feeding system starts using later is noticed. One channel each.
    all_slot_flags: bool = True
    ext_sensor_invalid_flags: bool = False


def _slot(key: str, family: str) -> int | None:
    if not key.startswith(f"{family}_"):
        return None
    head = key[len(family) + 1 :].split("_", 1)[0]
    return int(head) if head.isdigit() else None


def wanted_registers(fitted: Fitted) -> list[Reg]:
    """The registers a unit with these peripherals needs bridged."""
    out: list[Reg] = []
    for reg in REGISTERS:
        if reg.key in SKIP_KEYS or reg.kind is Kind.MAC:
            continue
        # The serial comes from configuration; the firmware and valve masks do
        # not change on a running unit and are kept from what was last known;
        # the away times are too wide to read exactly.
        if reg.words == 2 and reg.key not in EXACT_U32_KEYS:
            continue
        if reg.key.endswith("_temp_correction"):
            continue
        if reg.key.endswith(("_address", "_options")) and not reg.enabled_default:
            continue
        slot_ok = True
        for family, slots in (
            ("wall_ui", fitted.wall_ui),
            ("wall_sensor", fitted.wall_sensors),
            ("alfa", fitted.alfa),
        ):
            slot = _slot(reg.key, family)
            if slot is not None:
                slot_ok = slot in slots
        for family, slots in (
            ("ext_sensor", fitted.ext_sensors),
            ("ext_button", fitted.ext_buttons),
        ):
            slot = _slot(reg.key, family)
            if slot is None:
                continue
            if reg.key.endswith("_present"):
                slot_ok = fitted.all_slot_flags or slot in slots
            elif reg.key.endswith("_invalid"):
                slot_ok = slot in slots and fitted.ext_sensor_invalid_flags
            else:
                slot_ok = slot in slots
        if slot_ok:
            out.append(reg)
    return out


def channel_type_for(reg: Reg) -> str:
    """Every channel is an mb_u16; a 32-bit register gets two half channels."""
    return "mb_u16"


def rtype_for(reg: Reg) -> int:
    return RTYPE_INPUT if reg.table is Table.INPUT else RTYPE_HOLDING
