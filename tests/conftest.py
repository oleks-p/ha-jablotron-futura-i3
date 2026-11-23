"""Fixtures: a fake i3 controller publishing the reference Futura's registers.

`fixtures/modbus_golden.json` was recorded from the Modbus integration this one
is forked from, set up against its own model of the reference unit. It holds
that model's register words, every entity the integration produced from them,
and each entity's state and attributes. The fake controller here publishes the
same words, so a test can hold this integration to producing exactly the same.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.jablotron_futura_i3.const import (
    CONF_CHANNELS,
    CONF_CLIENT_ID,
    CONF_CONTROLLER,
    CONF_MAC,
    CONF_SERIAL,
    CONF_SLAVE,
    CONF_WORD_ORDER,
    DOMAIN,
)
from custom_components.jablotron_futura_i3.i3_client import join_u32
from custom_components.jablotron_futura_i3.i3_layout import Channel
from custom_components.jablotron_futura_i3.registers import REGISTERS, Kind, Table

GOLDEN = Path(__file__).parent / "fixtures" / "modbus_golden.json"
CLIENT_ID = "i3"
CONTROLLER = "A1B2"
SERIAL = 4200000000
MAC = "00:13:b0:00:00:01"
#: Not the Futura's own order, so every test also exercises the detection.
CONTROLLER_ORDER = "hi_lo"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text())


def golden_store(golden: dict[str, Any]) -> dict[tuple[Table, int], int]:
    out: dict[tuple[Table, int], int] = {}
    for key, word in golden["store"].items():
        table, _, address = key.partition(":")
        out[(Table(table), int(address))] = int(word)
    return out


def full_layout() -> list[Channel]:
    """A channel for every register the Modbus integration polled by default.

    That is everything but the temperature corrections (an opt-in there) and
    the MAC, which no i3 channel can read.
    """
    channels: list[Channel] = []
    for n, reg in enumerate(
        (r for r in REGISTERS
         if r.kind is not Kind.MAC and not r.key.endswith("_temp_correction")),
        start=1,
    ):
        # The form a Hydrogen N reads: every register an mb_u16 channel, a
        # 32-bit one with the u32 data type, spanning both words.
        channels.append(
            Channel(
                type="mb_u16",
                n=n,
                table=reg.table,
                address=reg.address,
                readable=True,
                writable=reg.table is Table.HOLDING,
                name=reg.key,
                span=reg.words,
            )
        )
    return channels


class FakeController:
    """Publishes register words the way an i3 controller does."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: dict[tuple[Table, int], int],
        channels: list[Channel],
        order: str = CONTROLLER_ORDER,
    ) -> None:
        self.hass = hass
        self.store = dict(store)
        self.channels = channels
        self.order = order

    def topic(self, ch: Channel, leaf: str = "value") -> str:
        return f"/devices/{CLIENT_ID}/events/{CONTROLLER}/{ch.point}/{leaf}"

    def value_of(self, ch: Channel) -> float:
        first = self.store.get((ch.table, ch.address), 0)
        if ch.words == 2:
            second = self.store.get((ch.table, ch.address + 1), 0)
            return join_u32(first, second, self.order)
        return first * ch.multiplier

    def publish(self, ch: Channel) -> None:
        value = self.value_of(ch)
        payload = str(int(value)) if float(value).is_integer() else f"{value:.2f}"
        async_fire_mqtt_message(self.hass, self.topic(ch), payload)

    def publish_all(self) -> None:
        # The firmware register first is not required, but a real controller
        # reads in channel order, and u32 channels arrive before the order is
        # known either way.
        for ch in self.channels:
            self.publish(ch)

    def heartbeat(self, uptime: int = 1000) -> None:
        async_fire_mqtt_message(
            self.hass, f"/devices/{CLIENT_ID}/events/{CONTROLLER}/system/1/uptime",
            str(uptime),
        )

    def set_word(self, table: Table, address: int, word: int) -> None:
        self.store[(table, address)] = word & 0xFFFF
        for ch in self.channels:
            if ch.table is table and ch.address <= address < ch.address + ch.words:
                self.publish(ch)
                return

    def channel_at(self, table: Table, address: int) -> Channel:
        return next(
            ch for ch in self.channels if ch.table is table and ch.address == address
        )


def make_entry(channels: list[Channel], **extra: Any) -> MockConfigEntry:
    data = {
        CONF_CLIENT_ID: CLIENT_ID,
        CONF_CONTROLLER: CONTROLLER,
        CONF_SLAVE: 1,
        CONF_SERIAL: SERIAL,
        CONF_MAC: MAC,
        CONF_WORD_ORDER: "auto",
        CONF_CHANNELS: [ch.as_dict() for ch in channels],
        "name": "Jablotron Futura",
    }
    data.update(extra)
    return MockConfigEntry(
        domain=DOMAIN, title="Jablotron Futura", unique_id=str(SERIAL), data=data
    )


@pytest.fixture
def channels() -> list[Channel]:
    return full_layout()


@pytest.fixture
def config_entry(channels) -> MockConfigEntry:
    return make_entry(channels)


@pytest.fixture
async def controller(hass: HomeAssistant, mqtt_mock, golden, channels) -> FakeController:
    return FakeController(hass, golden_store(golden), channels)


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant, config_entry: MockConfigEntry, controller: FakeController
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    controller.heartbeat()
    controller.publish_all()
    await settle(hass)
    return config_entry


async def settle(hass: HomeAssistant) -> None:
    """Let the publish debounce run out and the entities catch up."""
    from datetime import timedelta  # noqa: PLC0415

    from homeassistant.util import dt as dt_util  # noqa: PLC0415
    from pytest_homeassistant_custom_component.common import (  # noqa: PLC0415
        async_fire_time_changed,
    )

    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1))
    await hass.async_block_till_done()
