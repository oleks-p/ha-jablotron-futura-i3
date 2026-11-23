"""Replacing the Modbus integration in place."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jablotron_futura_i3.const import (
    CONF_MIGRATE_FROM,
    CONF_SEED,
    DOMAIN,
    MODBUS_DOMAIN,
)
from tests.conftest import MAC, SERIAL, full_layout, make_entry, settle

SUPPLY = "sensor.jablotron_futura_supply_air_temperature"
FAN = "fan.jablotron_futura"


def _modbus_install(hass: HomeAssistant, golden) -> tuple[MockConfigEntry, str]:
    """A Modbus entry with the registry rows the real one leaves behind."""
    old = MockConfigEntry(
        domain=MODBUS_DOMAIN,
        title="Jablotron Futura",
        data={"host": "192.0.2.10", "port": 502, "unit_id": 1, "name": "Jablotron Futura"},
    )
    old.add_to_hass(hass)
    area = ar.async_get(hass).async_create("Plant room")
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get_or_create(
        config_entry_id=old.entry_id,
        identifiers={(MODBUS_DOMAIN, str(SERIAL))},
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        manufacturer="Jablotron",
        name="Jablotron Futura",
    )
    ent_reg = er.async_get(hass)
    for row in golden["entities"]:
        domain, object_id = row["entity_id"].split(".", 1)
        ent_reg.async_get_or_create(
            domain,
            MODBUS_DOMAIN,
            row["unique_id"],
            suggested_object_id=object_id,
            config_entry=old,
            device_id=device.id,
            disabled_by=er.RegistryEntryDisabler.INTEGRATION if row["disabled"] else None,
        )
    # After the rows: an area on the device would otherwise prefix their ids.
    dev_reg.async_update_device(device.id, area_id=area.id)
    ent_reg.async_update_entity(SUPPLY, area_id=area.id, name="Supply (mine)")
    ent_reg.async_update_entity(
        "sensor.jablotron_futura_filter_clogging",
        hidden_by=er.RegistryEntryHider.USER,
    )
    return old, device.id


async def test_takeover_keeps_every_row_and_the_device(
    hass: HomeAssistant, mqtt_mock, golden
):
    old, device_id = _modbus_install(hass, golden)
    before = {
        e.entity_id: (e.unique_id, e.disabled, e.area_id, e.name, e.hidden_by)
        for e in er.async_get(hass).entities.values()
    }
    entry = make_entry(
        full_layout(),
        **{CONF_MIGRATE_FROM: old.entry_id, CONF_SEED: golden["store"]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)

    ent_reg = er.async_get(hass)
    after = {
        e.entity_id: (e.unique_id, e.disabled, e.area_id, e.name, e.hidden_by)
        for e in ent_reg.entities.values()
    }
    assert after == before
    assert {e.platform for e in ent_reg.entities.values()} == {DOMAIN}
    assert {e.config_entry_id for e in ent_reg.entities.values()} == {entry.entry_id}

    device = dr.async_get(hass).async_get(device_id)
    assert device is not None, "the device must survive with its id"
    assert device.config_entry_id == entry.entry_id
    assert device.area_id is not None
    assert (DOMAIN, str(SERIAL)) in device.identifiers

    # The Modbus entry is gone, and the one-shot data with it.
    assert hass.config_entries.async_get_entry(old.entry_id) is None
    assert CONF_MIGRATE_FROM not in entry.data
    assert CONF_SEED not in entry.data


async def test_takeover_shows_the_last_modbus_readings_before_any_publish(
    hass: HomeAssistant, mqtt_mock, golden
):
    """The controller may not publish a setpoint for hours; the seed covers it."""
    old, _ = _modbus_install(hass, golden)
    entry = make_entry(
        full_layout(),
        **{CONF_MIGRATE_FROM: old.entry_id, CONF_SEED: golden["store"]},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "15.7"
    assert hass.states.get(FAN).attributes["preset_mode"] == "auto"
    assert hass.states.get("climate.jablotron_futura").attributes["temperature"] == 24.0
