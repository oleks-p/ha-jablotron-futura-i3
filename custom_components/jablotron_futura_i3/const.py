"""Constants for the Jablotron Futura (via i3) integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "jablotron_futura_i3"

#: The integration this one replaces, whose registry rows it can take over.
MODBUS_DOMAIN: Final = "jablotron_futura_modbus"

# Where the controller publishes. The client id is the MQTT client id set in the
# i3 app; it is the second level of every topic.
CONF_CLIENT_ID: Final = "client_id"
CONF_CONTROLLER: Final = "controller"
CONF_SLAVE: Final = "slave"
CONF_CHANNELS: Final = "channels"
CONF_SERIAL: Final = "serial_number"
CONF_MAC: Final = "mac_address"
CONF_WORD_ORDER: Final = "word_order"
CONF_BACKUP: Final = "backup"
CONF_MIGRATE_FROM: Final = "migrate_from"
# The Modbus integration's last readings, for the first start only.
CONF_SEED: Final = "seed"
CONF_HEARTBEAT_TIMEOUT: Final = "heartbeat_timeout"

DEFAULT_CLIENT_ID: Final = "i3"
DEFAULT_SLAVE: Final = 1
DEFAULT_HEARTBEAT_TIMEOUT: Final = 240

# Nothing is polled; the periodic refresh only re-checks the heartbeat.
LIVENESS_INTERVAL: Final = 30
# A polling round arrives as a burst of publishes; decode once per burst.
PUBLISH_DEBOUNCE: Final = 0.5
# The word cache is what survives a restart, written at most this often.
STORE_VERSION: Final = 1
STORE_SAVE_DELAY: Final = 30

# Register map v4.00 requires firmware FUA.P.05.00 or newer.
MIN_FIRMWARE: Final = (5, 0)

MANUFACTURER: Final = "Jablotron"
DEVICE_ID_FUTURA: Final = 39

# The device name, and so the start of every entity id. A second unit is offered
# this with a number after it; see the config flow for why.
DEFAULT_NAME: Final = "Jablotron Futura"

PLATFORMS: Final = [
    "binary_sensor",
    "button",
    "climate",
    "fan",
    "number",
    "select",
    "sensor",
    "switch",
]

# Ventilation levels written to holding register 0.
MODE_OFF: Final = 0
MODE_AUTO: Final = 6
VENTILATION_LEVELS: Final = (1, 2, 3, 4, 5)

# The comfort setpoint the unit accepts, in degrees Celsius. The register itself
# takes a wider span, but anything outside this is rejected by the unit.
TEMP_SETPOINT_MIN: Final = 16.0
TEMP_SETPOINT_MAX: Final = 28.0
TEMP_SETPOINT_STEP: Final = 0.5

# Humidity presets written to holding register 11 (tenths of a percent).
HUMIDITY_PRESETS: Final = {"dry": 250, "comfort": 500, "wet": 750}

# Function timer limits, in seconds, from the register map.
# The app caps boost, circulation and overpressure at two hours, and night and
# party at ten. The register map documents 7200 s for night and 28800 s for
# party, but the unit accepts more than that, so the app's limits are used here.
TIMER_LIMITS: Final = {
    "boost": (0, 7200),
    "circulation": (0, 7200),
    "overpressure": (0, 7200),
    "night": (0, 36000),
    "party": (0, 36000),
}

# fut_t_out reports -99 degrees (raw -990) when the NTC probe is unplugged.
NTC_DISCONNECTED_RAW: Final = -990

SERVICE_SET_AWAY: Final = "set_away"
SERVICE_CLEAR_AWAY: Final = "clear_away"
SERVICE_SET_FUNCTION: Final = "set_function"
SERVICE_SET_EXTERNAL_SENSOR: Final = "set_external_sensor"
SERVICE_UNLOCK_SERVICE: Final = "unlock_service"
SERVICE_WRITE_REGISTER: Final = "write_register"
