# streamdeck-uhid-emulator

Proof-of-concept: emulate an Elgato Stream Deck on Linux using the kernel's
`/dev/uhid` interface, so that [StreamController](https://github.com/StreamController/StreamController),
[streamdeck-linux-gui](https://github.com/streamdeck-linux-gui/streamdeck-linux-gui),
or any tool built on [`python-elgato-streamdeck`](https://github.com/abcminiuser/python-elgato-streamdeck)
can talk to a virtual device — no Elgato hardware required.

> Target use case: a Raspberry Pi with a USB touchscreen acting as a software
> Stream Deck. Instead of writing every plugin from scratch, expose the
> touchscreen as a "real" Stream Deck so the existing Linux ecosystem just works.

## Status

| Phase | What | Status |
|------:|------|--------|
| 1 | Virtual HID device shows up in `/sys/bus/hid/devices/` | ✅ done |
| 2 | `python-elgato-streamdeck` enumerates it + reads firmware/serial | ✅ done |
| 3 | Button reports (UHID_INPUT2) deliver key callbacks in the app | ✅ done |
| 4 | Image OUTPUT chunks reassembled and rendered on HDMI (fb0) | ✅ done |
| 5 | Touch (`/dev/input/event0`) → press socket → key callback | ✅ done |

The virtual device announces itself as a **Stream Deck XL** (PID `0x006c`,
32 keys arranged 8 × 4, 96 × 96 px JPEG tiles, `KEY_FLIP=(True, True)`).
Wire-level identical to the layout `python-elgato-streamdeck`'s
`StreamDeckXL` class expects — see `Stream Deck XL` reference table below.

Validated 2026-05-17 on a Raspberry Pi 4B (1GB, Pi OS 13 Trixie):

```
Found: 1 deck
class: StreamDeckOriginalV2     # lib treats PID 0x0080 as a V2 variant
KEY_COUNT: 15  layout: (3, 5)  image: 72x72 JPEG
open() OK, is_open: True, connected: True
firmware: '1.00.000'
serial:   'VSD-MK2-0001'
```

## Stream Deck XL reference (what we emulate)

Sourced from `python-elgato-streamdeck/src/StreamDeck/Devices/StreamDeckXL.py`.

| Field                       | Value                                |
|-----------------------------|--------------------------------------|
| `VENDOR_ID`                 | `0x0FD9` (Elgato)                    |
| `PRODUCT_ID`                | `0x006C`                             |
| `DECK_TYPE`                 | `"Stream Deck XL"`                   |
| `KEY_COUNT`                 | 32                                   |
| `KEY_COLS × KEY_ROWS`       | 8 × 4                                |
| `KEY_PIXEL_WIDTH × HEIGHT`  | 96 × 96                              |
| `KEY_IMAGE_FORMAT`          | JPEG                                 |
| `KEY_FLIP`                  | `(True, True)` — flip H + V          |
| `KEY_ROTATION`              | 0                                    |
| `IMAGE_REPORT_LENGTH`       | 1024 bytes per OUTPUT chunk          |
| `IMAGE_REPORT_HEADER_LENGTH`| 8 bytes (cmd, key, last, len, page)  |

### Input report (button states)

`device.read(4 + 32)` returns 36 bytes:

```
byte  0   : report id 0x01
bytes 1-3 : constant padding (we always send 0x00)
bytes 4-35: one byte per key, 0 = released, 1 = pressed
```

`uhid_streamdeck.py` declares the descriptor exactly this way and emits a
`UHID_INPUT2` event with these 36 bytes every time `_key_state` changes.

### Output report (image upload)

The host emits `IMAGE_REPORT_LENGTH = 1024`-byte writes with this header:

```
byte 0      : report id 0x02
byte 1      : command 0x07 (set key image)
byte 2      : key index (0..31)
byte 3      : is_last (1 on final chunk, else 0)
bytes 4-5   : this chunk payload length (LE uint16)
bytes 6-7   : page / sequence number (LE uint16, 0-based)
bytes 8-1023: up to 1016 bytes of JPEG
```

We reassemble per key, save as `/tmp/streamdeck-keys/key-NN.jpg`, and
notify `renderer_fb.py` to repaint that tile.

### Feature reports (control)

Reads:
- **`0x05`** — firmware version: 32 bytes returned, `data[6:]` is the
  ASCII firmware string. We answer `"1.00.000"`.
- **`0x06`** — serial number: 32 bytes returned, `data[2:]` is the ASCII
  serial. We answer `"VSD-MK2-0001"`.

Writes:
- **`0x03`** — control surface. Two known commands on XL:
  - `[0x03, 0x02, …]` = reset (clear all keys)
  - `[0x03, 0x08, percent, …]` = set brightness 0..100
  We currently `UHID_SET_REPORT_REPLY`-ack them without acting.

## How it works

`/dev/uhid` lets userspace create a virtual HID device. We send a `UHID_CREATE2`
event with:

- VID `0x0fd9` (Elgato), PID `0x0080` (Stream Deck MK.2)
- A HID report descriptor matching the MK.2 layout (input buttons / output image
  data / feature reports for brightness, firmware version)

The kernel then registers the device as if it were a real USB HID, and any
`hidapi`-based library sees it.

## Run

On the Linux host (Raspberry Pi OS 64-bit, Ubuntu, etc.):

```bash
sudo modprobe uhid
sudo python3 uhid_streamdeck.py
```

In another shell, verify it shows up:

```bash
ls /sys/bus/hid/devices/                      # new entry with 0FD9:0080
cat /proc/bus/input/devices | grep -i elgato
```

Then run the Phase 2 enumeration test:

```bash
sudo apt install -y libhidapi-hidraw0 libhidapi-libusb0
pip install streamdeck hid
sudo python3 tests/test_enumerate.py            # sudo until udev rule for /dev/hidraw* lands
```

Expected output:

```
[+] 1 deck(s) found
--- deck 0 ---
  class           : StreamDeckOriginalV2
  type            : Stream Deck Original
  key count       : 15
  key layout      : (3, 5) (cols x rows)
  key image       : 72x72 JPEG
  firmware        : '1.00.000'
  serial          : 'VSD-MK2-0001'
```

### Why a custom transport?

`python-elgato-streamdeck` only ships a `libusb` transport that enumerates real
USB devices via `libusb`. `/dev/uhid` devices are virtual — they live on the
kernel's `uhid` bus, not on a USB host controller, so libusb can't see them.
`hid_transport.py` adds an `hidapi` backend that walks `/dev/hidraw*` via
`libhidapi-hidraw` (the `hid` pip package), which does enumerate uhid devices.
`tests/test_enumerate.py` monkey-patches the new backend into `DeviceManager`'s
transports dict at import time.

### Renderer notes (Phase 4c)

`renderer.py` displays the key images on the attached HDMI screen. On Pi OS
Lite there's no X server, so SDL2's `kmsdrm` backend is the path of least
resistance.

The `pygame` wheel on PyPI bundles its own SDL2 which on aarch64 was built
without `kmsdrm`. Use the Debian `python3-pygame` (built against system SDL2
2.32+, which has `kmsdrm`) instead. The simplest way to make it visible from a
venv is:

```bash
sudo apt install -y python3-pygame
~/sdvenv/bin/pip uninstall -y pygame
ln -sfn /usr/lib/python3/dist-packages/pygame \
    ~/sdvenv/lib/python3.13/site-packages/pygame
```

Run with `sudo` because `/dev/dri/card*` is restricted to the `video` group;
the systemd transient unit below works well and survives SSH disconnects:

```bash
sudo systemd-run --quiet --unit=streamdeck-renderer \
    --setenv=SDL_VIDEODRIVER=kmsdrm \
    /home/juliewwlr/sdvenv/bin/python -B -u /home/juliewwlr/renderer.py
sudo journalctl -u streamdeck-renderer -f
```

## Caveats

- Pure POC. The Stream Deck HID protocol is not officially documented; this
  reconstructs the parts needed for detection. See
  [cliffrowley's protocol notes](https://gist.github.com/cliffrowley/d18a9c4569537b195f2b1eb6c68469e0).
- `hidraw` access from userspace usually needs udev rules (or root).
- Not affiliated with or endorsed by Elgato/Corsair.

## License

MIT.
