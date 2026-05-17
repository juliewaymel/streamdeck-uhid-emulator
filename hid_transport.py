"""
HIDAPI transport backend for python-elgato-streamdeck.

The library only ships a libusb backend, which cannot see /dev/uhid-created
virtual HID devices (they are not USB devices). This adds an `hidapi`-style
backend that talks to /dev/hidraw* via the `hid` pip package (which wraps
libhidapi-hidraw on Linux).

Usage:
    from hid_transport import HIDAPI, register
    register()                                          # monkey-patch
    from StreamDeck.DeviceManager import DeviceManager
    decks = DeviceManager(transport="hidapi").enumerate()
"""
from __future__ import annotations

import hid

from StreamDeck.Transport.Transport import Transport, TransportError


class HIDAPI(Transport):
    """Talks to HID devices via libhidapi-hidraw (sees uhid devices)."""

    @classmethod
    def probe(cls) -> None:
        # `hid` raises on import if the C library is missing, so reaching
        # this point already means we have a working backend.
        if not hasattr(hid, "enumerate"):
            raise TransportError("hid module missing enumerate()")

    def enumerate(self, vid: int, pid: int):
        out = []
        for info in hid.enumerate():
            if vid is not None and info.get("vendor_id") != vid:
                continue
            if pid is not None and info.get("product_id") != pid:
                continue
            out.append(HIDAPI.Device(info))
        return out

    class Device(Transport.Device):
        def __init__(self, info: dict):
            self._info = info
            self._path: bytes = info["path"]
            self._dev: hid.Device | None = None

        # --- identity -----------------------------------------------------
        def path(self) -> bytes:
            return self._path

        def vendor_id(self) -> int:
            return self._info["vendor_id"]

        def product_id(self) -> int:
            return self._info["product_id"]

        # --- lifecycle ----------------------------------------------------
        def open(self) -> None:
            if self._dev is None:
                self._dev = hid.Device(path=self._path)

        def close(self) -> None:
            if self._dev is not None:
                self._dev.close()
                self._dev = None

        def is_open(self) -> bool:
            return self._dev is not None

        def connected(self) -> bool:
            # hidapi has no liveness ping; treat "open succeeded" as connected.
            try:
                if self._dev is None:
                    return any(d["path"] == self._path for d in hid.enumerate())
                return True
            except Exception:
                return False

        # --- I/O ----------------------------------------------------------
        def write(self, payload: bytes) -> int:
            assert self._dev is not None, "device not open"
            return self._dev.write(bytes(payload))

        def read(self, length: int) -> bytes:
            assert self._dev is not None, "device not open"
            return self._dev.read(length)

        def write_feature(self, payload: bytes) -> int:
            assert self._dev is not None, "device not open"
            return self._dev.send_feature_report(bytes(payload))

        def read_feature(self, report_id: int, length: int) -> bytes:
            assert self._dev is not None, "device not open"
            # libhidapi expects buffer[0] == report_id, then `length` bytes.
            buf = bytes([report_id]) + b"\x00" * length
            return self._dev.get_feature_report(report_id, len(buf))


def register() -> None:
    """Patch the DeviceManager's transports dict so transport='hidapi' works."""
    from StreamDeck.DeviceManager import DeviceManager
    src = DeviceManager._get_transport.__code__
    # Easiest path: shadow _get_transport with a version that knows hidapi.
    original = DeviceManager._get_transport

    @staticmethod
    def _patched(transport):
        if transport == "hidapi":
            HIDAPI.probe()
            return HIDAPI()
        return original(transport)

    DeviceManager._get_transport = _patched
