"""tools/i3_channels.py: a write-only feed keeps feeding."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from custom_components.jablotron_futura_i3.i3_layout import Fitted, channels_from_backup
from custom_components.jablotron_futura_i3.registers import Table
from tests.test_config_flow import backup

_spec = importlib.util.spec_from_file_location(
    "i3_channels", Path(__file__).resolve().parent.parent / "tools" / "i3_channels.py"
)
i3_channels = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(i3_channels)


def test_a_write_only_channel_stays_write_only_beside_a_new_reader():
    """The i3 rewrites a write-only channel every period, which keeps the Futura's
    external sensor slot alive; turned read/write it would write only on change."""
    data = backup()
    i3_channels.plan(data, "A1B2", 1, Fitted(ext_sensors=(1,)), prune=False)
    channels, _ = channels_from_backup(data, "A1B2", 1)
    at_302 = [ch for ch in channels if ch.table is Table.HOLDING and ch.address == 302]
    feeder = next(ch for ch in at_302 if ch.point == "mb_u16/3")
    assert feeder.writable and not feeder.readable
    readers = [ch for ch in at_302 if ch.readable]
    assert len(readers) == 1 and readers[0].point != "mb_u16/3"
    assert readers[0].period == 60
