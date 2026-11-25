"""A controller boot: its placeholder zeros and failures do not count as readings."""

from __future__ import annotations

import time
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.jablotron_futura_i3 import i3_client
from custom_components.jablotron_futura_i3.registers import Table
from tests.conftest import settle

FAN = "fan.jablotron_futura"
SUPPLY = "sensor.jablotron_futura_supply_air_temperature"


def _client(hass: HomeAssistant, entry):
    return hass.data["jablotron_futura_i3"][entry.entry_id].client


def _placeholders(hass: HomeAssistant, controller, *, failed=()) -> None:
    """What a booting controller publishes before it has read anything."""
    for ch in controller.channels:
        async_fire_mqtt_message(hass, controller.topic(ch), "0")
    for ch in failed:
        async_fire_mqtt_message(hass, controller.topic(ch, "available"), "0")


async def _later(hass: HomeAssistant, monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(i3_client, "_monotonic", lambda: time.monotonic() + seconds)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_placeholders_before_the_heartbeat_are_undone(
    hass: HomeAssistant, setup_integration, controller
):
    """The usual order: zeros first, the heartbeat that explains them after."""
    supply = controller.channel_at(Table.INPUT, 31)
    _placeholders(hass, controller, failed=[supply])
    controller.heartbeat(20)
    await settle(hass)
    assert hass.states.get(FAN).attributes["preset_mode"] == "auto"
    assert hass.states.get(SUPPLY).state == "15.7"
    assert _client(hass, setup_integration).failed_channels() == []
    assert _client(hass, setup_integration).restarts == 1


async def test_placeholders_after_the_heartbeat_are_held_back(
    hass: HomeAssistant, setup_integration, controller
):
    controller.heartbeat(20)
    await settle(hass)
    _placeholders(hass, controller, failed=[controller.channel_at(Table.INPUT, 31)])
    await settle(hass)
    assert hass.states.get(FAN).attributes["preset_mode"] == "auto"
    assert hass.states.get(SUPPLY).state == "15.7"


async def test_a_real_reading_after_the_boot_counts_at_once(
    hass: HomeAssistant, setup_integration, controller
):
    controller.heartbeat(20)
    _placeholders(hass, controller)
    await settle(hass)
    controller.set_word(Table.INPUT, 31, 183)
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "18.3"
    point = controller.channel_at(Table.INPUT, 31).point
    assert point not in _client(hass, setup_integration).booting_channels()


async def test_a_zero_confirmed_by_a_good_read_is_the_reading(
    hass: HomeAssistant, setup_integration, controller
):
    """Off is a real state: once the controller vouches for its 0, it counts."""
    mode = controller.channel_at(Table.HOLDING, 0)
    controller.heartbeat(20)
    _placeholders(hass, controller)
    await settle(hass)
    async_fire_mqtt_message(hass, controller.topic(mode, "available"), "1")
    await settle(hass)
    assert hass.states.get(FAN).attributes["preset_mode"] == "auto"  # not yet
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()
    await settle(hass)
    assert hass.states.get(FAN).state == "off"


async def test_a_value_right_after_the_good_read_wins_over_the_zero(
    hass: HomeAssistant, setup_integration, controller
):
    mode = controller.channel_at(Table.HOLDING, 0)
    controller.heartbeat(20)
    _placeholders(hass, controller)
    async_fire_mqtt_message(hass, controller.topic(mode, "available"), "1")
    controller.set_word(Table.HOLDING, 0, 3)
    await settle(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=10))
    await hass.async_block_till_done()
    await settle(hass)
    state = hass.states.get(FAN)
    assert state.state == "on" and state.attributes["preset_mode"] != "auto"


async def test_what_outlasts_the_channel_period_stands(
    hass: HomeAssistant, setup_integration, controller, monkeypatch
):
    """No other value and no good read in a whole period: the 0 and the failure were real."""
    supply = controller.channel_at(Table.INPUT, 31)
    controller.heartbeat(20)
    _placeholders(hass, controller, failed=[supply])
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "15.7"
    await _later(hass, monkeypatch, i3_client.BOOT_GRACE_DEFAULT + 1)
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "unknown"
    assert hass.states.get(FAN).state == "off"
    assert _client(hass, setup_integration).booting_channels() == []


async def test_a_failure_outside_a_boot_still_counts_at_once(
    hass: HomeAssistant, setup_integration, controller
):
    supply = controller.channel_at(Table.INPUT, 31)
    async_fire_mqtt_message(hass, controller.topic(supply, "available"), "0")
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "unknown"


async def test_a_value_after_a_failure_clears_it(
    hass: HomeAssistant, setup_integration, controller
):
    supply = controller.channel_at(Table.INPUT, 31)
    async_fire_mqtt_message(hass, controller.topic(supply, "available"), "0")
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "unknown"
    controller.set_word(Table.INPUT, 31, 160)
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "16.0"
