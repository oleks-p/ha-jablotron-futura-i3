"""Boot placeholders on channels with known periods, timed as a real controller.

Measured on a Hydrogen N: after a boot its first real read of a channel comes
about one period later plus 40-90 s (48 s for a 10 s channel, 690 s for a 600 s
one). Holding placeholders for one period plus a short margin let them through.
"""

from __future__ import annotations

import dataclasses

import pytest
from homeassistant.core import HomeAssistant

from custom_components.jablotron_futura_i3 import i3_client
from custom_components.jablotron_futura_i3.registers import Table
from tests.conftest import full_layout, settle
from tests.test_boot import FAN, SUPPLY, _client, _later, _placeholders

PERIOD = 10


@pytest.fixture
def channels():
    return [dataclasses.replace(ch, period=PERIOD) for ch in full_layout()]


async def test_placeholders_outlast_one_period_and_the_first_read(
    hass: HomeAssistant, setup_integration, controller, monkeypatch
):
    supply = controller.channel_at(Table.INPUT, 31)
    controller.heartbeat(20)
    _placeholders(hass, controller, failed=[supply])
    await settle(hass)
    # Past one period plus 30 s, before the first read: still the last known.
    await _later(hass, monkeypatch, PERIOD + 38)
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "15.7"
    assert hass.states.get(FAN).attributes["preset_mode"] == "auto"
    assert supply.point in _client(hass, setup_integration).booting_channels()


async def test_the_grace_still_ends(
    hass: HomeAssistant, setup_integration, controller, monkeypatch
):
    supply = controller.channel_at(Table.INPUT, 31)
    controller.heartbeat(20)
    _placeholders(hass, controller, failed=[supply])
    await settle(hass)
    await _later(hass, monkeypatch, i3_client.boot_grace(PERIOD) + 1)
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "unknown"
    assert hass.states.get(FAN).state == "off"


async def test_a_value_after_the_grace_clears_the_failure(
    hass: HomeAssistant, setup_integration, controller, monkeypatch
):
    """The controller may never repeat `available`; its values say enough.

    Seen on a Hydrogen N: a boot's failure outlived the grace and the mode
    flags stayed unknown for 3.5 hours while their new values kept arriving.
    """
    supply = controller.channel_at(Table.INPUT, 31)
    controller.heartbeat(20)
    _placeholders(hass, controller, failed=[supply])
    await settle(hass)
    await _later(hass, monkeypatch, i3_client.boot_grace(PERIOD) + 1)
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "unknown"
    controller.set_word(Table.INPUT, 31, 183)
    await settle(hass)
    assert hass.states.get(SUPPLY).state == "18.3"
    assert _client(hass, setup_integration).failed_channels() == []
