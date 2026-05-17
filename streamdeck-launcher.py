#!/usr/bin/env python3
"""
Wrapper that starts streamdeck-ui against our /dev/uhid virtual deck.

streamdeck-ui hardcodes `DeviceManager.DeviceManager().enumerate()` with
the default libusb transport, which can't see uhid-virtual devices.
Before importing the GUI we:
  1. register the hidapi transport from hid_transport.py
  2. monkey-patch DeviceManager.__init__ so any call without an explicit
     transport falls back to "hidapi" instead of "libusb"

Run inside an X session (or xvfb-run / DISPLAY=:N):

    DISPLAY=:99 sudo python3 streamdeck-launcher.py
"""
import os
import sys

# Make hid_transport.py importable wherever this launcher is dropped.
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import hid_transport
hid_transport.register()

# Patch DeviceManager so libusb is not the implicit default any more.
from StreamDeck.DeviceManager import DeviceManager

_original_init = DeviceManager.__init__


def _patched_init(self, transport=None):
    if transport is None:
        transport = "hidapi"
    return _original_init(self, transport)


DeviceManager.__init__ = _patched_init

# Hand off to streamdeck-ui's normal entrypoint.
from streamdeck_ui.gui import start  # noqa: E402

if __name__ == "__main__":
    sys.exit(start())
