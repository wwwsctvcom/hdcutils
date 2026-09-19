# -*- coding: utf-8 -*-
"""Python implementation of the hdc Struct serialization (protobuf-style).

hdc's file/app transfer tasks serialize structured messages with
``SerialStruct`` (``src/common/serial_struct_define.h``): standard protobuf
wire format -- ``[tag<<3 | wire_type][varint / length-delimited]``, fields
written unconditionally in declaration order.

The three messages used here (sources: ``src/common/serial_struct.h``):

* ``TransferConfig`` -- the CHECK frame of file send/recv and app install:
      1 fileSize(u64) 2 atime(u64) 3 mtime(u64) 4 options(str) 5 path(str)
      6 optionalName(str) 7 updateIfNew(bool) 8 compressType(u8)
      9 holdTimestamp(bool) 10 functionName(str) 11 clientCwd(str)
      12 reserve1(str) 13 reserve2(str)
* ``FileMode`` -- -m permission sync (unused, parsing kept): 1 perm 2 uId
      3 gId 4 context 5 fullName
* ``TransferPayload`` -- DATA frame header: 1 index(u64) 2 compressType
      3 compressSize 4 uncompressSize (u32 varints)
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "TRANSFER_PAYLOAD_PREFIX",
    "TransferConfig",
    "TransferPayload",
    "serialize_message",
    "parse_message",
]

TRANSFER_PAYLOAD_PREFIX = 64  # transfer.cpp: payloadPrefixReserve


def _encode_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError("varint requires non-negative value")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _decode_varint(data: bytes, offset: int):
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, offset
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def _tag(field_no: int, wire_type: int) -> bytes:
    return _encode_varint((field_no << 3) | wire_type)


def serialize_message(fields: list) -> bytes:
    """Encode ``[(field_no, value), ...]`` in protobuf wire format.

    value: int -> varint (wire type 0); bytes/str -> length-delimited
    (wire type 2). hdc's WriteMessage writes every field unconditionally
    (unlike proto3 zero-skipping).
    """
    out = bytearray()
    for field_no, value in fields:
        if isinstance(value, bool):
            out += _tag(field_no, 0)
            out += _encode_varint(int(value))
        elif isinstance(value, int):
            out += _tag(field_no, 0)
            out += _encode_varint(value)
        elif isinstance(value, str):
            raw = value.encode("utf-8")
            out += _tag(field_no, 2)
            out += _encode_varint(len(raw))
            out += raw
        elif isinstance(value, (bytes, bytearray)):
            out += _tag(field_no, 2)
            out += _encode_varint(len(value))
            out += bytes(value)
        else:
            raise TypeError("unsupported field type: %r" % type(value))
    return bytes(out)


def parse_message(data: bytes, fields: dict) -> dict:
    """Parse protobuf wire format into ``{field_no: value}``.

    ``fields``: ``{field_no: "varint"|"bytes"}``; unknown fields are skipped
    (same as the C++ parser).
    """
    result: dict = {}
    offset = 0
    while offset < len(data):
        tag_key, offset = _decode_varint(data, offset)
        field_no = tag_key >> 3
        wire_type = tag_key & 0x07
        if field_no not in fields:
            if wire_type == 0:
                _, offset = _decode_varint(data, offset)
            elif wire_type == 2:
                size, offset = _decode_varint(data, offset)
                offset += size
            elif wire_type == 1:
                offset += 8
            elif wire_type == 5:
                offset += 4
            else:
                raise ValueError("unsupported wire type %d" % wire_type)
            continue
        if wire_type == 0:
            value, offset = _decode_varint(data, offset)
        elif wire_type == 2:
            size, offset = _decode_varint(data, offset)
            value = data[offset : offset + size]
            offset += size
        else:
            raise ValueError("unexpected wire type %d for field %d" % (wire_type, field_no))
        result[field_no] = value
    return result


@dataclass
class TransferConfig:
    """``HdcTransferBase::TransferConfig`` (serial_struct.h fields 1-13)."""

    file_size: int = 0  # 1
    atime: int = 0  # 2 ns
    mtime: int = 0  # 3 ns
    options: str = ""  # 4
    path: str = ""  # 5 peer path (file) / empty (app)
    optional_name: str = ""  # 6
    update_if_new: bool = False  # 7 (-sync)
    compress_type: int = 0  # 8 (-z, only 0=NONE supported)
    hold_timestamp: bool = False  # 9 (-a)
    function_name: str = ""  # 10
    client_cwd: str = ""  # 11
    reserve1: str = ""  # 12 bundleName (-b sandbox)
    reserve2: str = ""  # 13

    def serialize(self) -> bytes:
        return serialize_message([
            (1, self.file_size),
            (2, self.atime),
            (3, self.mtime),
            (4, self.options),
            (5, self.path),
            (6, self.optional_name),
            (7, self.update_if_new),
            (8, self.compress_type),
            (9, self.hold_timestamp),
            (10, self.function_name),
            (11, self.client_cwd),
            (12, self.reserve1),
            (13, self.reserve2),
        ])

    @classmethod
    def parse(cls, data: bytes) -> "TransferConfig":
        raw = parse_message(data, {1: "varint", 2: "varint", 3: "varint", 4: "bytes",
                                   5: "bytes", 6: "bytes", 7: "varint", 8: "varint",
                                   9: "varint", 10: "bytes", 11: "bytes", 12: "bytes",
                                   13: "bytes"})
        return cls(
            file_size=raw.get(1, 0),
            atime=raw.get(2, 0),
            mtime=raw.get(3, 0),
            options=raw.get(4, b"").decode("utf-8", "replace"),
            path=raw.get(5, b"").decode("utf-8", "replace"),
            optional_name=raw.get(6, b"").decode("utf-8", "replace"),
            update_if_new=bool(raw.get(7, 0)),
            compress_type=raw.get(8, 0),
            hold_timestamp=bool(raw.get(9, 0)),
            function_name=raw.get(10, b"").decode("utf-8", "replace"),
            client_cwd=raw.get(11, b"").decode("utf-8", "replace"),
            reserve1=raw.get(12, b"").decode("utf-8", "replace"),
            reserve2=raw.get(13, b"").decode("utf-8", "replace"),
        )


@dataclass
class TransferPayload:
    """``HdcTransferBase::TransferPayload`` -- the 64-byte DATA frame header
    (protobuf bytes + NUL padding)."""

    index: int = 0
    compress_type: int = 0
    compress_size: int = 0
    uncompress_size: int = 0

    def serialize(self) -> bytes:
        """Encode and NUL-pad to 64 bytes (the SendIOPayload head convention)."""
        body = serialize_message([
            (1, self.index),
            (2, self.compress_type),
            (3, self.compress_size),
            (4, self.uncompress_size),
        ])
        if len(body) + 1 > TRANSFER_PAYLOAD_PREFIX:
            raise ValueError("transfer payload header too large")
        return body + b"\x00" * (TRANSFER_PAYLOAD_PREFIX - len(body))

    @classmethod
    def parse(cls, data: bytes) -> "TransferPayload":
        if len(data) < TRANSFER_PAYLOAD_PREFIX:
            raise ValueError("transfer payload header too short")
        raw = parse_message(data[:TRANSFER_PAYLOAD_PREFIX],
                            {1: "varint", 2: "varint", 3: "varint", 4: "varint"})
        return cls(index=raw.get(1, 0), compress_type=raw.get(2, 0),
                   compress_size=raw.get(3, 0), uncompress_size=raw.get(4, 0))
