"""Fed the same registers, this integration produces what the Modbus one did.

The golden file is the Modbus integration's own output against its model of the
reference unit: every entity, its registry fields, state and attributes. If the
two ever disagree, a dashboard built on one breaks on the other.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from tests.conftest import settle


def _actual(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    reg = er.async_get(hass)
    out: dict[str, dict[str, Any]] = {}
    for e in reg.entities.values():
        st = hass.states.get(e.entity_id)
        out[e.entity_id] = {
            "entity_id": e.entity_id,
            "unique_id": e.unique_id,
            "disabled": bool(e.disabled_by),
            "entity_category": e.entity_category,
            "original_name": e.original_name,
            "translation_key": e.translation_key,
            "device_class": e.device_class or e.original_device_class,
            "unit": e.unit_of_measurement,
            "icon": e.original_icon,
            "state": st.state if st else None,
            "attributes": dict(st.attributes) if st else {},
        }
    return out


def _normalise(row: dict[str, Any]) -> dict[str, Any]:
    """Compare as the golden file stores it: JSON-shaped, strings for objects."""
    import json  # noqa: PLC0415

    return json.loads(json.dumps(row, sort_keys=True, default=str))


async def test_same_entities(hass: HomeAssistant, setup_integration, golden):
    actual = _actual(hass)
    expected = {row["entity_id"]: row for row in golden["entities"]}
    assert sorted(actual) == sorted(expected)


async def test_same_registry_fields_and_states(
    hass: HomeAssistant, setup_integration, golden
):
    actual = _actual(hass)
    mismatches = []
    for row in golden["entities"]:
        mine = _normalise(actual.get(row["entity_id"], {}))
        theirs = _normalise(row)
        for field in theirs:
            if mine.get(field) != theirs[field]:
                mismatches.append(
                    f"{row['entity_id']}.{field}: {mine.get(field)!r} != {theirs[field]!r}"
                )
    assert not mismatches, "\n".join(mismatches[:40])


async def test_same_device(hass: HomeAssistant, setup_integration, golden):
    entry = setup_integration
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    device = devices[0]
    want = golden["devices"][0]
    assert device.name == want["name"]
    assert device.model == want["model"]
    assert device.sw_version == want["sw_version"]
    assert device.hw_version == want["hw_version"]
    assert device.serial_number == want["serial_number"]
    assert device.manufacturer == want["manufacturer"]
    assert sorted(map(list, device.connections)) == want["connections"]


async def test_restart_restores_what_the_controller_does_not_repeat(
    hass: HomeAssistant, setup_integration, golden
):
    """After a restart the controller publishes nothing that has not changed."""
    entry = setup_integration
    before = {k: v["state"] for k, v in _actual(hass).items()}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await settle(hass)
    after = {k: v["state"] for k, v in _actual(hass).items()}
    assert after == before
