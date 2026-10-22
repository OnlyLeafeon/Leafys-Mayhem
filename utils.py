from __future__ import annotations

import json
import os
import struct
import threading

from unrealsdk import logging

_active_sock = None
_send_lock = threading.Lock()


def log(msg: str) -> None:
    logging.info(f"[AP] {msg}")


def register_active_socket(sock) -> None:
    global _active_sock
    _active_sock = sock


def clear_active_socket() -> None:
    global _active_sock
    _active_sock = None


def get_active_socket():
    return _active_sock


def send_frame(sock, payload: bytes, opcode: int = 0x1) -> None:
    b1 = 0x80 | opcode
    length = len(payload)
    mask_key = os.urandom(4)
    masked_payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    if length <= 125:
        header = struct.pack("BB", b1, 0x80 | length)
    elif length <= 0xFFFF:
        header = struct.pack("!BBH", b1, 0x80 | 126, length)
    else:
        header = struct.pack("!BBQ", b1, 0x80 | 127, length)
    with _send_lock:
        sock.sendall(header + mask_key + masked_payload)


def send_json(packet: dict) -> bool:
    sock = _active_sock
    if sock is None:
        return False
    try:
        send_frame(sock, json.dumps([packet]).encode("utf-8"))
    except Exception as e:
        log(f"<send failed> {e!r}")
        return False
    return True


def send_location_check(name: str) -> None:
    if send_json({"cmd": "LocationChecks", "locations": [name]}):
        log(f"<check> sent {name!r}")
    else:
        log(f"<check> {name!r} ready to send but not connected right now")
