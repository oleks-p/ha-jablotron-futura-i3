"""Config and options flow for the Jablotron Futura (via i3) integration.

Nothing here can ask the unit anything: the controller publishes a register
only when it changes, so there may be nothing to hear for hours. Everything the
integration needs comes from two places instead.

* **The i3 app backup.** It is the only record of which channel bridges which
  register, so the first step takes the file and reads the map out of it.
* **The Modbus integration, if it is set up.** Its device carries the unit's
  serial number and MAC, which this integration's unique ids and device need,
  and its last readings are the best starting point there is. Replacing it moves
  its entities here with their ids, areas and settings intact.

Without a Modbus entry to take them from, the serial number is asked for; it is
on the unit's label and in the Jablotron app.
"""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util import slugify

from .const import (
    CONF_BACKUP,
    CONF_CHANNELS,
    CONF_CLIENT_ID,
    CONF_CONTROLLER,
    CONF_HEARTBEAT_TIMEOUT,
    CONF_MAC,
    CONF_MIGRATE_FROM,
    CONF_SEED,
    CONF_SERIAL,
    CONF_SLAVE,
    CONF_WORD_ORDER,
    DEFAULT_CLIENT_ID,
    DEFAULT_HEARTBEAT_TIMEOUT,
    DEFAULT_NAME,
    DEFAULT_SLAVE,
    DOMAIN,
)
from .i3_client import WORD_ORDER_AUTO, WORD_ORDERS, mac_words
from .i3_layout import Channel, LayoutError, channels_from_backup, check_layout
from .migration import modbus_entries, modbus_identity

CONF_REPLACE = "replace_modbus"


def _read_backup(hass: HomeAssistant, file_id: str) -> dict[str, Any]:
    with process_uploaded_file(hass, file_id) as path:
        return json.loads(Path(path).read_text(encoding="utf-8"))


def layout_from(
    backup: dict[str, Any], controller: str, slave: int
) -> tuple[list[Channel], list[str]]:
    """The channel map for one slave, and why anything was left out of it."""
    channels, skipped = channels_from_backup(backup, controller, slave)
    return channels, skipped + check_layout(channels)


def _controllers_with_slave(backup: dict[str, Any], slave: int) -> list[str]:
    from .i3_layout import controllers  # noqa: PLC0415

    found = []
    for unit in controllers(backup):
        try:
            channels, _ = channels_from_backup(backup, unit, slave)
        except LayoutError:
            continue
        if channels:
            found.append(unit)
    return found


class FuturaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._backup: dict[str, Any] | None = None
        self._base: dict[str, Any] = {}
        self._channels: list[Channel] = []
        self._notes: list[str] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                backup = await self.hass.async_add_executor_job(
                    _read_backup, self.hass, user_input[CONF_BACKUP]
                )
                slave = int(user_input.get(CONF_SLAVE, DEFAULT_SLAVE))
                candidates = _controllers_with_slave(backup, slave)
            except (LayoutError, ValueError, OSError):
                errors[CONF_BACKUP] = "invalid_backup"
            else:
                controller = (user_input.get(CONF_CONTROLLER) or "").strip().upper()
                if not controller and len(candidates) == 1:
                    controller = candidates[0]
                if not candidates:
                    errors[CONF_BACKUP] = "no_channels"
                elif controller not in candidates:
                    errors[CONF_CONTROLLER] = "pick_controller"
                    self._notes = [", ".join(candidates)]
                else:
                    channels, notes = layout_from(backup, controller, slave)
                    self._channels, self._notes = channels, notes
                    self._base = {
                        CONF_CLIENT_ID: user_input[CONF_CLIENT_ID].strip(),
                        CONF_CONTROLLER: controller,
                        CONF_SLAVE: slave,
                    }
                    return await self.async_step_unit()

        schema = vol.Schema(
            {
                vol.Required(CONF_BACKUP): selector.FileSelector(
                    selector.FileSelectorConfig(accept=".json,application/json")
                ),
                vol.Required(CONF_CLIENT_ID, default=DEFAULT_CLIENT_ID): cv.string,
                vol.Optional(CONF_CONTROLLER, default=""): cv.string,
                vol.Optional(CONF_SLAVE, default=DEFAULT_SLAVE): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=247)
                ),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"controllers": ", ".join(self._notes)},
        )

    async def async_step_unit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Identity of the unit, and whether to replace the Modbus integration."""
        errors: dict[str, str] = {}
        modbus = modbus_entries(self.hass)
        identity = modbus_identity(self.hass, modbus[0]) if modbus else {}

        if user_input is not None:
            replace = bool(user_input.get(CONF_REPLACE)) and bool(modbus)
            serial_text = str(user_input.get(CONF_SERIAL, "")).strip()
            mac = (user_input.get(CONF_MAC) or "").strip().lower() or None
            name = user_input.get(CONF_NAME, DEFAULT_NAME).strip() or DEFAULT_NAME
            if not serial_text.isdigit():
                errors[CONF_SERIAL] = "invalid_serial"
            if mac:
                try:
                    mac_words(mac)
                except ValueError:
                    errors[CONF_MAC] = "invalid_mac"
            if not replace and self._name_taken(name):
                errors[CONF_NAME] = "name_taken"
            if not errors:
                await self.async_set_unique_id(serial_text)
                self._abort_if_unique_id_configured()
                data: dict[str, Any] = {
                    **self._base,
                    CONF_NAME: name,
                    CONF_SERIAL: int(serial_text),
                    CONF_MAC: mac,
                    CONF_WORD_ORDER: WORD_ORDER_AUTO,
                    CONF_CHANNELS: [ch.as_dict() for ch in self._channels],
                }
                if replace:
                    data[CONF_MIGRATE_FROM] = modbus[0].entry_id
                    if identity.get("seed"):
                        data[CONF_SEED] = identity["seed"]
                return self.async_create_entry(title=name, data=data)

        schema: dict[Any, Any] = {}
        if modbus:
            schema[vol.Required(CONF_REPLACE, default=True)] = cv.boolean
        schema[
            vol.Required(CONF_SERIAL, default=str(identity.get("serial", "")))
        ] = cv.string
        schema[vol.Optional(CONF_MAC, default=identity.get("mac", ""))] = cv.string
        schema[
            vol.Optional(
                CONF_NAME, default=identity.get("name") or self._free_name()
            )
        ] = cv.string
        return self.async_show_form(
            step_id="unit",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "controller": self._base.get(CONF_CONTROLLER, ""),
                "channels": str(len(self._channels)),
                "notes": "\n".join(f"- {n}" for n in self._notes) or "-",
                "modbus": modbus[0].title if modbus else "-",
            },
        )

    def _name_taken(self, name: str) -> bool:
        """Whether a unit already answers to this name, or holds its entity ids.

        Entity ids are built from the device name, so two units under one name
        collide and Home Assistant puts `_2` on the end of the second unit's
        ids. The climate entity is the unit's one id that is the bare name.
        """
        names = {
            entry.data.get(CONF_NAME, DEFAULT_NAME)
            for entry in self._async_current_entries(include_ignore=False)
        }
        if name in names:
            return True
        return er.async_get(self.hass).async_is_registered(f"climate.{slugify(name)}")

    def _free_name(self) -> str:
        names = (DEFAULT_NAME if n == 1 else f"{DEFAULT_NAME} {n}" for n in count(1))
        return next(name for name in names if not self._name_taken(name))

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> FuturaOptionsFlow:
        return FuturaOptionsFlow()


class FuturaOptionsFlow(OptionsFlow):
    """Heartbeat timeout, word order, and a new backup after channels change."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        if user_input is not None:
            data = dict(entry.data)
            if user_input.get(CONF_BACKUP):
                try:
                    backup = await self.hass.async_add_executor_job(
                        _read_backup, self.hass, user_input[CONF_BACKUP]
                    )
                    channels, _ = layout_from(
                        backup, data[CONF_CONTROLLER], int(data[CONF_SLAVE])
                    )
                except (LayoutError, ValueError, OSError):
                    errors[CONF_BACKUP] = "invalid_backup"
                else:
                    if not channels:
                        errors[CONF_BACKUP] = "no_channels"
                    data[CONF_CHANNELS] = [ch.as_dict() for ch in channels]
            data[CONF_WORD_ORDER] = user_input.get(CONF_WORD_ORDER, WORD_ORDER_AUTO)
            if not errors:
                self.hass.config_entries.async_update_entry(entry, data=data)
                return self.async_create_entry(
                    data={
                        CONF_HEARTBEAT_TIMEOUT: int(user_input[CONF_HEARTBEAT_TIMEOUT])
                    }
                )

        schema = vol.Schema(
            {
                vol.Optional(CONF_BACKUP): selector.FileSelector(
                    selector.FileSelectorConfig(accept=".json,application/json")
                ),
                vol.Optional(
                    CONF_HEARTBEAT_TIMEOUT,
                    default=entry.options.get(
                        CONF_HEARTBEAT_TIMEOUT, DEFAULT_HEARTBEAT_TIMEOUT
                    ),
                ): vol.All(vol.Coerce(int), vol.Range(min=180, max=3600)),
                vol.Optional(
                    CONF_WORD_ORDER,
                    default=entry.data.get(CONF_WORD_ORDER, WORD_ORDER_AUTO),
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[WORD_ORDER_AUTO, *WORD_ORDERS],
                        translation_key=CONF_WORD_ORDER,
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "channels": str(len(entry.data.get(CONF_CHANNELS, []))),
                "controller": entry.data.get(CONF_CONTROLLER, ""),
            },
        )
