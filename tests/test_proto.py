# -*- coding: utf-8 -*-
"""Wire codec and handshake unit tests (no network required)."""
import struct

import pytest

from hdcutils._proto import (
    CMD_CHECK_SERVER,
    build_client_handshake,
    decode_frame,
    encode_frame,
    parse_server_handshake,
    strip_message_prefix,
)
from hdcutils.exceptions import HdcProtocolError


def test_client_handshake_layout():
    data = build_client_handshake("SER123")
    assert len(data) == 44
    assert data[:8] == b"OHOS HDC"
    assert data[8:10] == b"\x00\x00"
    assert data[10:11] == b"K"
    assert data[11:12] == b"H"
    assert data[12:18] == b"SER123"
    assert data[18:44] == b"\x00" * 26


def test_client_handshake_any():
    data = build_client_handshake("any")
    assert data[12:15] == b"any"
    assert data[15:44] == b"\x00" * 29


def test_client_handshake_too_long():
    import pytest
    with pytest.raises(HdcProtocolError):
        build_client_handshake("x" * 40)


def test_parse_server_handshake_44():
    banner = b"OHOS HDC\x00\x00KH"
    raw = banner + struct.pack(">I", 0x1234) + b"\x00" * 28
    channel_id, version = parse_server_handshake(raw)
    assert channel_id == 0x1234
    assert version is None


def test_parse_server_handshake_108_with_version():
    banner = b"OHOS HDC\x00\x00KH"
    version = b"hdc 5.0.2 #hash"
    raw = banner + struct.pack(">I", 1) + b"\x00" * 28
    raw += version + b"\x00" * (64 - len(version))
    assert len(raw) == 108
    channel_id, version = parse_server_handshake(raw)
    assert channel_id == 1
    assert version == "hdc 5.0.2 #hash"


def test_parse_server_handshake_bad_banner():
    raw = b"NOTHDC!!\x00\x00\x00\x00" + struct.pack(">I", 1) + b"\x00" * 28
    with __import__("pytest").raises(HdcProtocolError):
        parse_server_handshake(raw)


def test_parse_server_handshake_short():
    with __import__("pytest").raises(HdcProtocolError):
        parse_server_handshake(b"OHOS HDC" + b"\x00" * 10)


def test_frame_roundtrip():
    for payload in (b"", b"a", b"hello world" * 100, bytes(range(256))):
        buf = encode_frame(payload)
        out, rest = decode_frame(buf)
        assert out == payload
        assert rest == b""


def test_frame_zero_size_accepted():
    # zero-length frames are allowed (skipped by FrameReader, defensively)
    out, rest = decode_frame(struct.pack(">I", 0))
    assert out == b""
    assert rest == b""


def test_strip_message_prefix():
    assert strip_message_prefix("[Fail]No device connected\r\n") == ("fail", "No device connected")
    assert strip_message_prefix("[Info]hello\r\n") == ("info", "hello")
    assert strip_message_prefix("plain\r\n") == ("ok", "plain")
    assert strip_message_prefix("[Success]done\r\n") == ("ok", "[Success]done")


def test_checkserver_cmd_number():
    # the mock checkserver frame: u16 LE command number + version text
    payload = struct.pack("<H", CMD_CHECK_SERVER) + b"hdc mock"
    assert struct.unpack("<H", payload[:2])[0] == 13
