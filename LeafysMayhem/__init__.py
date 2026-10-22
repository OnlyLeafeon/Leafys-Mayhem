from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import socket
import struct
import threading
import uuid
import zlib
from dataclasses import dataclass
from urllib.parse import urlparse

try:
    import ssl
except ImportError:
    ssl = None

from mods_base import build_mod, command

from .hooks import _on_actor_tick, _on_health_depleted
from .locks import (
    _on_get_value_of_attribute,
    ap_lock_rarity,
    ap_lock_type,
    ap_rarities,
    ap_types,
    ap_unlock_rarity,
    ap_unlock_type,
)
from .LeafysMayhem.utils import (
    clear_active_socket,
    get_active_socket,
    log as _log,
    register_active_socket,
    send_frame,
    send_json,
)


GAME_NAME = "Borderlands 3"
CLIENT_VERSION = {"major": 0, "minor": 5, "build": 1, "class": "Version"}

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@dataclass
class _ConnectionState:
    thread: threading.Thread | None = None
    stop_requested: bool = False


_state = _ConnectionState()


class _WebSocketClosed(Exception):
    pass


class _PerMessageDeflate:

    def __init__(self, server_no_context_takeover: bool) -> None:
        self._reset_each_message = server_no_context_takeover
        self._decompressor = zlib.decompressobj(wbits=-15)

    def decompress(self, payload: bytes) -> bytes:
        data = self._decompressor.decompress(payload + b"\x00\x00\xff\xff")
        if self._reset_each_message:
            self._decompressor = zlib.decompressobj(wbits=-15)
        return data


def _recv_n(sock: socket.socket, leftover: list[bytes], n: int) -> bytes:
    while sum(len(b) for b in leftover) < n:
        chunk = sock.recv(4096)
        if not chunk:
            raise _WebSocketClosed("socket closed by peer")
        leftover.append(chunk)
    data = b"".join(leftover)
    result, rest = data[:n], data[n:]
    leftover.clear()
    if rest:
        leftover.append(rest)
    return result


def _ws_handshake(sock: socket.socket, host: str, port: int, path: str) -> tuple[list[bytes], "_PerMessageDeflate | None"]:
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Extensions: permessage-deflate; client_no_context_takeover; "
        "server_no_context_takeover\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise _WebSocketClosed("connection closed during handshake")
        buf += chunk
    header_data, _, leftover = buf.partition(b"\r\n\r\n")
    headers = header_data.decode("iso-8859-1", errors="replace")

    status_line = headers.split("\r\n", 1)[0]
    if " 101 " not in status_line:
        raise ConnectionError(f"handshake failed: {status_line}")

    expected_accept = base64.b64encode(
        hashlib.sha1((key + _WS_GUID).encode("ascii")).digest(),
    ).decode()
    if expected_accept not in headers:
        _log("Sec-WebSocket-Accept didn't match what was expected - continuing anyway.")

    deflate: _PerMessageDeflate | None = None
    for line in headers.split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "sec-websocket-extensions" and "permessage-deflate" in value.lower():
            params = value.lower()
            deflate = _PerMessageDeflate(
                server_no_context_takeover="server_no_context_takeover" in params,
            )
            _log("Server accepted permessage-deflate compression.")
            break

    return ([leftover] if leftover else []), deflate


def _read_frame(sock: socket.socket, leftover: list[bytes]) -> tuple[bool, int, bytes, bool]:
    b1, b2 = _recv_n(sock, leftover, 2)
    fin = bool(b1 & 0x80)
    rsv1 = bool(b1 & 0x40)
    opcode = b1 & 0x0F
    masked = bool(b2 & 0x80)
    length = b2 & 0x7F
    if length == 126:
        length = struct.unpack(">H", _recv_n(sock, leftover, 2))[0]
    elif length == 127:
        length = struct.unpack(">Q", _recv_n(sock, leftover, 8))[0]
    mask_key = _recv_n(sock, leftover, 4) if masked else None
    payload = _recv_n(sock, leftover, length)
    if masked:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload, rsv1


def _read_message(
    sock: socket.socket,
    leftover: list[bytes],
    deflate: _PerMessageDeflate | None,
) -> tuple[int, bytes]:
    parts: list[bytes] = []
    message_opcode: int | None = None
    message_rsv1 = False
    while True:
        fin, opcode, payload, rsv1 = _read_frame(sock, leftover)
        if opcode == 0x8:
            raise _WebSocketClosed("server sent a close frame")
        if opcode == 0x9:
            send_frame(sock, payload, opcode=0xA)
            continue
        if opcode == 0xA:
            continue
        if opcode != 0x0:
            message_opcode = opcode
            message_rsv1 = rsv1
        parts.append(payload)
        if fin:
            full_payload = b"".join(parts)
            if message_rsv1 and deflate is not None:
                full_payload = deflate.decompress(full_payload)
            return message_opcode if message_opcode is not None else 0x1, full_payload


def _handle_packet(packet: dict, slot_name: str, password: str | None) -> None:
    cmd = packet.get("cmd")

    if cmd == "RoomInfo":
        version = packet.get("version", {})
        _log(
            f"<- RoomInfo (server version {version.get('major', '?')}."
            f"{version.get('minor', '?')}, seed={packet.get('seed_name', '?')})"
        )
        send_json({
            "cmd": "Connect",
            "password": password,
            "name": slot_name,
            "version": CLIENT_VERSION,
            "tags": ["AP"],
            "items_handling": 0b111,
            "uuid": uuid.getnode(),
            "game": GAME_NAME,
            "slot_data": False,
        })

    elif cmd == "Connected":
        _log(
            f"<- Connected as slot {packet.get('slot')} (team {packet.get('team')}), "
            f"{len(packet.get('missing_locations', []))} locations known"
        )

    elif cmd == "ConnectionRefused":
        _log(f"<- ConnectionRefused: {packet.get('errors')}")

    elif cmd == "ReceivedItems":
        items = packet.get("items", [])
        _log(f"<- ReceivedItems: {len(items)} item(s), starting at index {packet.get('index')}")
        for item in items:
            _log(
                f"     item id={item.get('item')} location={item.get('location')} "
                f"player={item.get('player')} flags={item.get('flags', 0)}"
            )

    elif cmd == "PrintJSON":
        text = "".join(part.get("text", "") for part in packet.get("data", []))
        _log(f"<- PrintJSON: {text}")

    elif cmd == "Print":
        _log(f"<- Print: {packet.get('text')}")

    elif cmd == "InvalidPacket":
        _log(f"<- InvalidPacket ({packet.get('type')}): {packet.get('text')}")

    else:
        _log(f"<- {cmd}: {packet}")


def _connect_and_handshake(scheme: str, host: str, port: int, path: str) -> tuple[socket.socket, list[bytes], "_PerMessageDeflate | None"]:
    raw_sock = socket.create_connection((host, port), timeout=10)
    sock = raw_sock
    if scheme == "wss":
        if ssl is None:
            raw_sock.close()
            raise RuntimeError("the 'ssl' module isn't available, can't use wss://")
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(raw_sock, server_hostname=host)

    sock.settimeout(10)
    try:
        leftover, deflate = _ws_handshake(sock, host, port, path)
    except Exception:
        sock.close()
        raise
    sock.settimeout(None)
    return sock, leftover, deflate


def _run_connection(address: str, slot_name: str, password: str | None) -> None:
    explicit_scheme = "://" in address
    parsed = urlparse(address if explicit_scheme else f"ws://{address}")
    host = parsed.hostname
    path = parsed.path or "/"

    if host is None:
        _log(f"Couldn't parse an address out of {address!r}.")
        return

    schemes_to_try = [parsed.scheme] if explicit_scheme else ["ws", "wss"]

    sock: socket.socket | None = None
    leftover: list[bytes] = []
    deflate: _PerMessageDeflate | None = None
    for scheme in schemes_to_try:
        port = parsed.port or (443 if scheme == "wss" else 80)
        _log(f"Trying {scheme}://{host}:{port}{path} as {slot_name!r} ...")
        try:
            sock, leftover, deflate = _connect_and_handshake(scheme, host, port, path)
            _log(f"Connected via {scheme}://, waiting for RoomInfo.")
            break
        except Exception as e:
            _log(f"{scheme}:// attempt failed: {e!r}")

    if sock is None:
        _log("Couldn't connect via ws:// or wss:// - giving up.")
        return

    register_active_socket(sock)

    try:
        while not _state.stop_requested:
            opcode, payload = _read_message(sock, leftover, deflate)
            if opcode != 0x1:
                continue
            for packet in json.loads(payload.decode("utf-8")):
                _handle_packet(packet, slot_name, password)
    except _WebSocketClosed:
        _log("Connection closed.")
    except Exception as e:
        _log(f"Connection error: {e!r}")
    finally:
        clear_active_socket()
        try:
            sock.close()
        except OSError:
            pass
        _log("Disconnected.")


@command(description="Connect to an Archipelago server (logging only, no game hooks yet)")
def ap_connect(args: argparse.Namespace) -> None:
    if _state.thread and _state.thread.is_alive():
        _log("Already connected or connecting - run ap_disconnect first.")
        return

    _state.stop_requested = False
    _state.thread = threading.Thread(
        target=_run_connection,
        args=(args.address, args.slot_name, args.password),
        name="ArchipelagoConnection",
        daemon=True,
    )
    _state.thread.start()


ap_connect.add_argument("address", help="Server address, e.g. archipelago.gg:38281 or wss://archipelago.gg:38281")
ap_connect.add_argument("slot_name", help="Your slot name")
ap_connect.add_argument("password", nargs="?", default=None, help="Room password, if any")


@command(description="Disconnect from the Archipelago server")
def ap_disconnect(args: argparse.Namespace) -> None:
    if not (_state.thread and _state.thread.is_alive()):
        _log("Not connected.")
        return
    _state.stop_requested = True
    sock = get_active_socket()
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
    _log("Disconnect requested.")


mod = build_mod()
