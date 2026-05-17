#!/usr/bin/env python3
"""
Phase 4c-bis — direct framebuffer renderer.

The SDL2/pygame kmsdrm backend will not produce a visible frame on this
hardware (Pi 4 + 1024x600 USB touchscreen; SDL claims the display, the
panel wakes up, but the scanout stays black). Writing straight to
/dev/fb0 works, so we render in software with Pillow and memcpy the
RGB565 buffer.

Run:

    sudo apt install -y python3-pil
    sudo systemctl start streamdeck-uhid
    sudo /home/juliewwlr/sdvenv/bin/python -B -u /home/juliewwlr/renderer_fb.py
"""
from __future__ import annotations

import fcntl
import mmap
import os
import select
import socket
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

KEY_IMG_DIR = Path("/tmp/streamdeck-keys")
IMG_NOTIFY_SOCKET = "/tmp/streamdeck-img-notify.sock"
FB_PATH = "/dev/fb0"

KEY_COLS = 8     # Stream Deck XL geometry: 8 cols x 4 rows = 32 keys
KEY_ROWS = 4
FLIP_LR = True
FLIP_TB = True

GUTTER_PX = 4    # tighter gutter so 32 tiles fit on 1024x600
BG_RGB = (12, 12, 12)
TILE_BG_RGB = (40, 40, 40)


# fb_var_screeninfo / fb_fix_screeninfo ioctls (from <linux/fb.h>)
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602


def get_fb_info(fd: int) -> tuple[int, int, int, int]:
    """Return (xres, yres, bpp, line_length_bytes) for /dev/fb0."""
    # struct fb_var_screeninfo is 160 bytes; we only need the first ints.
    var = bytearray(160)
    fcntl.ioctl(fd, FBIOGET_VSCREENINFO, var)
    xres, yres, xres_virtual, yres_virtual, xoffset, yoffset, bits_per_pixel = \
        struct.unpack_from("<IIIIIII", var, 0)

    fix = bytearray(80)
    fcntl.ioctl(fd, FBIOGET_FSCREENINFO, fix)
    # fb_fix_screeninfo: char id[16]; unsigned long smem_start; __u32 smem_len;
    # __u32 type; __u32 type_aux; __u32 visual; __u16 xpanstep; __u16 ypanstep;
    # __u16 ywrapstep; __u32 line_length; ...
    # On 64-bit ARM, unsigned long is 8 bytes -> offset of line_length is
    # 16 (id) + 8 (smem_start) + 4 + 4 + 4 + 4 + 2 + 2 + 2 + 2 (padding) = 48.
    # Easier: look up by name via a dict-style unpack.
    line_length = struct.unpack_from("<I", fix, 48)[0]
    return xres, yres, bits_per_pixel, line_length


def rgb888_to_rgb565_bytes(img: Image.Image) -> bytes:
    """Convert a PIL RGB image to little-endian RGB565 bytes."""
    if img.mode != "RGB":
        img = img.convert("RGB")
    arr = np.asarray(img, dtype=np.uint16)        # shape (h, w, 3)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return rgb565.astype("<u2").tobytes()


def compute_layout(w: int, h: int) -> tuple[int, int, int]:
    """Centered 5x3 grid; tile_px square. Returns (tile, origin_x, origin_y)."""
    avail_w = (w - GUTTER_PX * (KEY_COLS + 1)) // KEY_COLS
    avail_h = (h - GUTTER_PX * (KEY_ROWS + 1)) // KEY_ROWS
    tile = max(8, min(avail_w, avail_h))
    grid_w = tile * KEY_COLS + GUTTER_PX * (KEY_COLS - 1)
    grid_h = tile * KEY_ROWS + GUTTER_PX * (KEY_ROWS - 1)
    return tile, (w - grid_w) // 2, (h - grid_h) // 2


def tile_pos(layout, key: int) -> tuple[int, int]:
    tile, ox, oy = layout
    col = key % KEY_COLS
    row = key // KEY_COLS
    return ox + col * (tile + GUTTER_PX), oy + row * (tile + GUTTER_PX)


def blit_into_buffer(buf: bytearray, src: bytes, src_w: int, src_h: int,
                     dst_x: int, dst_y: int, fb_line_len: int, bpp: int) -> None:
    """Copy a RGB565 buffer (src_w x src_h, no padding) into the framebuffer."""
    if bpp != 16:
        return
    bytes_per_pixel = 2
    src_stride = src_w * bytes_per_pixel
    for row in range(src_h):
        dst_off = (dst_y + row) * fb_line_len + dst_x * bytes_per_pixel
        src_off = row * src_stride
        buf[dst_off:dst_off + src_stride] = src[src_off:src_off + src_stride]


def fill_rect(buf: bytearray, x: int, y: int, w: int, h: int,
              rgb: tuple[int, int, int], fb_line_len: int) -> None:
    r, g, b = rgb
    pixel = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
    px = struct.pack("<H", pixel)
    row_bytes = px * w
    for row in range(h):
        off = (y + row) * fb_line_len + x * 2
        buf[off:off + len(row_bytes)] = row_bytes


def main() -> int:
    if not os.path.exists(IMG_NOTIFY_SOCKET):
        print(f"ERROR: {IMG_NOTIFY_SOCKET} missing — start uhid_streamdeck.py first.",
              file=sys.stderr)
        return 1

    fb_fd = os.open(FB_PATH, os.O_RDWR)
    xres, yres, bpp, line_len = get_fb_info(fb_fd)
    print(f"[fb] {FB_PATH} {xres}x{yres} {bpp}bpp, line={line_len}B")
    if bpp != 16:
        print(f"ERROR: only RGB565 (16bpp) is implemented, got {bpp}bpp",
              file=sys.stderr)
        return 2

    fb_size = line_len * yres
    fb = mmap.mmap(fb_fd, fb_size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    # Treat the mmap as a writable bytearray-like surface.
    surf = memoryview(fb)

    layout = compute_layout(xres, yres)
    tile_px, ox, oy = layout
    print(f"[fb] tile={tile_px}px origin=({ox},{oy})")

    # Initial paint: bg + empty tile slots.
    fill_rect(fb, 0, 0, xres, yres, BG_RGB, line_len)
    for k in range(KEY_COLS * KEY_ROWS):
        tx, ty = tile_pos(layout, k)
        fill_rect(fb, tx, ty, tile_px, tile_px, TILE_BG_RGB, line_len)

    def update_key(key: int, path: Path) -> None:
        try:
            img = Image.open(path).convert("RGB")
        except Exception as e:
            print(f"[fb] load {path}: {e}", file=sys.stderr)
            return
        if FLIP_LR or FLIP_TB:
            img = img.transpose(Image.FLIP_LEFT_RIGHT) if FLIP_LR else img
            img = img.transpose(Image.FLIP_TOP_BOTTOM) if FLIP_TB else img
        if img.size != (tile_px, tile_px):
            img = img.resize((tile_px, tile_px), Image.LANCZOS)
        rgb565 = rgb888_to_rgb565_bytes(img)
        tx, ty = tile_pos(layout, key)
        blit_into_buffer(fb, rgb565, tile_px, tile_px, tx, ty, line_len, bpp)
        print(f"[fb] key {key} drawn at ({tx},{ty})")

    # Preload any existing JPEGs.
    for p in sorted(KEY_IMG_DIR.glob("key-*.jpg")):
        try:
            k = int(p.stem.split("-")[1])
        except (IndexError, ValueError):
            continue
        update_key(k, p)

    # Subscribe to notify socket and run.
    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify.bind("")
    notify.setblocking(False)
    notify.sendto(b"subscribe", IMG_NOTIFY_SOCKET)
    print("[fb] subscribed, running. Ctrl-C to exit.")

    try:
        while True:
            r, _, _ = select.select([notify.fileno()], [], [], 1.0)
            if not r:
                continue
            try:
                msg, _ = notify.recvfrom(4096)
            except (BlockingIOError, ConnectionResetError):
                continue
            line = msg.decode("utf-8", "replace").strip()
            if not line.startswith("key "):
                continue
            parts = line.split(" ", 2)
            if len(parts) != 3:
                continue
            try:
                k = int(parts[1])
            except ValueError:
                continue
            update_key(k, Path(parts[2]))
    except KeyboardInterrupt:
        pass
    finally:
        try:
            notify.sendto(b"unsubscribe", IMG_NOTIFY_SOCKET)
        except OSError:
            pass
        notify.close()
        fb.close()
        os.close(fb_fd)
        print("[fb] bye")
    return 0


if __name__ == "__main__":
    sys.exit(main())
