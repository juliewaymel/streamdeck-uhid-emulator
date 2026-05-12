#!/usr/bin/env python3
"""
POC 1 — Virtual Elgato Stream Deck MK.2 via /dev/uhid.

Goal: announce a fake Elgato Stream Deck MK.2 to the Linux HID subsystem so
that hidapi / python-elgato-streamdeck / StreamController can detect it.

Run on the RPi:
    sudo modprobe uhid
    sudo python3 uhid_streamdeck.py

Then in another shell:
    ls /sys/bus/hid/devices/
    lsusb         # won't show (not a real USB device), but HID layer sees it
    cat /proc/bus/input/devices | grep -A4 -i elgato

If python-elgato-streamdeck is installed:
    python3 -c "from StreamDeck.DeviceManager import DeviceManager; \\
                print([d.deck_type() for d in DeviceManager().enumerate()])"
"""
import os
import select
import struct
import sys
import time

# /dev/uhid event types (from include/uapi/linux/uhid.h)
UHID_CREATE2          = 11
UHID_DESTROY          = 1
UHID_START            = 2
UHID_STOP             = 3
UHID_OPEN             = 4
UHID_CLOSE            = 5
UHID_OUTPUT           = 6
UHID_INPUT2           = 12
UHID_GET_REPORT       = 9
UHID_GET_REPORT_REPLY = 10
UHID_SET_REPORT       = 13
UHID_SET_REPORT_REPLY = 14

BUS_USB = 0x03

# Elgato Stream Deck MK.2 (15 buttons, 5x3, 72x72 px tiles)
VID_ELGATO         = 0x0fd9
PID_STREAMDECK_MK2 = 0x0080

# sizeof(struct uhid_event) = 4 (type) + 4372 (biggest member: create2_req)
EVENT_SIZE = 4376

# Minimal HID report descriptor (vendor-defined, matches Stream Deck layout):
#   - Input  report 0x01 : 17 bytes (1 report id + 16 bytes button data)
#   - Output report 0x02 : 1024 bytes (image chunk)
#   - Feature 0x03       : 32 bytes (brightness / reset / etc.)
#   - Feature 0x06       : 32 bytes (firmware ver — Stream Deck MK.2 uses 0x06)
HID_DESCRIPTOR = bytes([
    0x06, 0x00, 0xFF,        # Usage Page (Vendor Defined 0xFF00)
    0x09, 0x01,              # Usage (0x01)
    0xA1, 0x01,              # Collection (Application)

    # Input report 0x01 — button states
    0x85, 0x01,              #   Report ID (1)
    0x09, 0x01,              #   Usage (0x01)
    0x15, 0x00,              #   Logical Minimum (0)
    0x25, 0x01,              #   Logical Maximum (1)
    0x75, 0x01,              #   Report Size (1 bit)
    0x95, 0x80,              #   Report Count (128) -> 16 bytes
    0x81, 0x02,              #   Input (Data,Var,Abs)

    # Output report 0x02 — image data
    0x85, 0x02,              #   Report ID (2)
    0x09, 0x02,              #   Usage (0x02)
    0x15, 0x00,              #   Logical Minimum (0)
    0x26, 0xFF, 0x00,        #   Logical Maximum (255)
    0x75, 0x08,              #   Report Size (8 bits)
    0x96, 0xFF, 0x03,        #   Report Count (1023) -> 1023 bytes
    0x91, 0x02,              #   Output (Data,Var,Abs)

    # Feature report 0x03 — control (brightness, reset)
    0x85, 0x03,              #   Report ID (3)
    0x09, 0x03,              #   Usage (0x03)
    0x95, 0x1F,              #   Report Count (31)
    0xB1, 0x02,              #   Feature (Data,Var,Abs)

    # Feature report 0x06 — firmware version (Stream Deck MK.2 queries this)
    0x85, 0x06,              #   Report ID (6)
    0x09, 0x06,              #   Usage (0x06)
    0x95, 0x1F,              #   Report Count (31)
    0xB1, 0x02,              #   Feature (Data,Var,Abs)

    0xC0,                    # End Collection
])


def _pad(b: bytes, n: int) -> bytes:
    return (b + b"\x00" * n)[:n]


def create2_payload() -> bytes:
    """Pack a uhid_create2_req struct."""
    name = _pad(b"Elgato Stream Deck Virtual MK.2", 128)
    phys = _pad(b"uhid-virtual-streamdeck", 64)
    uniq = _pad(b"VSD-MK2-0001", 64)
    rd_data = _pad(HID_DESCRIPTOR, 4096)
    # <128s 64s 64s H H I I I I 4096s   (packed, little-endian)
    return struct.pack(
        "<128s64s64sHHIIII4096s",
        name, phys, uniq,
        len(HID_DESCRIPTOR), BUS_USB,
        VID_ELGATO, PID_STREAMDECK_MK2,
        0x0100, 0,
        rd_data,
    )


def write_event(fd: int, type_id: int, payload: bytes = b"") -> None:
    """Always write a full-sized uhid_event."""
    buf = struct.pack("<I", type_id) + payload
    buf = _pad(buf, EVENT_SIZE)
    os.write(fd, buf)


def event_name(type_id: int) -> str:
    return {
        UHID_START:            "START",
        UHID_STOP:             "STOP",
        UHID_OPEN:             "OPEN",
        UHID_CLOSE:            "CLOSE",
        UHID_OUTPUT:           "OUTPUT",
        UHID_GET_REPORT:       "GET_REPORT",
        UHID_SET_REPORT:       "SET_REPORT",
    }.get(type_id, f"UNKNOWN({type_id})")


def handle_get_report(fd: int, data: bytes) -> None:
    """
    uhid_get_report_req:
        __u32 id;
        __u8  rnum;
        __u8  rtype;
    Reply with UHID_GET_REPORT_REPLY:
        __u32 id;
        __u16 err;
        __u16 size;
        __u8  data[4096];
    """
    if len(data) < 4 + 6:
        return
    req_id, rnum, rtype = struct.unpack_from("<IBB", data, 4)
    print(f"    GET_REPORT id={req_id} rnum=0x{rnum:02x} rtype={rtype}")
    # Reply with dummy 32-byte zero buffer (just so the host doesn't hang).
    # Real Stream Deck would return firmware ver, serial number, etc.
    reply_data = _pad(bytes([rnum]) + b"\x00" * 31, 4096)
    payload = struct.pack("<IHH4096s", req_id, 0, 32, reply_data)
    write_event(fd, UHID_GET_REPORT_REPLY, payload)


def main() -> int:
    if not os.path.exists("/dev/uhid"):
        print("ERROR: /dev/uhid not found.", file=sys.stderr)
        print("  Try: sudo modprobe uhid", file=sys.stderr)
        return 1

    try:
        fd = os.open("/dev/uhid", os.O_RDWR)
    except PermissionError:
        print("ERROR: cannot open /dev/uhid (need root or 'input' group)", file=sys.stderr)
        return 1

    print(f"[+] /dev/uhid opened (fd={fd})")
    write_event(fd, UHID_CREATE2, create2_payload())
    print(f"[+] UHID_CREATE2 sent — VID=0x{VID_ELGATO:04x} PID=0x{PID_STREAMDECK_MK2:04x}")
    print(f"[+] Check: ls /sys/bus/hid/devices/  |  cat /proc/bus/input/devices")
    print(f"[+] Ctrl-C to destroy and exit\n")

    try:
        while True:
            r, _, _ = select.select([fd], [], [], 1.0)
            if fd not in r:
                continue
            data = os.read(fd, EVENT_SIZE)
            if len(data) < 4:
                continue
            type_id = struct.unpack_from("<I", data, 0)[0]
            print(f"[<] {event_name(type_id)} ({len(data)} bytes)")

            if type_id == UHID_GET_REPORT:
                handle_get_report(fd, data)
            elif type_id == UHID_OUTPUT:
                # uhid_output_req: __u8 data[4096]; __u16 size; __u8 rtype;
                size = struct.unpack_from("<H", data, 4 + 4096)[0]
                rtype = data[4 + 4096 + 2]
                rid = data[4] if size > 0 else 0
                print(f"    OUTPUT rid=0x{rid:02x} rtype={rtype} size={size}")
            elif type_id == UHID_SET_REPORT:
                # uhid_set_report_req: __u32 id; __u8 rnum; __u8 rtype; __u16 size; __u8 data[4096]
                req_id, rnum, rtype, size = struct.unpack_from("<IBBH", data, 4)
                print(f"    SET_REPORT id={req_id} rnum=0x{rnum:02x} rtype={rtype} size={size}")
                # Acknowledge with no-op reply
                ack = struct.pack("<IH", req_id, 0)
                write_event(fd, UHID_SET_REPORT_REPLY, ack)
    except KeyboardInterrupt:
        print("\n[+] Destroying virtual device...")
    finally:
        try:
            write_event(fd, UHID_DESTROY)
        except OSError:
            pass
        os.close(fd)
        print("[+] Bye.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
