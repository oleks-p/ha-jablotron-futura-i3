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
* ``mb_u16`` covers one register, ``mb_u32`` two, read in one request. That
  matters: the unit refuses a read that starts or ends inside a 32-bit value, so
  a u32 must be one ``mb_u32`` channel, never two ``mb_u16`` ones.
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

#: Channel types that bridge a Modbus register, and how many words each covers.
CHANNEL_WORDS: dict[str, int] = {"mb_u16": 1, "mb_float": 1, "mb_u32": 2}


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

    @property
    def words(self) -> int:
        return CHANNEL_WORDS[self.type]

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
        )


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
    seen: dict[tuple[Table, int], Channel] = {}
    for ch in channels:
        key = (ch.table, ch.address)
        if ch.readable and key in seen and seen[key].readable:
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
            # An mb_u32 over two separate u16 registers is legal Modbus but is
            # not something this map ever does.
            problems.append(f"{ch.point} reads two registers as one at {key[1]}")
    return problems


# ---------------------------------------------------------------------------
# What to bridge
# ---------------------------------------------------------------------------

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
    return "mb_u32" if reg.words == 2 else "mb_u16"


def rtype_for(reg: Reg) -> int:
    return RTYPE_INPUT if reg.table is Table.INPUT else RTYPE_HOLDING
