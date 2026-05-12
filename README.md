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
| 1 | Virtual HID device shows up in `/sys/bus/hid/devices/` | in progress |
| 2 | `python-elgato-streamdeck` enumerates it | todo |
| 3 | Button reports (touch → HID input) | todo |
| 4 | Image writes intercepted and rendered (pygame) | todo |
| 5 | StreamController launches against the virtual deck | todo |

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

# If python-elgato-streamdeck is installed:
python3 -c "from StreamDeck.DeviceManager import DeviceManager; \
            print([d.deck_type() for d in DeviceManager().enumerate()])"
```

## Caveats

- Pure POC. The Stream Deck HID protocol is not officially documented; this
  reconstructs the parts needed for detection. See
  [cliffrowley's protocol notes](https://gist.github.com/cliffrowley/d18a9c4569537b195f2b1eb6c68469e0).
- `hidraw` access from userspace usually needs udev rules (or root).
- Not affiliated with or endorsed by Elgato/Corsair.

## License

MIT.
