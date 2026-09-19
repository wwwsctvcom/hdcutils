# -*- coding: utf-8 -*-
"""Socket connection and command-session management for the hdc server.

Lifecycle model (identical to the official hdc client):

* one TCP connection carries exactly one command (as the official CLI does);
* daemon-side commands (shell/hilog etc.) end with the daemon sending
  ``CMD_KERNEL_CHANNEL_CLOSE``, after which the server closes the client TCP
  connection -- **EOF marks command completion**;
* local commands (list targets / tconn / fport ls / checkserver ...) keep the
  connection open after their response; the official CLI simply exits the
  process, this library decides completion with an idle window.
"""
from __future__ import annotations

import socket
import time
from typing import Iterator, Optional

from ._proto import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    MAX_FRAME_SIZE,
    build_client_handshake,
    encode_frame,
    parse_server_handshake,
)
from .exceptions import HdcProtocolError, HdcServerError, HdcTimeoutError

__all__ = ["HdcChannel", "ShellSession", "execute", "stream", "DEFAULT_IDLE_WINDOW"]

DEFAULT_IDLE_WINDOW = 0.5  # idle decision window after a local-command response
DEFAULT_FIRST_TIMEOUT = 10.0  # first-frame wait cap (tconn needs several seconds)
DEFAULT_CONNECT_TIMEOUT = 3.0
SERVER_HANDSHAKE_MIN = 44
SERVER_HANDSHAKE_FULL = 108


class _FrameReader:
    """Frame reader: assembles the 4-byte big-endian length header + payload."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    def read_frame(self, timeout: Optional[float]) -> Optional[bytes]:
        """Read the next frame payload.

        Returns ``None`` on peer close (EOF); raises :class:`HdcTimeoutError`
        on timeout. Zero-length frames are skipped defensively (a healthy
        server never sends them).
        """
        while True:
            if len(self._buf) >= 4:
                size = int.from_bytes(self._buf[:4], "big")
                if size > MAX_FRAME_SIZE:
                    raise HdcProtocolError("invalid frame size: %d" % size)
                if len(self._buf) >= 4 + size:
                    payload = self._buf[4 : 4 + size]
                    self._buf = self._buf[4 + size :]
                    if size == 0:
                        continue  # skip empty frames
                    return payload
                # incomplete frame: fall through to recv -- never spin here
            self._sock.settimeout(timeout)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                raise HdcTimeoutError("timeout waiting for frame (%.1fs)" % (timeout or -1))
            if not chunk:  # EOF: the server closed the connection (command done)
                if self._buf:
                    raise HdcProtocolError("truncated frame tail: %d bytes" % len(self._buf))
                return None
            self._buf += chunk


class HdcChannel:
    """A client connection to the hdc server (one connection, one command).

    Usage::

        with HdcChannel("127.0.0.1", 8710) as ch:
            ch.open(connect_key="any")
            ch.send_command("list targets")
            out = b"".join(ch.frames(completion="onetime"))
    """

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.handshake_drain_timeout = 0.05
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[_FrameReader] = None
        self.channel_id: int = 0
        self.server_version: Optional[str] = None

    # ---- lifecycle -------------------------------------------------
    def open(self, connect_key: str = "any") -> None:
        """Establish the TCP connection and complete the handshake (the
        official client's 44-byte reply).

        Note: servers compiled with version checking (e.g. DevEco's bundled
        hdc) send a 108-byte handshake (44 + 64 bytes of version text); the
        extra bytes must be drained or they would be misread as a frame.
        """
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except OSError as exc:
            raise HdcServerError(
                "cannot connect to hdc server %s:%s (%s)" % (self.host, self.port, exc)
            ) from exc
        self._sock = sock
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            data = self._recv_exact(SERVER_HANDSHAKE_MIN)
            extra = self._drain_handshake_extra(sock)
            if extra:
                data += extra
            self.channel_id, self.server_version = parse_server_handshake(data)
            sock.sendall(build_client_handshake(connect_key))
        except Exception:
            sock.close()
            self._sock = None
            raise
        self._reader = _FrameReader(sock)

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            try:
                self._sock.settimeout(self.connect_timeout)
                chunk = self._sock.recv(n - len(buf))
            except socket.timeout:
                raise HdcTimeoutError("timeout reading server handshake")
            if not chunk:
                raise HdcProtocolError("connection closed during handshake")
            buf += chunk
        return buf

    def _drain_handshake_extra(self, sock: socket.socket) -> bytes:
        """Drain the extra 64-byte version section of 108-byte handshakes
        (absent on servers without version checking)."""
        try:
            sock.settimeout(self.handshake_drain_timeout)
            sock.recv(1, socket.MSG_PEEK)
        except socket.timeout:
            return b""  # nothing extra: 44-byte handshake
        except OSError as exc:
            raise HdcServerError("handshake peek failed: %s" % exc) from exc
        buf = b""
        while len(buf) < SERVER_HANDSHAKE_FULL - SERVER_HANDSHAKE_MIN:
            try:
                sock.settimeout(self.connect_timeout)
                chunk = sock.recv(SERVER_HANDSHAKE_FULL - SERVER_HANDSHAKE_MIN - len(buf))
            except socket.timeout:
                break  # a partial version segment is harmless; treat as 44
            if not chunk:
                break
            buf += chunk
        return buf

    def close(self) -> None:
        sock, self._sock, self._reader = self._sock, None, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self) -> "HdcChannel":
        if self._sock is None:
            self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- sending ---------------------------------------------------
    def send_command(self, command: str) -> None:
        """Send one command (the official client appends a ``\\0``)."""
        self._require_open()
        self._sock.sendall(encode_frame(command.encode("utf-8") + b"\x00"))

    def send_frame(self, payload: bytes) -> None:
        """Send one raw frame (stdin data of the interactive shell)."""
        self._require_open()
        self._sock.sendall(encode_frame(payload))

    # ---- receiving -------------------------------------------------
    def frames(
        self,
        completion: str = "onetime",
        idle_window: float = DEFAULT_IDLE_WINDOW,
        first_timeout: float = DEFAULT_FIRST_TIMEOUT,
        overall_timeout: Optional[float] = None,
    ) -> Iterator[bytes]:
        """Yield server response frames one payload at a time.

        completion:
            * ``"onetime"`` -- single command: ends on EOF, or after
              ``idle_window`` seconds without new frames once data arrived
              (local commands keep the connection open);
            * ``"stream"`` -- streaming command: ends on EOF only (hilog /
              long shell output); the gap between frames may be arbitrarily
              long, only ``overall_timeout`` bounds the whole read.
        """
        if completion not in ("onetime", "stream"):
            raise ValueError("completion must be 'onetime' or 'stream'")
        self._require_open()
        start = time.monotonic()
        deadline = start + overall_timeout if overall_timeout is not None else None

        def check_deadline() -> float:
            if deadline is None:
                return 0.0
            left = deadline - time.monotonic()
            if left <= 0:
                raise HdcTimeoutError("overall timeout (%.1fs)" % overall_timeout)
            return left

        if completion == "stream":
            # Streaming commands may pause arbitrarily long between frames
            # (e.g. `shell sleep 60`, a quiet hilog); EOF-only completion.
            while True:
                left = check_deadline()
                payload = self._reader.read_frame(left if deadline is not None else None)
                if payload is None:  # EOF: command finished
                    return
                yield payload
            # unreachable

        # completion == "onetime"
        left = check_deadline()
        first = self._reader.read_frame(
            min(first_timeout, left) if deadline is not None else first_timeout)
        if first is None:
            return  # server closed immediately: empty response
        yield first
        while True:
            left = check_deadline()
            wait = min(idle_window, left) if deadline is not None else idle_window
            try:
                payload = self._reader.read_frame(wait)
            except HdcTimeoutError:
                return  # idle window expired: local-command response is complete
            if payload is None:
                return
            yield payload

    # ---- internals -------------------------------------------------
    def _require_open(self) -> None:
        if self._sock is None or self._reader is None:
            raise HdcServerError("channel is not open")


def execute(
    host: str,
    port: int,
    command: str,
    connect_key: str = "any",
    *,
    completion: str = "onetime",
    idle_window: float = DEFAULT_IDLE_WINDOW,
    first_timeout: float = DEFAULT_FIRST_TIMEOUT,
    timeout: Optional[float] = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> bytes:
    """Convenience entry: new connection -> handshake -> send -> collect -> close."""
    if not command:
        raise ValueError("empty command")
    ch = HdcChannel(host, port, connect_timeout=connect_timeout)
    ch.open(connect_key)
    try:
        ch.send_command(command)
        return b"".join(
            ch.frames(
                completion=completion,
                idle_window=idle_window,
                first_timeout=first_timeout,
                overall_timeout=timeout,
            )
        )
    finally:
        ch.close()


def stream(
    host: str,
    port: int,
    command: str,
    connect_key: str = "any",
    *,
    first_timeout: float = DEFAULT_FIRST_TIMEOUT,
    timeout: Optional[float] = None,
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
) -> Iterator[bytes]:
    """Streaming entry point: yields response frames until the server closes
    the connection or the timeout expires.

    Abandoning the generator (without consuming it fully) leaves the socket
    to the GC; prefer explicit ``close()`` or ``contextlib.closing``.
    """
    ch = HdcChannel(host, port, connect_timeout=connect_timeout)
    ch.open(connect_key)
    try:
        ch.send_command(command)
        yield from ch.frames(
            completion="stream", first_timeout=first_timeout, overall_timeout=timeout
        )
    finally:
        ch.close()


class ShellSession:
    """Interactive device shell (what ``hdc shell`` without arguments enters).

    The server switches to ``interactiveShellMode``: every frame payload on
    this connection is forwarded to the device as stdin, and shell output
    keeps coming back as raw frames; typing ``exit`` inside the shell ends
    the session (EOF).
    """

    def __init__(self, host: str, port: int, connect_key: str,
                 connect_timeout: float = DEFAULT_CONNECT_TIMEOUT):
        self._channel = HdcChannel(host, port, connect_timeout=connect_timeout)
        self._channel.open(connect_key)
        self._channel.send_command("shell")
        self.eof = False

    def send(self, data) -> None:
        """Write a line (or raw bytes) into the shell stdin (adds ``\\n``)."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data.endswith(b"\n"):
            data += b"\n"
        self._channel.send_frame(data)

    def recv(self, timeout: float = 1.0) -> Optional[bytes]:
        """Read the next output frame. ``None`` means timeout or EOF
        (check :attr:`eof` to tell them apart)."""
        if self.eof:
            return None
        try:
            payload = self._channel._reader.read_frame(timeout)
        except HdcTimeoutError:
            return None
        if payload is None:
            self.eof = True
            return None
        return payload

    def read_until_close(
        self, idle_window: float = DEFAULT_IDLE_WINDOW, overall_timeout: Optional[float] = None
    ) -> bytes:
        """Read until EOF; if nothing arrives for ``idle_window`` seconds,
        sends ``exit`` to finish the session."""
        chunks = []
        start = time.monotonic()
        sent_exit = False
        while True:
            wait = idle_window
            if overall_timeout is not None:
                wait = min(wait, max(overall_timeout - (time.monotonic() - start), 0.01))
            payload = self.recv(timeout=wait)
            if payload is None:
                if self.eof:
                    break
                if overall_timeout is not None and time.monotonic() - start >= overall_timeout:
                    break
                if not sent_exit:
                    # idle timeout: a live shell should respond promptly,
                    # send exit to end the session
                    self.send("exit")
                    sent_exit = True
                    payload = self.recv(timeout=idle_window * 2)
                    if payload:
                        chunks.append(payload)
                    if self.eof:
                        break
                    self.close()
                    self.eof = True
                    break
                break
            chunks.append(payload)
            sent_exit = False
        return b"".join(chunks)

    def close(self) -> None:
        self._channel.close()

    def __enter__(self) -> "ShellSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
