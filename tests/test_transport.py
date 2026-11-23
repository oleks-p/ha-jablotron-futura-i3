"""The i3 transport: payloads, word order, writes, liveness, persistence."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from custom_components.jablotron_futura_i3.i3_client import (
    WORD_ORDERS,
    FuturaModbusError,
    detect_order_from_firmware,
    detect_order_from_serial,
    format_number,
    join_u32,
    mac_words,
    parse_number,
    split_u32,
)
from custom_components.jablotron_futura_i3.i3_layout import Channel
from custom_components.jablotron_futura_i3.registers import Table
from tests.conftest import CLIENT_ID, CONTROLLER, SERIAL, settle

FAN = "fan.jablotron_futura"


def cmd(point: str) -> str:
    return f"/devices/{CLIENT_ID}/commands/{CONTROLLER}/{point}/set"


class TestPayloads:
    @pytest.mark.parametrize(
        ("payload", "expected"),
        [("410", 410.0), ("23.00", 23.0), (" 7 ", 7.0), (b"12", 12.0),
         ("", None), ("-nan", None), ("nan", None), ("inf", None), ("on", None)],
    )
    def test_parse(self, payload, expected):
        assert parse_number(payload) == expected

    @pytest.mark.parametrize(
        ("value", "text"), [(23, "23"), (23.0, "23"), (23.5, "23.5"), (0.1, "0.1")]
    )
    def test_format_has_no_exponent(self, value, text):
        assert format_number(value) == text

    @pytest.mark.parametrize("order", WORD_ORDERS)
    def test_u32_round_trips_in_every_order(self, order):
        first, second = 0x1234, 0xABCD
        assert split_u32(join_u32(first, second, order), order) == (first, second)

    @pytest.mark.parametrize("order", WORD_ORDERS)
    def test_firmware_marker_finds_the_order(self, order):
        assert detect_order_from_firmware(join_u32(1812, 9999, order)) == order

    @pytest.mark.parametrize("order", WORD_ORDERS)
    def test_serial_finds_the_order(self, order):
        value = join_u32(SERIAL & 0xFFFF, SERIAL >> 16, order)
        assert detect_order_from_serial(value, SERIAL) == order

    def test_mac_words_read_back_as_the_mac(self):
        from custom_components.jablotron_futura_i3.coordinator import decode_mac

        assert decode_mac(mac_words("00:13:b0:09:6f:23")) == "00:13:b0:09:6f:23"


async def test_level_write_is_one_command_on_its_channel(
    hass: HomeAssistant, setup_integration, controller, mqtt_mock
):
    ch = controller.channel_at(Table.HOLDING, 0)
    mqtt_mock.async_publish.reset_mock()
    await hass.services.async_call(
        "fan", "set_preset_mode", {"entity_id": FAN, "preset_mode": "auto"},
        blocking=True,
    )
    await hass.services.async_call(
        "fan", "set_percentage", {"entity_id": FAN, "percentage": 40}, blocking=True
    )
    published = [c.args for c in mqtt_mock.async_publish.call_args_list]
    assert (cmd(ch.point), "6", 0, False) in published
    assert published[-1][0] == cmd(ch.point)
    # Shown at once, before the controller's echo.
    assert hass.states.get(FAN).attributes["percentage"] == 40


async def test_stop_all_is_five_single_commands(
    hass: HomeAssistant, setup_integration, controller, mqtt_mock
):
    mqtt_mock.async_publish.reset_mock()
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.jablotron_futura_stop_all_functions"},
        blocking=True,
    )
    topics = [c.args[0] for c in mqtt_mock.async_publish.call_args_list]
    expected = [cmd(controller.channel_at(Table.HOLDING, a).point) for a in range(1, 6)]
    assert topics == expected


async def test_away_writes_each_u32_in_the_controllers_order(
    hass: HomeAssistant, mqtt_mock, golden
):
    """A controller that builds u32s high word first; writes must match."""
    from tests.conftest import FakeController, full_layout, golden_store, make_entry

    channels = full_layout(halves=False)
    entry = make_entry(channels)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    controller = FakeController(hass, golden_store(golden), channels)
    controller.heartbeat()
    controller.publish_all()
    await settle(hass)
    setup_integration = entry
    mqtt_mock.async_publish.reset_mock()
    await hass.services.async_call(
        "jablotron_futura_i3", "clear_away", {}, blocking=True
    )
    assert [c.args[:4] for c in mqtt_mock.async_publish.call_args_list] == [
        (cmd(controller.channel_at(Table.HOLDING, 6).point), "0", 0, False),
        (cmd(controller.channel_at(Table.HOLDING, 8).point), "0", 0, False),
    ]
    mqtt_mock.async_publish.reset_mock()
    coordinator = hass.data["jablotron_futura_i3"][setup_integration.entry_id]
    await coordinator.client.write_multiple(6, [0x0001, 0x0002])
    value = mqtt_mock.async_publish.call_args.args[1]
    assert int(value) == join_u32(0x0001, 0x0002, "hi_lo") == 0x00010002


async def test_a_scaled_channel_is_divided_back_out(
    hass: HomeAssistant, mqtt_mock, golden
):
    """An existing channel shown in degrees, x0.1, still writes raw tenths."""
    from tests.conftest import FakeController, full_layout, golden_store, make_entry

    channels = [
        ch if not (ch.table is Table.HOLDING and ch.address == 10) else Channel(
            type="mb_float", n=900, table=Table.HOLDING, address=10,
            readable=True, writable=True, multiplier=0.1, name="setpoint",
        )
        for ch in full_layout(halves=False)
    ]
    entry = make_entry(channels)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    fake = FakeController(hass, golden_store(golden), channels)
    fake.heartbeat()
    fake.publish_all()
    await settle(hass)
    climate = hass.states.get("climate.jablotron_futura")
    assert climate.attributes["temperature"] == 24.0

    mqtt_mock.async_publish.reset_mock()
    await hass.services.async_call(
        "climate", "set_temperature",
        {"entity_id": "climate.jablotron_futura", "temperature": 22.5}, blocking=True,
    )
    assert mqtt_mock.async_publish.call_args.args[:2] == (
        f"/devices/{CLIENT_ID}/commands/{CONTROLLER}/mb_float/900/set", "22.5"
    )


async def test_unbridged_write_is_refused(hass: HomeAssistant, setup_integration):
    coordinator = hass.data["jablotron_futura_i3"][setup_integration.entry_id]
    with pytest.raises(FuturaModbusError):
        await coordinator.client.write_single(999, 1234)


async def test_silent_controller_goes_unavailable_and_recovers(
    hass: HomeAssistant, setup_integration, controller
):
    assert hass.states.get(FAN).state != "unavailable"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=300))
    coordinator = hass.data["jablotron_futura_i3"][setup_integration.entry_id]
    # The client measures silence on the monotonic clock.
    coordinator.client._last_beat -= 300  # noqa: SLF001
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=330))
    await hass.async_block_till_done()
    assert hass.states.get(FAN).state == "unavailable"

    controller.heartbeat(2000)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=400))
    await hass.async_block_till_done()
    assert hass.states.get(FAN).state != "unavailable"


async def test_a_failed_read_drops_that_value_only(
    hass: HomeAssistant, setup_integration, controller
):
    sensor = "sensor.jablotron_futura_supply_air_temperature"
    assert hass.states.get(sensor).state == "15.7"
    ch = controller.channel_at(Table.INPUT, 31)
    async_fire_mqtt_message(hass, controller.topic(ch, "available"), "0")
    await settle(hass)
    assert hass.states.get(sensor).state == "unknown"
    assert hass.states.get(FAN).state != "unavailable"
    async_fire_mqtt_message(hass, controller.topic(ch, "available"), "1")
    await settle(hass)
    assert hass.states.get(sensor).state == "15.7"


async def test_every_read_failing_is_the_link_down(
    hass: HomeAssistant, setup_integration, controller
):
    for ch in controller.channels:
        async_fire_mqtt_message(hass, controller.topic(ch, "available"), "0")
    await settle(hass)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=60))
    await hass.async_block_till_done()
    assert hass.states.get(FAN).state == "unavailable"


async def test_nan_and_empty_payloads_keep_the_last_value(
    hass: HomeAssistant, setup_integration, controller
):
    sensor = "sensor.jablotron_futura_supply_air_temperature"
    ch = controller.channel_at(Table.INPUT, 31)
    for payload in ("", "-nan"):
        async_fire_mqtt_message(hass, controller.topic(ch), payload)
    await settle(hass)
    assert hass.states.get(sensor).state == "15.7"


async def test_word_cache_survives_a_restart_with_nothing_republished(
    hass: HomeAssistant, setup_integration, hass_storage
):
    """The controller repeats nothing unchanged; the cache is what remains."""
    entry = setup_integration
    coordinator = hass.data["jablotron_futura_i3"][entry.entry_id]
    await coordinator.async_save_now()
    key = f"jablotron_futura_i3.{entry.entry_id}"
    assert hass_storage[key]["data"]["order"] == "hi_lo"
    assert hass_storage[key]["data"]["words"]["holding:0"] == 6

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await settle(hass)
    assert hass.states.get(FAN).attributes["preset_mode"] == "auto"
    assert hass.states.get("sensor.jablotron_futura_serial_number").state == str(SERIAL)


async def test_a_failed_duplicate_does_not_hide_a_working_channel(
    hass: HomeAssistant, mqtt_mock, golden
):
    """A site may keep a broken mb_u32 beside the mb_u16 that replaced it."""
    from tests.conftest import FakeController, full_layout, golden_store, make_entry

    channels = full_layout(halves=False)
    broken = Channel(
        type="mb_u32", n=1, table=Table.INPUT, address=16, readable=True,
        writable=False, name="old mode bits",
    )
    entry = make_entry([*channels, broken])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    fake = FakeController(hass, golden_store(golden), channels)
    fake.heartbeat()
    fake.publish_all()
    async_fire_mqtt_message(hass, fake.topic(broken, "available"), "0")
    await settle(hass)
    assert hass.states.get("binary_sensor.jablotron_futura_device_on").state == "on"

    good = fake.channel_at(Table.INPUT, 16)
    async_fire_mqtt_message(hass, fake.topic(good, "available"), "0")
    await settle(hass)
    assert hass.states.get("binary_sensor.jablotron_futura_device_on").state == "unknown"


async def test_word_order_from_known_words_when_firmware_stays_quiet(
    hass: HomeAssistant, mqtt_mock, golden
):
    """A controller that never republishes firmware: the seed settles it."""
    from custom_components.jablotron_futura_i3.const import CONF_SEED
    from tests.conftest import FakeController, full_layout, golden_store, make_entry

    channels = full_layout(halves=False)
    entry = make_entry(channels, **{CONF_SEED: golden["store"]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    fake = FakeController(hass, golden_store(golden), channels)
    fake.heartbeat()
    # Only the valve layout publishes; neither firmware nor serial does.
    fake.publish(fake.channel_at(Table.INPUT, 70))
    await settle(hass)
    coordinator = hass.data["jablotron_futura_i3"][entry.entry_id]
    assert coordinator.client.word_order == "hi_lo"

    # From then on a live 32-bit value decodes: the unit goes to standby.
    fake.set_word(Table.INPUT, 17, 1 << (20 - 16))
    await settle(hass)
    assert hass.states.get("binary_sensor.jablotron_futura_standby").state == "on"


