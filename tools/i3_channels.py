#!/usr/bin/env python3
"""Give an i3 controller the channels this integration reads a Futura through.

    python3 tools/i3_channels.py BACKUP.json --controller A1B2 --slave 1 \\
        --ext-sensors 1,2,4,8 --prune --out BACKUP-futura.json

Reads a backup exported from the i3 app and writes a copy in which the named
controller has one channel per Futura register the integration needs, ready to
import back through the app. Only that controller's Modbus channels for that
slave change; every other unit, channel and automation is copied through as is.

For each register it needs, in order of preference:

* a channel that already bridges it is KEPT, its polling period adjusted;
* otherwise a channel is ADDED, numbered after the highest one in use.

A write-only channel is never turned into a read/write one, and never counts as
the register's reader: it gets a read channel of its own beside it. A
write-only channel is how a controller feeds the unit, and the i3 rewrites it
every period whether or not the value changed. A read/write channel writes only
on a change, and the Futura drops an external sensor that has not been written
for about two minutes -- with a supply zone left without its sensor, it raises
warning bit 9 ("incorrect configuration of a supply ventilation zone").

`--prune` removes the slave's channels that bridge nothing the integration
needs. Without it they are left alone and listed. A channel any automation
on any controller refers to is never removed, whatever the flags say.

Which peripherals are fitted decides which slot registers get channels, because
a channel per register of every possible slot would be some 130 channels for
peripherals that are not there. The Futura reports what is fitted, and the
Modbus integration's diagnostics list it under `present`.

Nothing here talks to the controller or the unit. The output is a file; what to
do with it is up to whoever imports it.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from custom_components.jablotron_futura_i3.i3_layout import (  # noqa: E402
    BYTE_ORDERS,
    CHANNEL_WORDS,
    DATA_TYPE_U32,
    EXACT_ORDER_FOR_WORD,
    Fitted,
    byte_swap_code,
    channel_type_for,
    channels_from_backup,
    check_layout,
    period_for,
    rtype_for,
    wanted_registers,
)
from custom_components.jablotron_futura_i3.registers import Table  # noqa: E402

#: The app's display handle for a plain bridged value.
DEFAULT_ENTITY_ID = 201
DEFAULT_GROUPS = "0x0000000000000000"
NAME_LIMIT = 31


def _slots(text: str | None) -> tuple[int, ...]:
    if not text:
        return ()
    return tuple(sorted({int(part) for part in text.split(",") if part.strip()}))


def _unit(backup: dict, controller: str) -> dict:
    for unit in backup["configurations"]["unitBackup"]:
        if str(unit["unitId"]).upper() == controller.upper():
            return unit
    raise SystemExit(f"controller {controller} is not in the backup")


def _cfg(raw: dict) -> dict:
    return raw["config"] if isinstance(raw.get("config"), dict) else raw


def referenced_points(backup: dict, controller: str) -> set[str]:
    """`type/n` of every Modbus channel on `controller` an automation may name.

    A rule refers to a channel as `<type code> <n> <state>`, then a literal
    short carrying the device id, little-endian (none for the rule's own
    unit). The type codes are the ones the reverse-engineered rule format
    pins for the three bridge types. This is a byte scan, not a decoder, and it
    errs one way only: it can report a channel that no rule uses, never miss
    one that is used. A channel it reports is kept.
    """
    type_codes = {0x27: "mb_u16", 0x29: "mb_u32", 0x2A: "mb_float"}
    device = bytes.fromhex(controller)[::-1]
    found: set[str] = set()
    for unit in backup["configurations"]["unitBackup"]:
        local = str(unit["unitId"]).upper() == controller.upper()
        for rule in unit.get("automations") or []:
            for value in rule.values() if isinstance(rule, dict) else []:
                if not isinstance(value, str) or len(value) < 8:
                    continue
                try:
                    blob = base64.b64decode(value, validate=True)
                except ValueError:
                    continue
                for i in range(len(blob) - 1):
                    ctype = type_codes.get(blob[i])
                    if ctype is None:
                        continue
                    if local or device in blob[i + 2 : i + 8]:
                        found.add(f"{ctype}/{blob[i + 1]}")
    return found


def _name(reg, half: int = -1) -> str:
    tail = "" if half < 0 else (" lo" if half == 0 else " hi")
    head = f"F {reg.table.value[0].upper()}{reg.address} "
    return head + reg.key[: NAME_LIMIT - len(head) - len(tail)] + tail


def plan(backup: dict, controller: str, slave: int, fitted: Fitted, prune: bool):
    unit = _unit(backup, controller)
    config = unit["config"]
    existing, skipped = channels_from_backup(backup, controller, slave)
    raw_by_point = {}
    for ctype in CHANNEL_WORDS:
        for raw in config.get(ctype) or []:
            cfg = _cfg(raw)
            if int(cfg.get("dev_id", -1)) == slave:
                raw_by_point[f"{ctype}/{int(cfg['id'])}"] = raw

    by_register: dict[tuple[Table, int], list] = {}
    for ch in existing:
        by_register.setdefault((ch.table, ch.address), []).append(ch)

    template = None
    for raw in config.get("mb_u16") or []:
        if int(_cfg(raw).get("dev_id", -1)) == slave:
            template = raw
            break
    groups = DEFAULT_GROUPS
    for ch in existing:
        if ch.table is Table.HOLDING and ch.address == 0:
            groups = _cfg(raw_by_point[ch.point]).get("groups", DEFAULT_GROUPS)

    used = set()
    log: list[tuple[str, str, str, str]] = []
    next_id = {
        ctype: max([int(_cfg(r)["id"]) for r in config.get(ctype) or []] + [0]) + 1
        for ctype in CHANNEL_WORDS
    }

    order_of_word = {}
    for word, order in EXACT_ORDER_FOR_WORD.items():
        code = byte_swap_code(order)
        if code is None:
            raise SystemExit(
                f"the app's byte_swap code for {order} is not known yet: add it to "
                "BYTE_SWAP_CODES in i3_layout.py, read from a backup with such a channel"
            )
        order_of_word[word] = (code, BYTE_ORDERS[order])

    for reg in wanted_registers(fitted):
        want_type = channel_type_for(reg)
        period_ms = period_for(reg) * 1000
        # A 32-bit register is two half channels, one per word; anything else
        # is one channel. mb_u32 and full-width 32-bit channels are never
        # reused: a Hydrogen N accepts them and never reads them.
        halves = (0, 1) if reg.words == 2 else (-1,)
        for half in halves:
            label = reg.key if half < 0 else f"{reg.key} {'lo' if half == 0 else 'hi'}"
            wanted_shape = order_of_word[half][1] if half >= 0 else (-1, False)
            candidates = [
                ch for ch in by_register.get((reg.table, reg.address), [])
                if ch.point not in used and ch.type != "mb_u32" and ch.readable
                and (ch.half, ch.swapped) == wanted_shape
                and (half >= 0 or ch.words == reg.words)
            ]
            candidates.sort(key=lambda ch: ch.type != want_type)
            if candidates:
                ch = candidates[0]
                used.add(ch.point)
                cfg = _cfg(raw_by_point[ch.point])
                changes = []
                if int(cfg.get("period", 0)) != period_ms:
                    changes.append(
                        f"period {int(cfg.get('period', 0)) // 1000}s -> {period_ms // 1000}s"
                    )
                    cfg["period"] = period_ms
                log.append((
                    "kept" if not changes else "changed", ch.point, label,
                    ", ".join(changes) or cfg.get("name", ""),
                ))
                continue

            if template is None:
                raise SystemExit(f"{controller} has no Modbus channel to copy settings from")
            raw = copy.deepcopy(template)
            cfg = _cfg(raw)
            n = next_id[want_type]
            next_id[want_type] += 1
            cfg.update(
                {
                    "dev_id": slave,
                    "start_addr": reg.address,
                    "register_type": rtype_for(reg),
                    "period": period_ms,
                    "timeout": 0,
                    "data_type": DATA_TYPE_U32 if half >= 0 else 1,
                    "byte_swap": order_of_word[half][0] if half >= 0 else 5,
                    "multiplier": 1,
                    "bias": 0,
                    "id": n,
                    "name": _name(reg, half),
                    "entity_id": DEFAULT_ENTITY_ID,
                    "groups": groups,
                    "flags": 0,
                }
            )
            config.setdefault(want_type, []).append(raw)
            used.add(f"{want_type}/{n}")
            log.append(("added", f"{want_type}/{n}", label, f"every {period_ms // 1000}s"))

    referenced = referenced_points(backup, controller)
    for ch in existing:
        if ch.point in used:
            continue
        name = _cfg(raw_by_point[ch.point]).get("name", "")
        if prune and ch.point not in referenced:
            config[ch.type] = [r for r in config[ch.type] if r is not raw_by_point[ch.point]]
            log.append(("removed", ch.point, f"{ch.table.value} {ch.address}", name))
        else:
            why = "an automation uses it" if ch.point in referenced else "not needed"
            log.append(("left", ch.point, f"{ch.table.value} {ch.address}", f"{name} ({why})"))

    after, after_skipped = channels_from_backup(backup, controller, slave)
    return log, after, skipped, check_layout(after)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("backup", type=Path)
    ap.add_argument("--controller", required=True)
    ap.add_argument("--slave", type=int, default=1)
    ap.add_argument("--ext-sensors", help="external sensor slots in use, e.g. 1,2,4,8")
    ap.add_argument("--ext-buttons", help="external button slots in use")
    ap.add_argument("--wall-ui", help="wall controller slots fitted")
    ap.add_argument("--wall-sensors", help="wall CO2 sensor slots fitted")
    ap.add_argument("--alfa", help="ALFA controller slots fitted")
    ap.add_argument("--only-fitted-flags", action="store_true",
                    help="read the in-use flag of the fitted external slots only")
    ap.add_argument("--invalid-flags", action="store_true",
                    help="also read the external sensors' invalid-value flags")
    ap.add_argument("--prune", action="store_true",
                    help="remove the slave's channels nothing here needs")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    backup = json.loads(args.backup.read_text(encoding="utf-8"))
    fitted = Fitted(
        wall_ui=_slots(args.wall_ui),
        wall_sensors=_slots(args.wall_sensors),
        alfa=_slots(args.alfa),
        ext_sensors=_slots(args.ext_sensors),
        ext_buttons=_slots(args.ext_buttons),
        all_slot_flags=not args.only_fitted_flags,
        ext_sensor_invalid_flags=args.invalid_flags,
    )
    log, after, skipped, problems = plan(
        backup, args.controller, args.slave, fitted, args.prune
    )

    width = max(len(row[1]) for row in log)
    for action, point, what, note in log:
        print(f"{action:8} {point:<{width}}  {what:<34} {note}")
    counts = Counter(action for action, *_ in log)
    types = Counter(ch.type for ch in after)
    per_minute = 0.0
    unit = _unit(backup, args.controller)
    for ctype in CHANNEL_WORDS:
        for raw in unit["config"].get(ctype) or []:
            cfg = _cfg(raw)
            if int(cfg.get("dev_id", -1)) == args.slave and cfg.get("period"):
                per_minute += 60_000 / int(cfg["period"])
    print()
    print(", ".join(f"{n} {a}" for a, n in sorted(counts.items())))
    print(f"{len(after)} channels for slave {args.slave} on {args.controller}: "
          + ", ".join(f"{n} {t}" for t, n in sorted(types.items())))
    print(f"about {per_minute:.0f} requests a minute on the controller's Modbus link")
    for line in skipped:
        print(f"note: not a register channel this integration reads: {line}")
    for line in problems:
        print(f"PROBLEM: {line}")

    args.out.write_text(json.dumps(backup, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written {args.out}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
