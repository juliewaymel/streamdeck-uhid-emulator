#!/usr/bin/env python3
"""
Virtual Elgato Stream Deck MK.2 via /dev/uhid.

Announces a fake Stream Deck MK.2 to the Linux HID subsystem so that
hidapi / python-elgato-streamdeck / StreamController can detect it.

Run on the RPi:
    sudo modprobe uhid
    sudo python3 uhid_streamdeck.py

In another shell:
    ls /sys/bus/hid/devices/
    cat /proc/bus/input/devices | grep -A4 -i elgato
    python3 tests/test_enumerate.py
"""
import os
import select
import signal
import socket
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

# Elgato Stream Deck MK.2: 15 buttons (5x3), 72x72 px tiles, JPEG-encoded
VID_ELGATO         = 0x0fd9
PID_STREAMDECK_MK2 = 0x0080

# python-elgato-streamdeck reads:
#   - feature 0x05, 32 bytes  -> firmware version (data[6:] is ASCII)
#   - feature 0x06, 32 bytes  -> serial number   (data[2:] is ASCII)
# It writes:
#   - feature 0x03, 32 bytes  -> reset / brightness / etc.
#   - output  0x02, 1024 bytes -> image chunk per key
FW_VERSION_STRING = b"1.00.000"
SERIAL_STRING     = b"VSD-MK2-0001"

# sizeof(struct uhid_event) = 4 (type) + 4372 (biggest member: create2_req)
EVENT_SIZE = 4376
UHID_DATA_MAX = 4096

# Stream Deck MK.2 input-report layout (verified against
# python-elgato-streamdeck's StreamDeckOriginalV2._read_control_states):
#   device.read(4 + KEY_COUNT)  ->  4 header bytes + 15 key bytes
# The first byte returned by hidapi is the report ID. So:
#   wire bytes 0..3  = report id (0x01) + 3 bytes padding/header
#   wire bytes 4..18 = 1 byte per key (0=released, 1=pressed)
# Total 19 bytes, including the report id prefix.
KEY_COUNT = 15
INPUT_PAD_BYTES = 3                   # bytes between report id and key states
INPUT_REPORT_SIZE_NOID = INPUT_PAD_BYTES + KEY_COUNT  # bytes we send via UHID_INPUT2

# HID report descriptor (vendor-defined, mimics Stream Deck MK.2):
#   - Input   0x01 : report id + 3 pad + 15 key bytes (19 bytes wire)
#   - Output  0x02 : 1024 bytes (image chunk)
#   - Feature 0x03 : 32 bytes (control: reset / brightness)
#   - Feature 0x05 : 32 bytes (firmware version, read)
#   - Feature 0x06 : 32 bytes (serial number, read)
HID_DESCRIPTOR = bytes([
    0x06, 0x00, 0xFF,        # Usage Page (Vendor Defined 0xFF00)
    0x09, 0x01,              # Usage (0x01)
    0xA1, 0x01,              # Collection (Application)

    # Input report 0x01 — 3 constant padding bytes + 15 per-key state bytes
    0x85, 0x01,              #   Report ID (1)
    0x09, 0x01,              #   Usage
    0x75, 0x08,              #   Report Size (8 bits)
    0x95, INPUT_PAD_BYTES,   #   Report Count (3) -> 3 bytes padding
    0x81, 0x03,              #   Input (Cnst,Var,Abs)
    0x09, 0x02,              #   Usage (key states)
    0x15, 0x00,              #   Logical Min 0
    0x25, 0x01,              #   Logical Max 1
    0x75, 0x08,              #   Report Size (8 bits)
    0x95, KEY_COUNT,         #   Report Count (15) -> 15 key state bytes
    0x81, 0x02,              #   Input (Data,Var,Abs)

    # Output report 0x02 — image data (1023 bytes after report id)
    0x85, 0x02,
    0x09, 0x02,
    0x15, 0x00,
    0x26, 0xFF, 0x00,
    0x75, 0x08,
    0x96, 0xFF, 0x03,
    0x91, 0x02,

    # Feature 0x03 — control (set brightness, reset, etc.)
    0x85, 0x03,
    0x09, 0x03,
    0x95, 0x1F,
    0xB1, 0x02,

    # Feature 0x05 — firmware version (read)
    0x85, 0x05,
    0x09, 0x05,
    0x95, 0x1F,
    0xB1, 0x02,

    # Feature 0x06 — serial number (read)
    0x85, 0x06,
    0x09, 0x06,
    0x95, 0x1F,
    0xB1, 0x02,

    0xC0,                    # End Collection
])


def _pad(b: bytes, n: int) -> bytes:
    return (b + b"\x00" * n)[:n]


def create2_payload() -> bytes:
    """Pack a uhid_create2_req struct."""
    name = _pad(b"Elgato Stream Deck Virtual MK.2", 128)
    phys = _pad(b"uhid-virtual-streamdeck", 64)
    uniq = _pad(SERIAL_STRING, 64)
    rd_data = _pad(HID_DESCRIPTOR, UHID_DATA_MAX)
    return struct.pack(
        "<128s64s64sHHIIII4096s",
        name, phys, uniq,
        len(HID_DESCRIPTOR), BUS_USB,
        VID_ELGATO, PID_STREAMDECK_MK2,
        0x0100,  # version
        0,       # country
        rd_data,
    )


def write_event(fd: int, type_id: int, payload: bytes = b"") -> None:
    """Always write a full-sized uhid_event."""
    buf = struct.pack("<I", type_id) + payload
    buf = _pad(buf, EVENT_SIZE)
    os.write(fd, buf)


def event_name(type_id: int) -> str:
    return {
        UHID_START:      "START",
        UHID_STOP:       "STOP",
        UHID_OPEN:       "OPEN",
        UHID_CLOSE:      "CLOSE",
        UHID_OUTPUT:     "OUTPUT",
        UHID_GET_REPORT: "GET_REPORT",
        UHID_SET_REPORT: "SET_REPORT",
    }.get(type_id, f"UNKNOWN({type_id})")


def build_feature_reply(rnum: int) -> bytes:
    """Return the 32-byte buffer hidapi will see for read_feature(rnum, 32)."""
    if rnum == 0x05:
        # python-elgato-streamdeck: get_firmware_version() reads response[6:]
        body = b"\x05" + b"\x00" * 5 + FW_VERSION_STRING
    elif rnum == 0x06:
        # python-elgato-streamdeck: get_serial_number() reads response[2:]
        body = b"\x06" + b"\x00" + SERIAL_STRING
    else:
        body = bytes([rnum])
    return _pad(body, 32)


def handle_get_report(fd: int, data: bytes) -> None:
    """
    Reply with UHID_GET_REPORT_REPLY:
        __u32 id; __u16 err; __u16 size; __u8 data[4096];
    """
    if len(data) < 4 + 6:
        return
    req_id, rnum, rtype = struct.unpack_from("<IBB", data, 4)
    reply = build_feature_reply(rnum)
    print(f"    GET_REPORT id={req_id} rnum=0x{rnum:02x} rtype={rtype} "
          f"-> reply {len(reply)}B")
    payload = struct.pack(
        f"<IHH{UHID_DATA_MAX}s",
        req_id, 0, len(reply), _pad(reply, UHID_DATA_MAX),
    )
    write_event(fd, UHID_GET_REPORT_REPLY, payload)


def handle_set_report(fd: int, data: bytes) -> None:
    """
    uhid_set_report_req:
        __u32 id; __u8 rnum; __u8 rtype; __u16 size; __u8 data[4096];
    Acknowledge so the host doesn't hang.
    """
    if len(data) < 4 + 8:
        return
    req_id, rnum, rtype, size = struct.unpack_from("<IBBH", data, 4)
    payload_bytes = data[4 + 8 : 4 + 8 + size] if size > 0 else b""
    print(f"    SET_REPORT id={req_id} rnum=0x{rnum:02x} rtype={rtype} "
          f"size={size} head={payload_bytes[:8].hex()}")
    ack = struct.pack("<IH", req_id, 0)
    write_event(fd, UHID_SET_REPORT_REPLY, ack)


# Track image-write progress per key so we can see whether the host is
# actually pushing frames at us.
_image_chunks_received = 0
_last_log_t = 0.0


def handle_output(data: bytes) -> None:
    """uhid_output_req: __u8 data[4096]; __u16 size; __u8 rtype;"""
    global _image_chunks_received, _last_log_t
    if len(data) < 4 + UHID_DATA_MAX + 3:
        return
    size = struct.unpack_from("<H", data, 4 + UHID_DATA_MAX)[0]
    rtype = data[4 + UHID_DATA_MAX + 2]
    rid = data[4] if size > 0 else 0
    if rid == 0x02:
        _image_chunks_received += 1
        now = time.monotonic()
        if now - _last_log_t > 1.0:
            print(f"    OUTPUT image chunks={_image_chunks_received} "
                  f"(last size={size})")
            _last_log_t = now
    else:
        print(f"    OUTPUT rid=0x{rid:02x} rtype={rtype} size={size}")


# ---------- Phase 3: button input ---------------------------------------
#
# Button state lives in a 15-byte buffer (one byte per key, 0 or 1). Every
# time it changes we send a UHID_INPUT2 event whose payload matches the
# input report layout declared in HID_DESCRIPTOR: report id 0x01, three
# constant padding bytes, then the 15 key bytes.
#
# To drive the buffer we listen on a Unix datagram socket. Test harnesses
# (or a future pygame touchscreen layer) write line-oriented commands:
#   press <0-14>     set key N to 1, send report
#   release <0-14>   set key N to 0, send report
#   state <30 hex>   replace all 15 key bytes, send report
#   reset            clear all keys, send report

CTRL_SOCKET_PATH = "/tmp/streamdeck-vctrl.sock"

_key_state = bytearray(KEY_COUNT)


def send_input_report(fd: int) -> None:
    """Send the current _key_state as a UHID_INPUT2 event."""
    report = bytes([0x01]) + b"\x00" * INPUT_PAD_BYTES + bytes(_key_state)
    # uhid_input2_req: __u16 size; __u8 data[4096];
    payload = struct.pack(f"<H{UHID_DATA_MAX}s", len(report),
                          _pad(report, UHID_DATA_MAX))
    write_event(fd, UHID_INPUT2, payload)


def handle_ctrl_command(fd: int, line: str) -> str:
    """Parse one command, mutate _key_state, send the report. Returns a reply."""
    parts = line.strip().split()
    if not parts:
        return "ERR empty\n"
    cmd = parts[0].lower()

    if cmd == "press" and len(parts) == 2 and parts[1].isdigit():
        n = int(parts[1])
        if not 0 <= n < KEY_COUNT:
            return f"ERR key out of range 0..{KEY_COUNT-1}\n"
        _key_state[n] = 1
        send_input_report(fd)
        return f"OK press {n}\n"

    if cmd == "release" and len(parts) == 2 and parts[1].isdigit():
        n = int(parts[1])
        if not 0 <= n < KEY_COUNT:
            return f"ERR key out of range 0..{KEY_COUNT-1}\n"
        _key_state[n] = 0
        send_input_report(fd)
        return f"OK release {n}\n"

    if cmd == "state" and len(parts) == 2:
        try:
            new = bytes.fromhex(parts[1])
        except ValueError:
            return "ERR state hex invalid\n"
        if len(new) != KEY_COUNT:
            return f"ERR state needs {KEY_COUNT} bytes ({KEY_COUNT*2} hex)\n"
        _key_state[:] = new
        send_input_report(fd)
        return f"OK state {parts[1]}\n"

    if cmd == "reset":
        for i in range(KEY_COUNT):
            _key_state[i] = 0
        send_input_report(fd)
        return "OK reset\n"

    return f"ERR unknown command: {parts[0]}\n"


def open_ctrl_socket() -> socket.socket:
    """Create a Unix SOCK_DGRAM socket at CTRL_SOCKET_PATH (overwrite if stale)."""
    try:
        os.unlink(CTRL_SOCKET_PATH)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    s.bind(CTRL_SOCKET_PATH)
    os.chmod(CTRL_SOCKET_PATH, 0o666)
    s.setblocking(False)
    return s


def main() -> int:
    if not os.path.exists("/dev/uhid"):
        print("ERROR: /dev/uhid not found.", file=sys.stderr)
        print("  Try: sudo modprobe uhid", file=sys.stderr)
        return 1

    try:
        fd = os.open("/dev/uhid", os.O_RDWR)
    except PermissionError:
        print("ERROR: cannot open /dev/uhid (need root)", file=sys.stderr)
        return 1

    print(f"[+] /dev/uhid opened (fd={fd})")
    write_event(fd, UHID_CREATE2, create2_payload())
    print(f"[+] UHID_CREATE2 sent — VID=0x{VID_ELGATO:04x} PID=0x{PID_STREAMDECK_MK2:04x}")
    print(f"[+] firmware='{FW_VERSION_STRING.decode()}'  serial='{SERIAL_STRING.decode()}'")

    ctrl = open_ctrl_socket()
    print(f"[+] Ctrl socket at {CTRL_SOCKET_PATH} "
          f"(send 'press N', 'release N', 'state HEX', 'reset')")
    print(f"[+] Check: ls /sys/bus/hid/devices/  |  python3 tests/test_enumerate.py")
    print(f"[+] Ctrl-C to destroy and exit\n")

    stop = False
    def _sigterm(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGTERM, _sigterm)

    try:
        while not stop:
            r, _, _ = select.select([fd, ctrl.fileno()], [], [], 1.0)
            if ctrl.fileno() in r:
                try:
                    data, addr = ctrl.recvfrom(4096)
                    line = data.decode("utf-8", "replace")
                    reply = handle_ctrl_command(fd, line)
                    print(f"[ctrl] {line.strip()!r} -> {reply.strip()}")
                    if addr:
                        try:
                            ctrl.sendto(reply.encode(), addr)
                        except OSError:
                            pass
                except (BlockingIOError, ConnectionResetError):
                    pass
            if fd in r:
                data = os.read(fd, EVENT_SIZE)
                if len(data) < 4:
                    continue
                type_id = struct.unpack_from("<I", data, 0)[0]
                if type_id not in (UHID_OUTPUT,):
                    print(f"[<] {event_name(type_id)}")

                if type_id == UHID_GET_REPORT:
                    handle_get_report(fd, data)
                elif type_id == UHID_SET_REPORT:
                    handle_set_report(fd, data)
                elif type_id == UHID_OUTPUT:
                    handle_output(data)
    except KeyboardInterrupt:
        print("\n[+] Ctrl-C — destroying virtual device...")
    finally:
        try:
            ctrl.close()
        except OSError:
            pass
        try:
            os.unlink(CTRL_SOCKET_PATH)
        except OSError:
            pass
        try:
            write_event(fd, UHID_DESTROY)
        except OSError:
            pass
        os.close(fd)
        print("[+] Bye.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
