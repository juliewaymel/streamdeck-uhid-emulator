#!/usr/bin/env python3
"""Push a colored numbered tile to every key. Demo for the renderer."""
import os, sys, time
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hid_transport
hid_transport.register()
from StreamDeck.DeviceManager import DeviceManager
from StreamDeck.ImageHelpers import PILHelper


COLORS = [
    (220, 50, 50), (220, 130, 40), (220, 200, 40),
    (130, 200, 50), (40, 200, 80), (40, 200, 200),
    (40, 130, 220), (60, 80, 220), (160, 60, 220),
    (220, 60, 200), (180, 180, 180), (90, 90, 90),
    (220, 220, 110), (110, 220, 220), (220, 110, 180),
]


def main():
    decks = DeviceManager(transport="hidapi").enumerate()
    deck = decks[0]
    deck.open()
    print(f"Pushing to {deck.key_count()} keys ...")
    for k in range(deck.key_count()):
        img = Image.new("RGB", (deck.KEY_PIXEL_WIDTH, deck.KEY_PIXEL_HEIGHT),
                        COLORS[k % len(COLORS)])
        d = ImageDraw.Draw(img)
        # big numeral, simple geometric
        d.rectangle([(4, 4), (67, 67)], outline=(0, 0, 0), width=3)
        digit = str(k)
        for i, ch in enumerate(digit):
            d.text((18 + i * 16, 22), ch, fill=(0, 0, 0))
        deck.set_key_image(k, PILHelper.to_native_key_format(deck, img))
        time.sleep(0.1)
    deck.close()
    print("done")


if __name__ == "__main__":
    main()
