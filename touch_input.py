#!/usr/bin/env python3
"""
Phase 5 — read the USB touchscreen via evdev and forward taps to
uhid_streamdeck.py's control socket as press/release commands.

Coordinates from /dev/input/event0 arrive in the panel's own 0..max
range (4096 on the wch.cn module), so we first scale them to the
framebuffer's pixel space, then map onto the 5x3 tile layout that
renderer_fb.py paints.

Run after uhid_streamdeck.py is up:

    sudo /home/juliewwlr/sdvenv/bin/python -B -u /home/juliewwlr/touch_input.py
"""
from __future__ import annotations

import fcntl
import socket
import struct
import sys
from pathlib import Path

from evdev import InputDevice, categorize, ecodes, list_devices

CTRL_SOCKET_PATH = "/tmp/streamdeck-vctrl.sock"
FB_PATH = "/dev/fb0"

KEY_COLS = 8     # Stream Deck XL geometry: 8 cols x 4 rows = 32 keys
KEY_ROWS = 4
GUTTER_PX = 4

FBIOGET_VSCREENINFO = 0x4600


def find_touchscreen() -> InputDevice:
    """Pick the first evdev node that supports BTN_TOUCH + ABS_X/ABS_Y."""
    for path in list_devices():
        d = InputDevice(path)
        caps = d.capabilities()
        if ecodes.BTN_TOUCH not in caps.get(ecodes.EV_KEY, []):
            continue
        abs_codes = {c for c, _ in caps.get(ecodes.EV_ABS, [])}
        if ecodes.ABS_X in abs_codes and ecodes.ABS_Y in abs_codes:
            return d
    raise SystemExit("ERROR: no touchscreen found in /dev/input/event*")


def get_screen_size() -> tuple[int, int]:
    """Read xres/yres from /dev/fb0 ioctl."""
    fd = None
    try:
        import os
        fd = os.open(FB_PATH, os.O_RDONLY)
        var = bytearray(160)
        fcntl.ioctl(fd, FBIOGET_VSCREENINFO, var)
        xres, yres = struct.unpack_from("<II", var, 0)
        return xres, yres
    finally:
        if fd is not None:
            os.close(fd)


def compute_layout(w: int, h: int) -> tuple[int, int, int]:
    avail_w = (w - GUTTER_PX * (KEY_COLS + 1)) // KEY_COLS
    avail_h = (h - GUTTER_PX * (KEY_ROWS + 1)) // KEY_ROWS
    tile = max(8, min(avail_w, avail_h))
    grid_w = tile * KEY_COLS + GUTTER_PX * (KEY_COLS - 1)
    grid_h = tile * KEY_ROWS + GUTTER_PX * (KEY_ROWS - 1)
    return tile, (w - grid_w) // 2, (h - grid_h) // 2


def hit_test(layout, x: int, y: int) -> int | None:
    """Map pixel coords to key index 0..14, or None if outside any tile."""
    tile, ox, oy = layout
    if x < ox or y < oy:
        return None
    col = (x - ox) // (tile + GUTTER_PX)
    row = (y - oy) // (tile + GUTTER_PX)
    if not (0 <= col < KEY_COLS and 0 <= row < KEY_ROWS):
        return None
    # Reject points that fell in the gutters between tiles.
    cell_left = ox + col * (tile + GUTTER_PX)
    cell_top = oy + row * (tile + GUTTER_PX)
    if x >= cell_left + tile or y >= cell_top + tile:
        return None
    return int(row * KEY_COLS + col)


def send_cmd(line: str) -> None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        s.sendto(line.encode() + b"\n", CTRL_SOCKET_PATH)
    finally:
        s.close()


def main() -> int:
    import time

    screen_w, screen_h = get_screen_size()
    layout = compute_layout(screen_w, screen_h)
    print(f"[touch] screen={screen_w}x{screen_h} tile={layout[0]}px "
          f"origin=({layout[1]},{layout[2]})")
    print(f"[touch] ctrl socket -> {CTRL_SOCKET_PATH}")

    held_key: int | None = None
    raw_x = 0
    raw_y = 0
    x_max = y_max = 4096

    def release_held():
        nonlocal held_key
        if held_key is not None:
            try:
                send_cmd(f"release {held_key}")
            except OSError:
                pass
            held_key = None

    # Outer loop: re-find the touchscreen if the USB device disappears
    # (the wch.cn module hot-disconnects sporadically). We re-scan all
    # /dev/input/event* each time and pick the first touch-capable one.
    while True:
        try:
            dev = find_touchscreen()
        except SystemExit:
            print("[touch] no touchscreen found, retrying in 2s...")
            time.sleep(2)
            continue
        try:
            abs_info = dict(dev.capabilities().get(ecodes.EV_ABS, []))
            x_max = abs_info[ecodes.ABS_X].max or 4096
            y_max = abs_info[ecodes.ABS_Y].max or 4096
            print(f"[touch] reading {dev.path} ({dev.name}) "
                  f"raw range x=0..{x_max} y=0..{y_max}")
            for event in dev.read_loop():
                if event.type == ecodes.EV_ABS:
                    if event.code == ecodes.ABS_X:
                        raw_x = event.value
                    elif event.code == ecodes.ABS_Y:
                        raw_y = event.value
                elif event.type == ecodes.EV_KEY and event.code == ecodes.BTN_TOUCH:
                    if event.value == 1:
                        px = (raw_x * screen_w) // x_max
                        py = (raw_y * screen_h) // y_max
                        k = hit_test(layout, px, py)
                        print(f"[touch] DOWN raw=({raw_x},{raw_y}) "
                              f"px=({px},{py}) -> key {k}")
                        if k is not None:
                            try:
                                send_cmd(f"press {k}")
                                held_key = k
                            except OSError as e:
                                print(f"[touch] send_cmd failed: {e}")
                    else:
                        print(f"[touch] UP   (was held key {held_key})")
                        release_held()
        except (OSError, IOError) as e:
            # USB hot-disconnect or evdev hiccup — drop the held key, retry.
            print(f"[touch] device error ({e}); re-discovering in 1s...")
            release_held()
            time.sleep(1)
        except KeyboardInterrupt:
            release_held()
            return 0


if __name__ == "__main__":
    sys.exit(main())
