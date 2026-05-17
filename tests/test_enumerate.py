#!/usr/bin/env python3
"""
Phase 2 sanity check: does python-elgato-streamdeck see the virtual device?

Run uhid_streamdeck.py in one shell, then in another:

    pip install streamdeck
    python3 tests/test_enumerate.py

Expected: at least one device found, deck_type() includes "Stream Deck Mk.2",
and get_firmware_version() / get_serial_number() return the strings hard-coded
in uhid_streamdeck.py.
"""
import sys


def main() -> int:
    try:
        from StreamDeck.DeviceManager import DeviceManager
    except ImportError:
        print("ERROR: python-elgato-streamdeck not installed.")
        print("  pip install streamdeck")
        return 1

    # uhid-created devices are visible via /dev/hidraw, not libusb (they
    # are not USB devices). The library ships only a libusb backend, so we
    # register our own hidapi backend that uses libhidapi-hidraw under the
    # hood. See hid_transport.py at the repo root.
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import hid_transport
    hid_transport.register()

    decks = DeviceManager(transport="hidapi").enumerate()
    print(f"[+] {len(decks)} deck(s) found")
    if not decks:
        print("    (is uhid_streamdeck.py running? is the user in the 'input' group "
              "or running as root?)")
        return 2

    rc = 0
    for i, deck in enumerate(decks):
        print(f"\n--- deck {i} ---")
        try:
            deck.open()
            print(f"  class           : {type(deck).__name__}")
            print(f"  type            : {deck.deck_type()}")
            print(f"  key count       : {deck.key_count()}")
            print(f"  key layout      : {deck.key_layout()} (cols x rows)")
            print(f"  key image       : "
                  f"{deck.KEY_PIXEL_WIDTH}x{deck.KEY_PIXEL_HEIGHT} "
                  f"{deck.KEY_IMAGE_FORMAT}")
            try:
                print(f"  firmware        : {deck.get_firmware_version()!r}")
            except Exception as e:
                print(f"  firmware        : ERR {e!r}")
                rc = 3
            try:
                print(f"  serial          : {deck.get_serial_number()!r}")
            except Exception as e:
                print(f"  serial          : ERR {e!r}")
                rc = 3
        finally:
            try:
                deck.close()
            except Exception:
                pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
