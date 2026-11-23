"""A Hydrogen N: 32-bit registers through pairs of 16-bit channels.

The controller assembles a pair in the channel's byte order, holds it as a
32-bit float and publishes the low 16 bits. `hydrogen_value` models that, and
the first test holds the model to every reading taken on the real unit.
"""

from __future__ import annotations

import random

import pytest
from homeassistant.core import HomeAssistant

from custom_components.jablotron_futura_i3.const import CONF_SEED
from custom_components.jablotron_futura_i3.i3_client import (
    FuturaModbusError,
    _swap_bytes,
)
from custom_components.jablotron_futura_i3.registers import Table
from tests.conftest import (
    FakeController,
    full_layout,
    golden_store,
    hydrogen_value,
    make_entry,
    settle,
)

#: (first word, second word) -> {order: what the app showed}, 2026-11-23.
MEASURED = {
    # input 70, the supply valve mask: 59 476 482
    (0x8A02, 0x038B): {"ABCD": 1024, "BADC": 35588, "CDAB": 35328, "DCBA": 768},
    # input 16, the mode bits: device on
    (0x0100, 0x0000): {"ABCD": 0, "BADC": 0, "CDAB": 256, "DCBA": 1},
    # input 6, the hardware revision: 131 238
    (0x00A6, 0x0002): {"ABCD": 2, "BADC": 512, "CDAB": 166, "DCBA": 42496},
}
#: How the app displayed them: under 1000 as is, else truncated to thousands.
SHOWN = {
    (0x8A02, 0x038B): {"ABCD": "1K", "BADC": "35K", "CDAB": "35K", "DCBA": "768"},
    (0x00A6, 0x0002): {"DCBA": "42K"},
}


def _display(value: int) -> str:
    return str(value) if value < 1000 else f"{value // 1000}K"


@pytest.mark.parametrize("pair", list(MEASURED))
def test_the_model_reproduces_every_reading_on_the_unit(pair):
    for order, expected in MEASURED[pair].items():
        got = hydrogen_value(*pair, order)
        assert got == expected, order
        if order in SHOWN.get(pair, {}):
            assert _display(got) == SHOWN[pair][order]


def test_cdab_and_badc_are_exact_for_every_mode_bits_value():
    """Bits 0-22: the high word never reaches 256, so neither read rounds."""
    for value in range(0, 1 << 23, 997):
        first, second = value & 0xFFFF, value >> 16
        assert hydrogen_value(first, second, "CDAB") == first
        assert _swap_bytes(hydrogen_value(first, second, "BADC")) == second


def test_a_wide_register_would_round():
    rng = random.Random(7)
    wrong = 0
    for _ in range(1000):
        first, second = rng.randrange(1 << 16), rng.randrange(256, 1 << 16)
        wrong += hydrogen_value(first, second, "CDAB") != first
    assert wrong > 900


@pytest.fixture
async def hydrogen(hass: HomeAssistant, mqtt_mock, golden):
    channels = full_layout(halves=True)
    entry = make_entry(channels, **{CONF_SEED: golden["store"]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    fake = FakeController(hass, golden_store(golden), channels)
    fake.heartbeat()
    fake.publish_all()
    await settle(hass)
    return entry, fake


async def test_mode_flags_are_live_and_exact(hass: HomeAssistant, hydrogen):
    _, fake = hydrogen
    assert hass.states.get("binary_sensor.jablotron_futura_device_on").state == "on"
    # Standby (bit 20) with the bypass open (bit 11) and the unit on: a value
    # wide enough that ABCD would round it, and CDAB+BADC do not.
    fake.set_word(Table.INPUT, 16, (1 << 8) | (1 << 11) | 1)
    fake.set_word(Table.INPUT, 17, 1 << (20 - 16))
    await settle(hass)
    for flag, want in (
        ("standby", "on"), ("bypass_open", "on"), ("boost_active", "on"),
        ("zone_boost", "off"), ("antifreeze_active", "off"),
    ):
        assert hass.states.get(f"binary_sensor.jablotron_futura_{flag}").state == want, flag


async def test_unreadable_registers_keep_what_was_known(
    hass: HomeAssistant, hydrogen, golden
):
    """Firmware and valve layout come from the seed, exactly, with no channel."""
    entry, _ = hydrogen
    coordinator = hass.data["jablotron_futura_i3"][entry.entry_id]
    assert coordinator.data.get("firmware") == "FUA.P.07.20"
    store = golden_store(golden)
    want = (store[(Table.INPUT, 71)] << 16) | store[(Table.INPUT, 70)]
    assert coordinator.data.get("connected_valve_supply") == want
    assert "input:8" in coordinator.client.kept_static()


async def test_the_away_period_is_not_bridged(hass: HomeAssistant, hydrogen):
    entry, _ = hydrogen
    coordinator = hass.data["jablotron_futura_i3"][entry.entry_id]
    assert "away_begin" not in coordinator.polled_keys
    with pytest.raises(FuturaModbusError):
        await coordinator.client.write_multiple(6, [0, 0])
