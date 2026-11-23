"""Setting up from an i3 app backup, with and without a Modbus entry to replace."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.jablotron_futura_i3.const import (
    CONF_CHANNELS,
    CONF_MIGRATE_FROM,
    CONF_SEED,
    DOMAIN,
    MODBUS_DOMAIN,
)
from custom_components.jablotron_futura_i3.i3_layout import (
    Channel,
    Fitted,
    channels_from_backup,
    check_layout,
    wanted_registers,
)
from custom_components.jablotron_futura_i3.registers import Table

FILE_ID = "0123456789abcdef0123456789abcdef"


def _channel(ctype: str, n: int, dev: int, addr: int, rtype: int, mult: float = 1.0):
    return {
        "config": {
            "dev_id": dev, "start_addr": addr, "register_type": rtype,
            "period": 30000, "byte_swap": 5, "data_type": 1, "multiplier": mult,
            "bias": 0, "id": n, "name": f"ch{n}", "entity_id": 201,
            "groups": "0x0000000000000000", "flags": 0, "timeout": 0,
            "metadata": {"register_modes": []},
        },
        "metadata": {"register_modes": []},
    }


def backup() -> dict:
    """Two controllers; only one bridges slave 1, and one channel is a coil."""
    return {
        "version": "v1",
        "metadata": {"version": "v1"},
        "configurations": {
            "version": "v1",
            "unitBackup": [
                {
                    "unitId": "A1B2", "unitModel": "Hydrogen N", "unitName": "Hydrogen N",
                    "config": {
                        "mb_u16": [
                            _channel("mb_u16", 1, 1, 0, 8),
                            _channel("mb_u16", 2, 1, 31, 1),
                            _channel("mb_u16", 3, 1, 302, 7),
                            _channel("mb_u16", 4, 1, 5, 11),   # a coil: not ours
                            _channel("mb_u16", 5, 2, 1, 1),    # another slave
                        ],
                        "mb_u32": [_channel("mb_u32", 1, 1, 16, 1)],
                        "mb_float": [_channel("mb_float", 1, 1, 10, 8, 0.1)],
                    },
                    "automations": [],
                },
                {"unitId": "C3D4", "unitModel": "Ferrum 3", "config": {}, "automations": []},
            ],
        },
    }


class TestLayout:
    def test_reads_the_slaves_channels(self):
        channels, skipped = channels_from_backup(backup(), "a1b2", 1)
        by_point = {ch.point: ch for ch in channels}
        assert set(by_point) == {"mb_u16/1", "mb_u16/2", "mb_u16/3", "mb_u32/1", "mb_float/1"}
        assert by_point["mb_u16/1"].table is Table.HOLDING and by_point["mb_u16/1"].readable
        assert by_point["mb_u16/2"].table is Table.INPUT and not by_point["mb_u16/2"].writable
        assert not by_point["mb_u16/3"].readable and by_point["mb_u16/3"].writable
        assert by_point["mb_float/1"].multiplier == 0.1
        assert skipped == ["mb_u16/4 'ch4': register type 11"]

    def test_half_a_u32_is_a_problem(self):
        bad = [
            Channel("mb_u16", 1, Table.INPUT, 16, True, False),
            Channel("mb_u16", 2, Table.INPUT, 17, True, False),
        ]
        problems = check_layout(bad)
        assert any("half of mode_bits" in p for p in problems)
        assert any("starts inside mode_bits" in p for p in problems)

    def test_fitted_slots_decide_the_slot_registers(self):
        keys = {reg.key for reg in wanted_registers(Fitted(ext_sensors=(1, 2, 4, 8)))}
        assert "ext_sensor_4_co2" in keys and "ext_sensor_3_co2" not in keys
        assert "ext_sensor_3_present" in keys      # every flag, by default
        assert "alfa_1_co2" not in keys
        assert "mac_address" not in keys
        assert not any(k.endswith("_temp_correction") for k in keys)
        lean = {
            reg.key
            for reg in wanted_registers(
                Fitted(ext_sensors=(1,), all_slot_flags=False)
            )
        }
        assert "ext_sensor_3_present" not in lean and "ext_sensor_1_present" in lean


@contextlib.contextmanager
def _uploaded(tmp_path: Path, content: dict):
    path = tmp_path / "backup.json"
    path.write_text(json.dumps(content))

    @contextlib.contextmanager
    def fake(hass, file_id):
        yield path

    with patch(
        "custom_components.jablotron_futura_i3.config_flow.process_uploaded_file", fake
    ):
        yield


@pytest.fixture(autouse=True)
def _no_setup():
    with patch(
        "custom_components.jablotron_futura_i3.async_setup_entry", return_value=True
    ):
        yield


async def test_fresh_install_asks_for_the_serial(hass: HomeAssistant, tmp_path):
    with _uploaded(tmp_path, backup()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"backup": FILE_ID, "client_id": "i3", "slave": 1}
        )
    assert result["step_id"] == "unit"
    assert "replace_modbus" not in result["data_schema"].schema
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"serial_number": "not a number"}
    )
    assert result["errors"] == {"serial_number": "invalid_serial"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"serial_number": "4200000001", "mac_address": ""}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    data = result["data"]
    assert data["controller"] == "A1B2"          # the only one with slave 1
    assert data["client_id"] == "i3"
    assert data["serial_number"] == 4200000001
    assert len(data[CONF_CHANNELS]) == 5
    assert CONF_MIGRATE_FROM not in data


async def test_not_a_backup(hass: HomeAssistant, tmp_path):
    with _uploaded(tmp_path, {"hello": "world"}):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"backup": FILE_ID, "client_id": "i3"}
        )
    assert result["errors"] == {"backup": "invalid_backup"}


async def test_wrong_slave_has_no_channels(hass: HomeAssistant, tmp_path):
    with _uploaded(tmp_path, backup()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"backup": FILE_ID, "client_id": "i3", "slave": 9}
        )
    assert result["errors"] == {"backup": "no_channels"}


async def test_replacing_modbus_takes_its_identity(hass: HomeAssistant, tmp_path):
    old = MockConfigEntry(
        domain=MODBUS_DOMAIN, title="Futura", data={"host": "192.0.2.1", "name": "Futura"}
    )
    old.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=old.entry_id,
        identifiers={(MODBUS_DOMAIN, "4200000002")},
        connections={(dr.CONNECTION_NETWORK_MAC, "00:13:b0:00:00:02")},
        name="Futura",
    )
    with _uploaded(tmp_path, backup()):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"backup": FILE_ID, "client_id": "i3", "controller": "a1b2"}
        )
    assert result["step_id"] == "unit"
    defaults = {
        str(key): key.default() for key in result["data_schema"].schema
        if hasattr(key, "default") and callable(key.default)
    }
    assert defaults["serial_number"] == "4200000002"
    assert defaults["mac_address"] == "00:13:b0:00:00:02"
    assert defaults["name"] == "Futura"
    assert defaults["replace_modbus"] is True

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "replace_modbus": True,
            "serial_number": "4200000002",
            "mac_address": "00:13:b0:00:00:02",
            "name": "Futura",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MIGRATE_FROM] == old.entry_id
    # Not running in this test, so there are no last readings to carry over.
    assert CONF_SEED not in result["data"]
