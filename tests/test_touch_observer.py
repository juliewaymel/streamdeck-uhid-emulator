#!/usr/bin/env python3
"""
Phase 5 sanity check: opens the deck via python-elgato-streamdeck and
prints every key press/release. Touch the HDMI screen — each tap
should print "key N pressed/released".

Run with uhid_streamdeck.py + touch_input.py active:

    sudo /home/juliewwlr/sdvenv/bin/python -B -u tests/test_touch_observer.py

Ctrl-C to quit.
"""
import os, sys, time, threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hid_transport
hid_transport.register()

from StreamDeck.DeviceManager import DeviceManager


def main():
    decks = DeviceManager(transport="hidapi").enumerate()
    if not decks:
        print("ERROR: no deck — start uhid_streamdeck.py first")
        return 1
    deck = decks[0]
    deck.open()
    print(f"[+] watching {deck.deck_type()} — touch the screen, Ctrl-C to exit")

    stop = threading.Event()

    def on_key(_d, k, pressed):
        kind = "DOWN" if pressed else "UP  "
        print(f"  {time.strftime('%H:%M:%S')}  {kind}  key {k}")

    deck.set_key_callback(on_key)
    try:
        while not stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        deck.set_key_callback(None)
        deck.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
