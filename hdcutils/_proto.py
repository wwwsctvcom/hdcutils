# -*- coding: utf-8 -*-
"""Wire codec for the hdc host-client <-> hdc-server socket protocol.

The protocol was reverse-engineered from the open-source hdc implementation
(there is no official wire documentation):

* open-source sources: https://github.com/openharmony/developtools_hdc
  - ``src/common/channel.h``      : ``ChannelHandShake`` packed struct
  - ``src/common/channel.cpp``    : ``ReadStream`` 4-byte big-endian length framing
  - ``src/common/define.h``       : banner / tag / port constants
  - ``src/host/server_for_client.cpp`` : server-side handshake checks and replies
* third-party protocol write-up: https://github.com/Attect/muka_rust_hdc
  (``docs/HDC_SERVER_SOCKET_PROTOCOL.md``; a Rust re-implementation verified
  that the protocol can be implemented independently)

Summary:

    1. The server listens on ``127.0.0.1:8710`` (configurable through the
       ``HDC_SERVER_PORT`` / ``OHOS_HDC_SERVER_PORT`` environment variables).
    2. After TCP connect the server sends its handshake first; the client
       replies with 44 bytes (banner echo + 32-byte connectKey, zero padded).
       Official clients compiled without version checking send exactly
       ``offsetof(ChannelHandShake, version)`` = 44 bytes.
    3. Afterwards both directions use frames of ``[4-byte big-endian
       length][payload]`` (the length excludes the 4-byte header).
    4. The client's first frame payload is the command text (the official
       client appends a ``\\0``).
    5. Responses are text frames (``[Fail]/[Info]`` prefix + ``\\r\\n``), raw
       data frames (shell output), or structured frames with a u16
       little-endian command prefix (checkserver / file / app tasks).
    6. Command lifetime: daemon-side commands (shell etc.) end with the
       daemon sending ``CMD_KERNEL_CHANNEL_CLOSE`` after which the server
       closes the client TCP connection -- EOF marks completion; local
       commands (list targets etc.) keep the connection open and need an
       idle-window decision.
"""
from __future__ import annotations

import struct
from typing import Optional, Tuple

from .exceptions import HdcProtocolError, HdcTimeoutError

__all__ = [
    "DEFAULT_PORT",
    "DEFAULT_HOST",
    "BANNER",
    "HUGE_BUF_TAG",
    "SERVICE_KILL_TAG",
    "SERVER_HANDSHAKE_MIN",
    "SERVER_HANDSHAKE_FULL",
    "CLIENT_HANDSHAKE_SIZE",
    "MAX_FRAME_SIZE",
    "build_client_handshake",
    "parse_server_handshake",
    "encode_frame",
    "decode_frame",
    "FrameStream",
    "strip_message_prefix",
    "MSG_FAIL",
    "MSG_INFO",
    "MSG_EMPTY",
    "CMD_CHECK_SERVER",
    "CMD_KERNEL_CHANNEL_CLOSE",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8710  # src/common/define.h: constexpr uint16_t DEFAULT_PORT = 8710

# Server handshake banner: strcpy_s(banner[12], "OHOS HDC") writes 9 bytes,
# the rest is zero-filled; server_for_client.cpp then sets banner[11]='H'
# (HUGE_BUF_TAG) and banner[10]='K' (SERVICE_KILL_TAG).
BANNER = b"OHOS HDC"
HUGE_BUF_TAG = b"H"  # banner[11], BANNER_FEATURE_TAG_OFFSET
SERVICE_KILL_TAG = b"K"  # banner[10], SERVICE_KILL_OFFSET

SERVER_HANDSHAKE_MIN = 44  # banner[12] + union(connectKey[32])
SERVER_HANDSHAKE_FULL = 108  # + version[64] (version-check builds)
CLIENT_HANDSHAKE_SIZE = 44  # offsetof(ChannelHandShake, version)

# src/common/define.h: HDC_BUF_MAX_BYTES = INT_MAX, narrowed defensively here
MAX_FRAME_SIZE = 0x40000000 // 2  # 1 GiB

MSG_FAIL = "[Fail]"
MSG_INFO = "[Info]"
MSG_EMPTY = "[Empty]"

# define_enum.h::HdcCommand (the subset a client needs)
CMD_KERNEL_CHANNEL_CLOSE = 2
CMD_CHECK_SERVER = 13


def build_client_handshake(connect_key: str = "any") -> bytes:
    """Build the 44-byte client -> server handshake.

    Mirrors the official client's ``FillConnectKeyAndCheckVersion`` without
    version checking: banner[12] (echoing the server banner) + connectKey[32]
    zero-padded.
    """
    key_bytes = (connect_key or "any").encode("utf-8")
    if len(key_bytes) >= 32:
        raise HdcProtocolError("connect key too long (max 31 bytes): %r" % connect_key)
    banner = BANNER + b"\x00\x00" + SERVICE_KILL_TAG + HUGE_BUF_TAG  # 12 bytes
    assert len(banner) == 12
    return banner + key_bytes + b"\x00" * (32 - len(key_bytes))


def parse_server_handshake(data: bytes) -> Tuple[int, Optional[str]]:
    """Parse the server -> client handshake (accepts both 44/108 variants).

    Returns ``(channel_id, version|None)``; channel_id is a big-endian u32.
    """
    if len(data) < SERVER_HANDSHAKE_MIN:
        raise HdcProtocolError(
            "server handshake too short: %d bytes (need >= %d)" % (len(data), SERVER_HANDSHAKE_MIN)
        )
    if data[: len(BANNER)] != BANNER:
        raise HdcProtocolError("bad server handshake banner: %r" % data[:12])
    channel_id = struct.unpack(">I", data[12:16])[0]
    version = None
    if len(data) >= SERVER_HANDSHAKE_FULL:
        raw_version = data[44:SERVER_HANDSHAKE_FULL]
        version = raw_version.split(b"\x00", 1)[0].decode("utf-8", "replace") or None
    return channel_id, version


def encode_frame(payload: bytes) -> bytes:
    """Frame encoding: ``[4-byte big-endian length][payload]`` (header excluded)."""
    if len(payload) > MAX_FRAME_SIZE:
        raise HdcProtocolError("frame payload too large: %d" % len(payload))
    return struct.pack(">I", len(payload)) + payload


def decode_frame(buf: bytes) -> Tuple[bytes, bytes]:
    """Extract one frame from a buffer. Returns ``(payload, rest)``; raises
    :class:`ValueError` when the buffer does not hold a full frame."""
    if len(buf) < 4:
        raise ValueError("frame header incomplete")
    (size,) = struct.unpack(">I", buf[:4])
    if size > MAX_FRAME_SIZE:
        raise HdcProtocolError("invalid frame size: %d" % size)
    if len(buf) < 4 + size:
        raise ValueError("frame payload incomplete")
    return buf[4 : 4 + size], buf[4 + size :]


def strip_message_prefix(text: str) -> Tuple[str, str]:
    """Strip the ``[Fail]``/``[Info]`` response prefixes.

    Returns ``(level, text)`` where level is ``"ok"/"info"/"fail"`` and text
    is the trimmed remainder. ``[Empty]`` is kept as-is (not a level prefix).
    """
    text = text.strip()
    if text.startswith(MSG_FAIL):
        return "fail", text[len(MSG_FAIL) :].strip()
    if text.startswith(MSG_INFO):
        return "info", text[len(MSG_INFO) :].strip()
    return "ok", text
