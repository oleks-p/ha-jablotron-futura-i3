# Jablotron Futura — Home Assistant integration (via an i3 controller)

Monitoring and control of a **Jablotron Futura** heat-recovery ventilation unit
whose Modbus link is mastered by an **i3** building controller. Home Assistant
never talks to the unit: the controller polls the registers it has channels for
and publishes them over MQTT, and this integration decodes them.

It is a fork of
[`ha-jablotron-futura-modbus`](https://github.com/oleks-p/ha-jablotron-futura-modbus)
with the transport replaced and nothing else. The register map, every entity,
the cards, the dashboards and the translations are that integration's, so it
produces **the same entities, with the same ids, states and attributes** —
`tests/test_equivalence.py` holds it to exactly that, against output recorded
from the Modbus integration.

## Why

A Futura accepts one Modbus TCP connection and drops the older one when a second
arrives. Where an i3 controller already reads and writes the unit — feeding it
room sensors, say — a Home Assistant Modbus integration on the same unit means
two masters evicting each other. This integration reads through the controller
instead, so the unit keeps a single master.

## How it works

```
Futura  ──Modbus──  i3 controller  ──MQTT──  Home Assistant
                     (one channel           /devices/<client>/events/<DEV>/<type>/<n>/value
                      per register)         /devices/<client>/commands/<DEV>/<type>/<n>/set
```

- **One channel per register.** Input registers use register type 1 (read),
  holding registers type 8 (read, and write on command).
- **32-bit registers need care.** The unit refuses a read that splits one, so a
  channel must read both words in one request: an `mb_u16` channel with data
  type 3 (uint32) does. But a Hydrogen N then holds the assembled value as a
  32-bit float and publishes only its low 16 bits, so each such channel delivers
  one word, chosen by its byte order, and only while the value fits 24 bits.
  Two channels per register are exact whenever the high word stays below 256:
  CDAB for the low word, BADC for the (byte-swapped) high word. That covers the
  mode, error and warning bitfields, the wall sensor mask and the hardware
  revision. The firmware and the valve masks are wider and do not change on a
  running unit: they keep the value last known — taken from the Modbus
  integration when it is replaced. The away times are not bridged.
  `tests/test_hydrogen.py` models the controller and holds the model to twelve
  readings taken on a real unit.
- **The map comes from the i3 app backup.** Setup takes the backup file and
  reads which channel carries which register from it. Nothing has to be typed.
- **The controller publishes only on change, and retains nothing.** A register
  that never moves — the serial number, a setpoint nobody touches — is published
  once after the controller boots. So the integration keeps every register word
  it has seen in Home Assistant's storage, and a restart shows the last known
  values rather than `unknown`.
- **On a controller that publishes 32-bit values whole**, the word order is
  learned: the Futura's firmware register carries a constant 9999 in its high
  word, and the registers that never change are compared with their last known
  values.
- **Liveness** comes from the controller's uptime heartbeat (about once a
  minute). A silent controller, or one whose every read of the unit fails, takes
  the entities unavailable. A single failed read leaves only that value unknown.
- **Writes** go to the channel's `set` topic and show at once; the controller's
  echo confirms them about a second later.

## Setting up

### 1. Give the controller the channels

[`tools/i3_channels.py`](tools/i3_channels.py) edits a backup exported from the
i3 app. It reuses any channel that already bridges a needed register, turns a
write-only one into read/write, adds the rest, and with `--prune` removes the
unit's channels nothing needs — never one an automation refers to.

```bash
python3 tools/i3_channels.py backup.json --controller A1B2 --slave 1 \
    --ext-sensors 1,2,4,8 --prune --out backup-futura.json
```

Pass the slots that are in use (`--ext-sensors`, `--ext-buttons`, `--wall-ui`,
`--wall-sensors`, `--alfa`); only those get per-slot channels. The tool prints
every channel it keeps, changes, adds or removes, and the polling load it adds.
Import the result through the i3 app, then export a fresh backup.

What is bridged by default: everything the Modbus integration polled, except
the MAC address (three registers; an i3 channel reads at most two — it comes
from configuration instead), the Modbus statistics counters, the UIN voltages,
the digital inputs, the identify bitfield and the service registers. All of
those back entities that are disabled by default.

### 2. Add the integration

Requires the MQTT integration, connected to the broker the controller publishes
to.

1. HACS → Custom repositories → add this repository (category Integration),
   install **Jablotron Futura (via i3)**, restart.
2. Settings → Devices & services → Add integration → **Jablotron Futura (via i3)**.
3. Upload the fresh backup, enter the controller's MQTT client id (the second
   topic level), and the controller id if more than one has channels for the
   unit.

### Replacing the Modbus integration

If `jablotron_futura_modbus` is set up, the second step offers to replace it,
and fills in the unit's serial number, MAC and name from its device. Replacing:

- moves every one of its entities here **in the registry**, so entity ids,
  areas, custom names and icons, and disabled and hidden flags all survive, and
  so does the device with its area;
- takes its last readings as the starting point, so values the controller will
  not republish until they change are there from the first second;
- moves the card resource to this integration's copy of the same bundle;
- then removes the Modbus entry.

Dashboards keep working unchanged. Automations that call the Modbus
integration's services (`jablotron_futura_modbus.set_function` and so on) need
the domain changed to `jablotron_futura_i3`; the bundled control card already
calls the new one.

## Services

Every service takes an optional `device_id`, needed only with more than one unit.

| Service | Purpose |
| --- | --- |
| `set_away` / `clear_away` | Set or clear the holiday period, as local wall-clock time. |
| `set_function` | Start boost, circulation, overpressure, night or party for a number of seconds. |
| `set_external_sensor` | Write a reading into one of the unit's external sensor slots. Needs channels for that slot. |
| `unlock_service` | Not available: the service registers have no channel. |
| `write_register` | Write a holding register that has a channel but no entity. |

## Options

- **Heartbeat timeout** (default 240 s, minimum 180: heartbeats are round-robin
  and anything lower flaps).
- **32-bit word order** — automatic by default.
- **New backup** — re-reads the channel map after channels change in the app.

## Dashboard, cards and languages

Exactly as in the Modbus integration: copy
[`dashboards/futura.yaml`](dashboards/futura.yaml) (or `futura.uk.yaml`), and the
eight cards register themselves. English and Ukrainian ship. See that
repository's README for the card reference and the design tokens.

## Known limitations

- **The controller's channel limit is not documented.** A unit with four external
  sensor slots in use needs about 86 channels on one controller. Check that the
  app accepts them before relying on it.
- **Bus load.** Each channel is one request per period: the defaults (10 s for
  the level and mode bits, 20 s for the function timers, 30 s for
  measurements, 60 s for settings, 600 s for identity and fitted peripherals)
  come to about two requests a second.
- **The away period is not shown or settable** through a Hydrogen N: its
  timestamps are too wide for the float the controller rounds through.
- **One 32-bit word order per controller.** It is learned from the first
  32-bit channel that publishes the firmware or serial register, and applied
  to all of them. That needs the controller to publish those channels at least
  once with this integration listening: after adding them, or after a
  controller restart.
- **A value that changes while Home Assistant is down is missed** until it
  changes again, because the controller does not repeat it. Countdown timers and
  measurements move constantly and recover within a period; a setting changed
  from the i3 app during a restart shows its old value until the next change or
  controller restart.
- **The away period does not arm from a write** — a limitation of the unit, the
  same as over Modbus TCP.
- Everything in the Modbus integration's list of unit limitations applies.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/pytest -q

npm --prefix cards install
npm --prefix cards run typecheck
npm --prefix cards run build             # rebuilds the committed bundle
```

`tests/fixtures/modbus_golden.json` is the Modbus integration's output against
its model of a reference unit: the register words, and every entity's registry
fields, state and attributes. The tests publish the same words as a controller
would and require the same result.

## Credits

The register map and everything above the transport are
[`ha-jablotron-futura-modbus`](https://github.com/oleks-p/ha-jablotron-futura-modbus),
which credits the community work it builds on. Not affiliated with or endorsed by
Jablotron or i3.

## License

MIT — see [LICENSE](LICENSE).
