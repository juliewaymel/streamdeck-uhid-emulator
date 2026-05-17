#!/usr/bin/env python3
"""
Phase 4c — pygame renderer that displays the virtual Stream Deck's key
images on the attached HDMI screen.

Reads JPEGs that uhid_streamdeck.py drops into /tmp/streamdeck-keys/ and
listens on the image-notify Unix socket for live updates.

Run on the Pi (after `sudo python3 uhid_streamdeck.py` is up):

    sudo python3 renderer.py

`sudo` is currently needed because on Pi OS Lite the pygame/SDL2 kmsdrm
backend wants r/w on /dev/dri/card*. Add `juliewwlr` to the `video` group
and a udev rule for /dev/dri/card0 to drop the sudo later.

Press Esc or Ctrl-C to quit.
"""
from __future__ import annotations

import os
import select
import socket
import sys
import time
from pathlib import Path

KEY_IMG_DIR = Path("/tmp/streamdeck-keys")
IMG_NOTIFY_SOCKET = "/tmp/streamdeck-img-notify.sock"

# Deck geometry — matches what uhid_streamdeck.py advertises.
KEY_COLS = 5
KEY_ROWS = 3
NATIVE_KEY_PX = 72
# Images arrive flipped both axes (Stream Deck OriginalV2 KEY_FLIP=(True, True)).
FLIP_LR = True
FLIP_TB = True

# Background and gutters between tiles.
BG_COLOR = (12, 12, 12)
TILE_BG = (40, 40, 40)
GUTTER_PX = 8


def best_video_driver() -> str:
    """Pick a video driver that works on Pi OS Lite without an X server."""
    # Prefer kmsdrm (direct DRM/KMS) when X is not running.
    if "DISPLAY" in os.environ or "WAYLAND_DISPLAY" in os.environ:
        return os.environ.get("SDL_VIDEODRIVER", "x11")
    return os.environ.get("SDL_VIDEODRIVER", "kmsdrm")


def init_pygame():
    os.environ.setdefault("SDL_VIDEODRIVER", best_video_driver())
    # Don't fail hard if there's no audio device (common on Lite).
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    import pygame

    # Init only the display subsystem so we can detect failure clearly.
    try:
        pygame.display.init()
    except pygame.error as e:
        print(f"ERROR: pygame display init failed under "
              f"SDL_VIDEODRIVER={os.environ.get('SDL_VIDEODRIVER')!r}: {e}",
              file=sys.stderr)
        raise

    info = pygame.display.Info()
    target = (info.current_w or 1024, info.current_h or 600)
    flags = pygame.FULLSCREEN | pygame.SCALED
    try:
        screen = pygame.display.set_mode(target, flags)
    except pygame.error:
        # Fallback to windowed for ad-hoc tests under X.
        screen = pygame.display.set_mode((1024, 600))
    pygame.display.set_caption("streamdeck-uhid-emulator")
    try:
        pygame.mouse.set_visible(False)
    except pygame.error:
        pass
    return pygame, screen


def compute_layout(screen_w: int, screen_h: int) -> tuple[int, int, int]:
    """Return (tile_px, origin_x, origin_y) centered grid."""
    avail_w = (screen_w - GUTTER_PX * (KEY_COLS + 1)) // KEY_COLS
    avail_h = (screen_h - GUTTER_PX * (KEY_ROWS + 1)) // KEY_ROWS
    tile = max(8, min(avail_w, avail_h))
    grid_w = tile * KEY_COLS + GUTTER_PX * (KEY_COLS - 1)
    grid_h = tile * KEY_ROWS + GUTTER_PX * (KEY_ROWS - 1)
    return tile, (screen_w - grid_w) // 2, (screen_h - grid_h) // 2


def tile_rect(layout, key: int):
    tile, ox, oy = layout
    col = key % KEY_COLS
    row = key // KEY_COLS
    x = ox + col * (tile + GUTTER_PX)
    y = oy + row * (tile + GUTTER_PX)
    return x, y, tile, tile


def load_tile(pygame, path: Path, tile_px: int):
    try:
        surf = pygame.image.load(str(path))
    except Exception as e:
        print(f"[render] {path.name}: {e}", file=sys.stderr)
        return None
    surf = surf.convert()
    if FLIP_LR or FLIP_TB:
        surf = pygame.transform.flip(surf, FLIP_LR, FLIP_TB)
    if surf.get_size() != (tile_px, tile_px):
        surf = pygame.transform.smoothscale(surf, (tile_px, tile_px))
    return surf


def subscribe_notify() -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind("")          # autobind
    s.setblocking(False)
    s.sendto(b"subscribe", IMG_NOTIFY_SOCKET)
    return s


def main() -> int:
    if not os.path.exists(IMG_NOTIFY_SOCKET):
        print(f"ERROR: {IMG_NOTIFY_SOCKET} missing — start uhid_streamdeck.py first.",
              file=sys.stderr)
        return 1

    pygame, screen = init_pygame()
    sw, sh = screen.get_size()
    layout = compute_layout(sw, sh)
    tile_px = layout[0]
    print(f"[render] display {sw}x{sh}, tile={tile_px}px, "
          f"origin=({layout[1]},{layout[2]})  driver={os.environ.get('SDL_VIDEODRIVER')}")

    surfaces: dict[int, "pygame.Surface"] = {}

    def redraw_all():
        screen.fill(BG_COLOR)
        for k in range(KEY_COLS * KEY_ROWS):
            x, y, w, h = tile_rect(layout, k)
            pygame.draw.rect(screen, TILE_BG, (x, y, w, h))
            if k in surfaces:
                screen.blit(surfaces[k], (x, y))
        pygame.display.flip()

    # Pre-load anything uhid_streamdeck.py has already written.
    for p in sorted(KEY_IMG_DIR.glob("key-*.jpg")):
        try:
            k = int(p.stem.split("-")[1])
        except (IndexError, ValueError):
            continue
        s = load_tile(pygame, p, tile_px)
        if s is not None:
            surfaces[k] = s
    redraw_all()

    notify = subscribe_notify()
    clock = pygame.time.Clock()
    running = True
    while running:
        # Drain key-change notifications (no blocking).
        try:
            r, _, _ = select.select([notify.fileno()], [], [], 0)
        except Exception:
            r = []
        if notify.fileno() in r:
            try:
                msg, _ = notify.recvfrom(4096)
                line = msg.decode("utf-8", "replace").strip()
                if line.startswith("key "):
                    parts = line.split(" ", 2)
                    if len(parts) == 3:
                        k = int(parts[1])
                        p = Path(parts[2])
                        s = load_tile(pygame, p, tile_px)
                        if s is not None:
                            surfaces[k] = s
                            redraw_all()
                            print(f"[render] key {k} updated ({p.name})")
            except (BlockingIOError, ConnectionResetError):
                pass

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                running = False

        clock.tick(30)

    try:
        notify.sendto(b"unsubscribe", IMG_NOTIFY_SOCKET)
        notify.close()
    except OSError:
        pass
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
