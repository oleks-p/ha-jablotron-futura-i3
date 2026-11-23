"""Taking over from the Modbus integration, in place.

Both integrations build their unique ids the same way -- `<serial>_<key>` -- and
name their entities the same way, so the only thing standing between the two is
the registry: each row records which integration owns it. Deleting the Modbus
entry and adding this one would create every entity afresh, which loses what
the registry holds about them -- the areas they were put in, names and icons
someone chose, which ones were disabled or hidden -- and gives each one an id
generated from the translated name in whatever language the server is set to,
not necessarily the id every dashboard refers to.

So the rows are moved instead. Home Assistant has a call for exactly this,
`async_update_entity_platform`, which keeps the entity id and everything else on
the row and changes only the owner. It refuses an entity that is loaded, so the
Modbus entry is unloaded first.

The device moves the same way: this entry is added to it, under this domain's
identifier, so its area and its id survive too. The Modbus entry is then
removed, which leaves the device to this entry alone.

What the Modbus integration last read is the other thing worth keeping. The
controller publishes a register only when it changes, so a setpoint nobody
touches would otherwise stay unknown until something did. The config flow takes
those words while the Modbus entry is still running, and they are the starting
point here.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, MODBUS_DOMAIN
from .registers import Table

_LOGGER = logging.getLogger(__name__)


def modbus_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return hass.config_entries.async_entries(MODBUS_DOMAIN, include_ignore=False)


def modbus_identity(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Serial, MAC, name and last readings of a Modbus entry's unit."""
    dev_reg = dr.async_get(hass)
    out: dict[str, Any] = {"name": entry.data.get("name") or entry.title}
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        for domain, ident in device.identifiers:
            if domain == MODBUS_DOMAIN and str(ident).isdigit():
                out["serial"] = int(ident)
        for kind, value in device.connections:
            if kind == dr.CONNECTION_NETWORK_MAC:
                out["mac"] = value
        if device.name:
            out["name"] = device.name
    if "serial" not in out:
        # No device yet: the unique ids carry the serial too.
        ent_reg = er.async_get(hass)
        for row in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            head = row.unique_id.split("_", 1)[0]
            if head.isdigit():
                out["serial"] = int(head)
                break
    out["seed"] = modbus_words(hass, entry)
    return out


def modbus_words(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, int]:
    """The raw words the running Modbus entry last read, keyed `table:address`."""
    coordinator = (hass.data.get(MODBUS_DOMAIN) or {}).get(entry.entry_id)
    raw = getattr(getattr(coordinator, "data", None), "raw", None) or {}
    words: dict[str, int] = {}
    for block, block_words in raw.items():
        table, _, start = block.partition(":")
        try:
            Table(table)
            base = int(start)
        except ValueError:
            continue
        for offset, word in enumerate(block_words):
            words[f"{table}:{base + offset}"] = int(word) & 0xFFFF
    return words


def parse_seed(seed: dict[str, int] | None) -> dict[tuple[Table, int], int]:
    out: dict[tuple[Table, int], int] = {}
    for key, word in (seed or {}).items():
        table, _, address = key.partition(":")
        try:
            out[(Table(table), int(address))] = int(word)
        except ValueError:
            continue
    return out


async def async_take_over(
    hass: HomeAssistant, entry: ConfigEntry, old_entry_id: str, serial: int
) -> int:
    """Move the Modbus entry's registry rows to `entry`. Returns rows moved."""
    old = hass.config_entries.async_get_entry(old_entry_id)
    if old is None:
        return 0
    if old.state is ConfigEntryState.LOADED and not (
        await hass.config_entries.async_unload(old_entry_id)
    ):
        _LOGGER.warning(
            "Could not unload %s; its entities stay where they are", old.title
        )
        return 0

    ent_reg = er.async_get(hass)
    prefix = f"{serial}_"
    moved = 0
    for row in list(er.async_entries_for_config_entry(ent_reg, old_entry_id)):
        if not row.unique_id.startswith(prefix):
            continue
        if ent_reg.async_get_entity_id(row.domain, DOMAIN, row.unique_id):
            continue
        # The unique id stays as it is; passing it again would collide with the
        # row's own current one.
        try:
            ent_reg.async_update_entity_platform(
                row.entity_id, DOMAIN, new_config_entry_id=entry.entry_id
            )
        except ValueError as err:
            _LOGGER.warning("Could not take over %s: %s", row.entity_id, err)
            continue
        # Disabling a config entry disables its entities on its behalf; that
        # reason no longer applies to an entity that has moved out of it.
        if row.disabled_by is er.RegistryEntryDisabler.CONFIG_ENTRY:
            ent_reg.async_update_entity(row.entity_id, disabled_by=None)
        moved += 1

    dev_reg = dr.async_get(hass)
    identifiers = {(DOMAIN, str(serial))}
    # The device follows its entities, not the other way round: since Home
    # Assistant 2026.10 a device has a single owning entry, and moving it
    # deletes whatever entities on it the old entry still owns. Before that
    # release a device was shared, and joining it was enough.
    movable = "new_config_entry_id" in inspect.signature(
        dev_reg.async_update_device
    ).parameters
    for device in list(dr.async_entries_for_config_entry(dev_reg, old_entry_id)):
        if movable:
            dev_reg.async_update_device(
                device.id,
                new_config_entry_id=entry.entry_id,
                new_identifiers=identifiers,
            )
        else:
            dev_reg.async_update_device(
                device.id,
                add_config_entry_id=entry.entry_id,
                merge_identifiers=identifiers,
            )
            if device.disabled_by is dr.DeviceEntryDisabler.CONFIG_ENTRY:
                dev_reg.async_update_device(device.id, disabled_by=None)

    _LOGGER.info(
        "Took over %d entities of %s from %s", moved, old.title, MODBUS_DOMAIN
    )
    return moved
