# -*- coding: utf-8 -*-
"""Core of hdcutils: the ``HdcClient``, modeled after ``adbutils.AdbClient``.

**Every device operation goes through a plain socket connection to the hdc
server (default ``127.0.0.1:8710``) -- no hdc.exe involved**, so binary
version or install location differences cannot break it. The hdc executable
is used in exactly one scenario: pulling up the server process when it is
absent (USB transport physically lives in that process). ``kill_server``
replicates the official ``hdc kill`` PID-file mechanism in pure Python.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Iterator, List, Optional

from ._connection import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_FIRST_TIMEOUT,
    DEFAULT_IDLE_WINDOW,
    execute as _sock_execute,
    stream as _sock_stream,
)
from ._device import HdcDevice
from ._proto import DEFAULT_HOST, DEFAULT_PORT, strip_message_prefix
from ._server import (
    ensure_server,
    find_hdc_binary,
    kill_server as _kill_server_pid,
    probe_server,
    start_server as _start_server,
)
from .exceptions import (
    HdcCommandError,
    HdcDeviceNotFoundError,
    HdcError,
    HdcProtocolError,
    HdcServerError,
    HdcTimeoutError,
)

__all__ = ["HdcClient", "TargetInfo", "ENV_SERVER_PORT_VARS"]

logger = logging.getLogger("hdcutils")

ENV_SERVER_PORT_VARS = ("HDCUTILS_HDC_SERVER_PORT", "HDC_SERVER_PORT", "OHOS_HDC_SERVER_PORT")
_CMD_CHECK_SERVER = 13  # define_enum.h:CMD_CHECK_SERVER

# Commands that stream until the daemon closes the channel; everything else
# completes after the response burst (idle-window detection)
_STREAM_COMMANDS = ("shell", "hilog", "bugreport", "wait")

# These local commands answer with a single EchoClient frame (verified in
# server_for_client.cpp: GetTargetList / ReportServerVersion /
# OrderConnecTargetResult). The connection stays open afterwards, so we use a
# narrow idle window -- done right after the first frame, no lingering socket.
_SINGLE_RESPONSE_COMMANDS = ("list targets", "checkserver", "tconn")
_SINGLE_FRAME_IDLE = 0.08  # seconds; covers extra frames coalesced into the burst


class TargetInfo:
    """One device parsed from ``list targets -v``."""

    __slots__ = ("connect_key", "type", "state", "addr")

    def __init__(self, connect_key: str, type: str = "", state: str = "", addr: str = ""):
        self.connect_key = connect_key
        self.type = type
        self.state = state
        self.addr = addr

    @property
    def is_connected(self) -> bool:
        """Only ``Connected`` counts as usable.

        The official ``list targets -v`` also reports UART/COM probe entries
        and unauthorized devices as ``Ready`` -- those are not usable targets
        (device()/wait() must ignore them; the official
        non-verbose list excludes them too).
        """
        return self.state.lower() == "connected"

    def __str__(self) -> str:
        line = "%s %s %s" % (self.connect_key, self.type, self.state)
        if self.addr:
            line += " %s" % self.addr
        return line

    def __repr__(self) -> str:
        return "TargetInfo(connect_key=%r, type=%r, state=%r, addr=%r)" % (
            self.connect_key, self.type, self.state, self.addr)


def resolve_port(port: Optional[int]) -> int:
    """Port resolution: explicit arg > env vars > 8710 (the official default)."""
    if port:
        return int(port)
    for name in ENV_SERVER_PORT_VARS:
        value = os.environ.get(name)
        if value:
            try:
                return int(value)
            except ValueError:
                logger.warning("invalid %s=%r, ignored", name, value)
    return DEFAULT_PORT


class HdcClient:
    """hdc server client (pure socket; hdc.exe only pulls up the server).

    Methods are named after the official hdc commands: ``list targets``,
    ``wait``, ``tconn``, ``checkserver``, ``start``, ``kill``, ``tmode``,
    ``fport ls``.

    Args:
        host: hdc server address, default ``127.0.0.1``.
        port: hdc server port; defaults to the ``HDC_SERVER_PORT`` /
            ``OHOS_HDC_SERVER_PORT`` environment variable, otherwise 8710.
        hdc_path: path to the hdc executable (**only used to pull up the
            server when it is not running**); resolution order: the
            ``HDCUTILS_HDC_PATH`` environment variable > PATH > common
            DevEco install locations.
        auto_start: pull the server up automatically when it is not running.
    """

    SERVER_LIVE_CACHE = 30.0  # seconds; "server known alive" probe cache

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: Optional[int] = None,
        hdc_path: Optional[str] = None,
        auto_start: bool = True,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ):
        self.host = host
        self.port = resolve_port(port)
        self._hdc_path = hdc_path
        self._hdc_binary: Optional[str] = None
        self.auto_start = auto_start
        self.connect_timeout = connect_timeout
        self._server_ok_until: float = 0.0

    # ---------------------------------------------------------------
    # Basics
    # ---------------------------------------------------------------
    @property
    def hdc_path(self) -> Optional[str]:
        """Resolved hdc executable path (only used to pull up the server)."""
        if self._hdc_binary is None:
            self._hdc_binary = find_hdc_binary(self._hdc_path)
        return self._hdc_binary

    def __repr__(self) -> str:
        return "HdcClient(host=%r, port=%r)" % (self.host, self.port)

    def __enter__(self) -> "HdcClient":
        return self

    def __exit__(self, *exc) -> None:
        pass

    # ---------------------------------------------------------------
    # Server management
    # ---------------------------------------------------------------
    def server_version(self) -> str:
        """Return the hdc server version (``checkserver``, socket direct).

        The response frame carries a u16 little-endian command prefix
        (CMD_CHECK_SERVER = 13) which is stripped here; plain-text variants
        are handled too.
        """
        payload = self._execute("checkserver", check_fail=False)
        if len(payload) > 2:
            cmd = int.from_bytes(payload[:2], "little")
            if cmd == _CMD_CHECK_SERVER:
                return payload[2:].split(b"\x00", 1)[0].strip().decode("utf-8", "replace")
        return payload.strip().decode("utf-8", "replace")

    def start_server(self, timeout: float = 15.0) -> None:
        """Pull up the hdc server (locates the binary, triggers the official
        ``PullupServer`` mechanism).

        The server process itself must be provided by an official binary
        (it carries the USB transport); no device operation uses the binary.
        """
        binary = self.hdc_path
        if not binary:
            raise HdcError(
                "hdc binary not found; install HarmonyOS Command Line Tools "
                "or set HDCUTILS_HDC_PATH"
            )
        _start_server(binary, self.port, timeout=timeout)

    def kill_server(self, timeout: float = 10.0) -> None:
        """Kill the hdc server -- replicates ``hdc kill`` (PID file / port
        lookup + SIGKILL), **without invoking hdc.exe**."""
        _kill_server_pid(self.port, timeout=timeout)

    def restart_server(self, timeout: float = 15.0) -> None:
        try:
            self.kill_server()
        except HdcError:
            pass
        time.sleep(0.5)
        self.start_server(timeout=timeout)

    # ---------------------------------------------------------------
    # Devices
    # ---------------------------------------------------------------
    def list_targets(self, verbose: bool = False) -> List:
        """List devices.

        Returns ``list[str]`` (connect keys only); ``verbose=True`` returns
        :class:`TargetInfo` entries.
        """
        command = "list targets -v" if verbose else "list targets"
        raw = self._execute(command, check_fail=False)
        return _parse_list_targets(raw, verbose)

    def device(self, serial: Optional[str] = None) -> HdcDevice:
        """Return a device handle.

        Without ``serial``: exactly one device -> return it; multiple ->
        error listing them; none -> :class:`HdcDeviceNotFoundError`
        (adbutils semantics).
        """
        if serial:
            return HdcDevice(self, serial)
        targets = self.list_targets()
        if not targets:
            raise HdcDeviceNotFoundError("no device connected (hdc list targets is empty)")
        if len(targets) > 1:
            raise HdcError(
                "multiple devices connected (%s); specify serial via device(serial=...)"
                % ", ".join(targets[:5])
            )
        return HdcDevice(self, targets[0])

    def device_list(self) -> List[HdcDevice]:
        """Return :class:`HdcDevice` handles for every connected device."""
        return [HdcDevice(self, key) for key in self.list_targets()]

    def connect(self, addr: str, timeout: float = 15.0) -> str:
        """Connect a network device (``hdc tconn <ip:port>``)."""
        return self._execute(
            "tconn %s" % addr, check_fail=False, first_timeout=timeout, timeout=timeout
        ).strip().decode("utf-8", "replace")

    def disconnect(self, addr: str) -> str:
        """Disconnect a network device (``hdc tconn <addr> -remove``)."""
        return self._execute(
            "tconn %s -remove" % addr, check_fail=False
        ).strip().decode("utf-8", "replace")

    def wait(
        self, serial: Optional[str] = None, timeout: float = 30.0, poll_interval: float = 1.0
    ) -> HdcDevice:
        """``hdc wait``: block until a device is ready, then return it.

        Polls ``list targets -v`` (only ``Connected`` targets count; UART/COM
        probe entries reported as ``Ready`` are ignored).
        """
        deadline = time.monotonic() + timeout
        while True:
            try:
                targets = self.list_targets(verbose=True)
            except HdcError:
                targets = []
            ready = [
                t for t in targets
                if t.is_connected and (serial is None or t.connect_key == serial)
            ]
            if ready:
                return HdcDevice(self, ready[0].connect_key)
            if time.monotonic() >= deadline:
                raise HdcTimeoutError(
                    "wait%s timed out after %.0fs" % (
                        "(%s)" % serial if serial else "", timeout)
                )
            time.sleep(poll_interval)

    # ---------------------------------------------------------------
    # Execution
    # ---------------------------------------------------------------
    def _ensure(self) -> None:
        """Ensure the server is online (with a liveness cache so ordinary
        commands do not open a throwaway probe connection first)."""
        now = time.monotonic()
        if now < self._server_ok_until:
            return
        if probe_server(self.host, self.port, timeout=1.0):
            self._server_ok_until = now + self.SERVER_LIVE_CACHE
            return
        if self.auto_start:
            ensure_server(self.host, self.port, hdc_path=self._hdc_path)
            self._server_ok_until = now + self.SERVER_LIVE_CACHE

    def _execute(
        self,
        command: str,
        serial: Optional[str] = None,
        completion: Optional[str] = None,
        check_fail: bool = True,
        timeout: Optional[float] = None,
        idle_window: Optional[float] = None,
        first_timeout: float = DEFAULT_FIRST_TIMEOUT,
    ) -> bytes:
        """Single entry point (pure socket; the command text is exactly the
        official CLI syntax).

        Short-connection model: one TCP connection per command, closed as
        soon as the response is complete.

        * **streaming commands** (shell/hilog ...): the server closes the
          connection when the daemon finishes -- EOF means done;
        * **local commands**: the server keeps the connection open after the
          response. Hot-path commands (list targets / checkserver / tconn)
          use an 80ms narrow window (done on first frame); other local
          commands fall back to a 0.5s idle window (override with
          ``idle_window``).
        """
        if completion is None:
            head = command.split(" ", 1)[0]
            completion = "stream" if head in _STREAM_COMMANDS else "onetime"
        if idle_window is None:
            idle_window = (
                _SINGLE_FRAME_IDLE
                if command.startswith(_SINGLE_RESPONSE_COMMANDS)
                else DEFAULT_IDLE_WINDOW
            )
        self._ensure()
        payload = _sock_execute(
            self.host, self.port, command, serial or "any",
            completion=completion, timeout=timeout,
            idle_window=idle_window, first_timeout=first_timeout,
            connect_timeout=self.connect_timeout,
        )
        self._server_ok_until = time.monotonic() + self.SERVER_LIVE_CACHE
        if check_fail:
            _raise_on_fail(payload, command)
        return payload

    def stream_command(self, command: str, serial: Optional[str] = None,
                       timeout: Optional[float] = None) -> Iterator[bytes]:
        """Run a command and yield raw chunks until the server closes the
        connection (used by hilog and friends)."""
        self._ensure()
        yield from _sock_stream(
            self.host, self.port, command, serial or "any",
            timeout=timeout, connect_timeout=self.connect_timeout,
        )


def _raise_on_fail(payload: bytes, command: str) -> None:
    text = payload.decode("utf-8", "replace")
    level, message = strip_message_prefix(text)
    if level == "fail":
        raise HdcCommandError(message or "command failed: %s" % command, output=text)


def _parse_list_targets(raw: bytes, verbose: bool) -> List:
    text = raw.decode("utf-8", "replace")
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    lines = [line for line in lines if line and not line.startswith("[")]
    if not verbose:
        return lines
    targets = []
    for line in lines:
        # Official output is tab-separated: key, type, state, addr, "hdc"
        # e.g. "SERIAL  USB  Connected  <sn>  hdc" / "COM3  UART  Ready  unknown..."
        parts = line.split()
        if not parts:
            continue
        key = parts[0]
        if len(parts) >= 3:
            ttype = parts[1]
            addr = parts[3] if len(parts) >= 4 else ""
            if parts[2].lower() in ("connected", "offline", "ready", "disconnected"):
                state = parts[2]
            else:
                state = parts[-1]
        else:
            ttype, state, addr = "", line, ""
        targets.append(TargetInfo(key, ttype, state, addr))
    return targets
