#!/usr/bin/env python3
"""
Phase 4a sanity check: push a generated test image to a key via
python-elgato-streamdeck.set_key_image() and verify uhid_streamdeck.py
assembled the chunks back into a valid JPEG on disk.

Run uhid_streamdeck.py first, then in another shell:

    sudo /home/juliewwlr/sdvenv/bin/python tests/test_images.py

Exit codes:
    0  image saved, decodes, dimensions correct, pixels match
    1  python-elgato-streamdeck / Pillow not installed
    2  no deck enumerated
    3  uhid_streamdeck.py didn't write the expected file in time
    4  saved file fails to decode or has wrong size
    5  pixel mismatch (image got corrupted in transit)
"""
from __future__ import annotations

import os
import socket
import sys
import time
from pathlib import Path

KEY_IMG_DIR = Path("/tmp/streamdeck-keys")
IMG_NOTIFY_SOCKET = "/tmp/streamdeck-img-notify.sock"


def main() -> int:
    try:
        from StreamDeck.DeviceManager import DeviceManager
        from StreamDeck.ImageHelpers import PILHelper
        from PIL import Image, ImageDraw, ImageChops
    except ImportError as e:
        print(f"ERROR: missing dep: {e}", file=sys.stderr)
        print("  sudo apt install -y python3-pil; pip install streamdeck Pillow",
              file=sys.stderr)
        return 1

    # Register hidapi backend (same as test_enumerate / test_buttons).
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import hid_transport
    hid_transport.register()

    decks = DeviceManager(transport="hidapi").enumerate()
    if not decks:
        print("ERROR: no deck found. Is uhid_streamdeck.py running?", file=sys.stderr)
        return 2
    deck = decks[0]
    deck.open()
    print(f"[+] Opened {deck.deck_type()} ({deck.key_count()} keys, "
          f"{deck.KEY_PIXEL_WIDTH}x{deck.KEY_PIXEL_HEIGHT} {deck.KEY_IMAGE_FORMAT})")

    # Subscribe to the image-notify socket so we get told when the file lands.
    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify.bind("")     # autobind
    notify.settimeout(3.0)
    notify.sendto(b"subscribe", IMG_NOTIFY_SOCKET)
    ack, _ = notify.recvfrom(4096)
    print(f"[+] notify: {ack.decode().strip()}")

    rc = 0
    KEY = 7
    out_path = KEY_IMG_DIR / f"key-{KEY:02d}.jpg"
    try:
        out_path.unlink()
    except FileNotFoundError:
        pass

    # Build a recognisable test image: red bg, white "TEST 7" text-ish via
    # geometric shapes (no font file dependency).
    src = Image.new("RGB",
                    (deck.KEY_PIXEL_WIDTH, deck.KEY_PIXEL_HEIGHT),
                    (200, 30, 30))
    d = ImageDraw.Draw(src)
    d.rectangle([(6, 6), (65, 65)], outline=(255, 255, 255), width=3)
    d.line([(15, 15), (55, 55)], fill=(255, 255, 255), width=3)
    d.line([(55, 15), (15, 55)], fill=(255, 255, 255), width=3)
    image_bytes = PILHelper.to_native_key_format(deck, src)
    print(f"[+] encoded test image: {len(image_bytes)} bytes "
          f"(~{(len(image_bytes) + 1015) // 1016} chunks)")

    print(f"[+] set_key_image({KEY}, ...) ...")
    deck.set_key_image(KEY, image_bytes)

    # Wait for the assembled file via notify socket (with a fallback poll).
    deadline = time.monotonic() + 3.0
    saw_msg = None
    while time.monotonic() < deadline:
        try:
            msg, _ = notify.recvfrom(4096)
            saw_msg = msg.decode().strip()
            print(f"  notify: {saw_msg}")
            if saw_msg.startswith(f"key {KEY} "):
                break
        except socket.timeout:
            break

    if not out_path.exists():
        print(f"ERROR: {out_path} never appeared", file=sys.stderr)
        rc = 3
    else:
        size = out_path.stat().st_size
        try:
            decoded = Image.open(out_path)
            decoded.load()
        except Exception as e:
            print(f"ERROR: cannot decode {out_path}: {e}", file=sys.stderr)
            rc = 4
        else:
            if decoded.size != (deck.KEY_PIXEL_WIDTH, deck.KEY_PIXEL_HEIGHT):
                print(f"ERROR: wrong size {decoded.size}, expected "
                      f"({deck.KEY_PIXEL_WIDTH},{deck.KEY_PIXEL_HEIGHT})",
                      file=sys.stderr)
                rc = 4
            else:
                # The on-wire image is flipped both axes (KEY_FLIP=(True,True)
                # on this deck class). Un-flip before comparing.
                fixed = decoded.transpose(Image.FLIP_LEFT_RIGHT) \
                               .transpose(Image.FLIP_TOP_BOTTOM)
                diff = ImageChops.difference(src.convert("RGB"),
                                             fixed.convert("RGB"))
                bbox = diff.getbbox()
                # JPEG isn't lossless — measure mean abs error instead of
                # demanding byte-perfect match.
                import statistics
                pixels = list(diff.getdata())
                flat = [c for px in pixels for c in px]
                mae = statistics.mean(flat)
                print(f"  saved   : {out_path}  ({size} bytes)")
                print(f"  decoded : {decoded.size} {decoded.mode}")
                print(f"  diff bbox: {bbox}  mean-abs-err: {mae:.2f}")
                if mae > 15:
                    print("ERROR: mean error too high — image likely corrupted",
                          file=sys.stderr)
                    rc = 5

    notify.sendto(b"unsubscribe", IMG_NOTIFY_SOCKET)
    notify.close()
    deck.close()
    print(f"\n[+] exit rc={rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
