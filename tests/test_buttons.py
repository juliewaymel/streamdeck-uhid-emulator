#!/usr/bin/env python3
"""
Phase 3 sanity check: send press/release commands to the running
uhid_streamdeck.py over its Unix control socket and verify that
python-elgato-streamdeck delivers the corresponding key callbacks.

Run uhid_streamdeck.py first (must be root for /dev/uhid), then in
another shell:

    sudo python3 tests/test_buttons.py

Exit codes:
    0  all expected callbacks fired in the right order
    1  python-elgato-streamdeck not installed
    2  no deck enumerated
    3  one or more callbacks missing / wrong key / wrong state
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections import deque

CTRL_SOCK = "/tmp/streamdeck-vctrl.sock"


def send_cmd(line: str) -> str:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        # Bind to an autobind name so we can receive the reply.
        s.bind("")
        s.settimeout(1.5)
        s.sendto(line.encode() + b"\n", CTRL_SOCK)
        try:
            data, _ = s.recvfrom(4096)
            return data.decode("utf-8", "replace").strip()
        except socket.timeout:
            return "<no reply>"
    finally:
        s.close()


def main() -> int:
    try:
        from StreamDeck.DeviceManager import DeviceManager
    except ImportError:
        print("ERROR: pip install streamdeck", file=sys.stderr)
        return 1

    # Register the hidapi backend the same way test_enumerate does.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import hid_transport
    hid_transport.register()

    decks = DeviceManager(transport="hidapi").enumerate()
    if not decks:
        print("ERROR: no deck found. Is uhid_streamdeck.py running?", file=sys.stderr)
        return 2

    deck = decks[0]
    deck.open()
    print(f"[+] Opened {deck.deck_type()} ({deck.key_count()} keys)")

    events: deque = deque()
    ev_lock = threading.Lock()
    ev_cv = threading.Condition(ev_lock)

    def on_key(_deck, key, pressed):
        with ev_cv:
            events.append((key, pressed))
            ev_cv.notify_all()

    deck.set_key_callback(on_key)

    # Give the reader thread a beat so the first read() drains any boot-time
    # state. Then send press/release pairs and wait for matching callbacks.
    time.sleep(0.2)
    with ev_lock:
        events.clear()

    plan = [
        ("press 0",    (0, True)),
        ("release 0",  (0, False)),
        ("press 7",    (7, True)),
        ("press 14",   (14, True)),
        ("release 7",  (7, False)),
        ("release 14", (14, False)),
    ]

    rc = 0
    for cmd, expected in plan:
        reply = send_cmd(cmd)
        print(f"  -> {cmd:12s}  ctrl={reply!r}", end="")
        deadline = time.monotonic() + 1.0
        got = None
        with ev_cv:
            while time.monotonic() < deadline and not events:
                ev_cv.wait(timeout=deadline - time.monotonic())
            if events:
                got = events.popleft()
        if got == expected:
            print(f"  callback={got} ✓")
        else:
            print(f"  callback={got} (expected {expected}) ✗")
            rc = 3

    # Clean state, close.
    send_cmd("reset")
    deck.set_key_callback(None)
    deck.close()
    print(f"\n[+] exit rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
