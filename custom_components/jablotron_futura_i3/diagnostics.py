"""Diagnostics dump: raw register words plus the decoded view, for bug reports."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_CHANNELS, CONF_MAC, CONF_SEED, CONF_SERIAL, DOMAIN
from .coordinator import FuturaCoordinator
from .i3_client import SERIAL_ADDRESS
from .registers import Table

TO_REDACT = {
    CONF_SERIAL,
    CONF_MAC,
    "mac_address",
    "serial_number",
    "user_password",
    "access_code",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: FuturaCoordinator = hass.data[DOMAIN][entry.entry_id]
    client = coordinator.client
    data = coordinator.data
    serial_points = {
        ch.point
        for ch in client.channels
        if ch.table is Table.INPUT and ch.address == SERIAL_ADDRESS
    }
    entry_data = {
        k: v for k, v in entry.data.items() if k not in (CONF_CHANNELS, CONF_SEED)
    }
    return {
        "entry": {
            "data": async_redact_data(entry_data, TO_REDACT),
            "options": dict(entry.options),
        },
        "transport": {
            "controller": client.controller,
            "client_id": client.client_id,
            "word_order": client.word_order,
            "controller_restarts": client.restarts,
            "problem": client.problem(),
            "failed_channels": client.failed_channels(),
            "booting_channels": client.booting_channels(),
            "kept_static": client.kept_static(),
            # Raw, before any word order: what to look at when it is not learned.
            # The serial number's channel is left out, like the serial itself.
            "u32_published": {
                point: value
                for point, value in client.u32_values().items()
                if point not in serial_points
            },
        },
        "channels": [
            f"{ch.point} -> {ch.table.value} {ch.address}"
            f"{'' if ch.words == 1 else '-' + str(ch.address + 1)}"
            f" ({'rw' if ch.readable and ch.writable else 'r' if ch.readable else 'w'}"
            f"{'' if ch.multiplier == 1 else f' x{ch.multiplier:g}'}) {ch.name}"
            for ch in sorted(client.channels, key=lambda c: (c.table.value, c.address))
        ],
        "bridged": sorted(coordinator.polled_keys),
        "values": async_redact_data(dict(data.values), TO_REDACT),
        "flags": dict(data.flags),
        "present": sorted(data.present),
        "topology": data.topology,
        # Raw words reproduce a decoding problem without access to the unit.
        "raw_registers": {
            key: words
            for key, words in sorted(data.raw.items())
            if key not in ("input:1", "input:3")
        },
    }
