# -*- coding: utf-8 -*-
"""Pure-socket implementation of the hdc file/app transfer tasks (no hdc.exe).

Protocol verified against the open-source hdc sources (Apache-2.0):

* ``src/host/main.cpp``              -- ``AppendCwdWhenTransfer``: file send/recv and
  install command texts get `` remote -cwd "<cwd>"`` appended; the server then
  enters "client-hosted task" mode (fromClient) and echoes INIT back to the client.
* ``src/host/server_for_client.cpp`` -- ``TaskCommand``: INIT echo (with the
  "send "/"install " prefix stripped); CMD_APP_UNINSTALL is relayed to the
  daemon by a server-side task.
* ``src/common/file.cpp``            -- file-task master/slave choreography.
* ``src/host/host_app.cpp``          -- install CHECK frame (randomized optionalName).
* ``src/daemon/daemon_app.cpp``      -- the daemon writes /data/local/tmp then runs
  ``bm install``, reporting through CMD_APP_FINISH([mode][ok][echo]).
* ``src/common/transfer.cpp``        -- 64-byte TransferPayload prefix + data in DATA frames.
* ``src/common/serial_struct_define.h`` -- protobuf-style serialization (_serial.py).

Command lifetimes:

* **file send** (host is master): text command -> wait for the ``CMD_FILE_INIT``
  echo -> send ``CMD_FILE_CHECK`` (TransferConfig) -> wait for
  ``CMD_FILE_BEGIN`` (8B feature flags) -> stream ``CMD_FILE_DATA``
  (64B TransferPayload prefix + data) -> send ``CMD_FILE_FINISH(1)`` ->
  wait for ``CMD_FILE_FINISH(0)`` -> channel EOF.
* **file recv** (host is slave): text command -> wait for the daemon
  ``CMD_FILE_CHECK`` (path = local target) -> send ``CMD_FILE_BEGIN`` ->
  receive ``CMD_FILE_DATA`` and write at index -> wait for
  ``CMD_FILE_FINISH(1)`` -> reply ``CMD_FILE_FINISH(0)`` -> wait for EOF.
* **install**: text command -> wait for the ``CMD_APP_INIT`` echo -> send
  ``CMD_APP_CHECK`` (randomized optionalName) -> wait for ``CMD_APP_BEGIN``
  -> stream ``CMD_APP_DATA`` -> wait for ``CMD_APP_FINISH``
  ([mode u8][ok u8][msg]).
* **uninstall**: the text command goes straight through (the daemon runs
  ``bm uninstall -n <bundle>``); the result arrives as text echo and/or a
  CMD_APP_FINISH frame.
"""
from __future__ import annotations

import os
import secrets
import struct
import time
from typing import Optional, Tuple

from ._connection import HdcChannel
from ._proto import DEFAULT_HOST
from ._serial import TRANSFER_PAYLOAD_PREFIX, TransferConfig, TransferPayload
from .exceptions import HdcCommandError, HdcServerError, HdcTimeoutError

__all__ = ["FileTask", "AppTask", "TRANSFER_CHUNK_SIZE", "random_package_name"]

# define_enum.h::HdcCommand (transfer-task subset)
CMD_KERNEL_CHANNEL_CLOSE = 2
CMD_FILE_INIT = 3000
CMD_FILE_CHECK = 3001
CMD_FILE_BEGIN = 3002
CMD_FILE_DATA = 3003
CMD_FILE_FINISH = 3004
CMD_FILE_MODE = 3005
CMD_DIR_MODE = 3006
CMD_APP_INIT = 3500
CMD_APP_CHECK = 3501
CMD_APP_BEGIN = 3502
CMD_APP_DATA = 3503
CMD_APP_FINISH = 3504
CMD_APP_UNINSTALL = 3505

_STRUCTURED_CMDS = frozenset([
    CMD_KERNEL_CHANNEL_CLOSE,
    CMD_FILE_INIT, CMD_FILE_CHECK, CMD_FILE_BEGIN, CMD_FILE_DATA,
    CMD_FILE_FINISH, CMD_FILE_MODE, CMD_DIR_MODE,
    CMD_APP_INIT, CMD_APP_CHECK, CMD_APP_BEGIN, CMD_APP_DATA,
    CMD_APP_FINISH, CMD_APP_UNINSTALL,
])

# master read chunk (transfer.cpp caps at GetMaxBufSize()*0.8; 256KB is safe)
TRANSFER_CHUNK_SIZE = 0x40000


def random_package_name(ext: str) -> str:
    """host_app.cpp CheckMaster: random name prevents illegal package names in pm."""
    return secrets.token_hex(10) + ext  # 20 hex chars + extension


def _frame_encode(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big") + payload


class TaskChannel:
    """Connection hosting file/app tasks: task frames are ``[u16 LE cmd][payload]``.

    The server prepends a u16 command number to task frames
    (SendCommandToClient/SendWithCmd); log text (EchoClient) travels as plain
    text frames. Frames are told apart by a command-number whitelist (the first
    two bytes of a text frame never land in the task range, e.g. "[Fail]" is
    0x5B 0x46).
    """

    def __init__(self, channel: HdcChannel):
        self.channel = channel
        self._reader = channel._reader

    @classmethod
    def open(cls, host: str, port: int, command: str, connect_key: str = "any",
             connect_timeout: float = 3.0) -> "TaskChannel":
        channel = HdcChannel(host, port, connect_timeout=connect_timeout)
        channel.open(connect_key)
        channel.send_command(command)
        return cls(channel)

    def send_cmd(self, command: int, payload: bytes = b"") -> None:
        self.channel._require_open()
        data = command.to_bytes(2, "little") + payload
        self.channel._sock.sendall(_frame_encode(data))

    def recv(self, timeout: float = 10.0):
        """Read the next frame. Returns ``(cmd, payload)``; cmd=None means EOF,
    cmd=-1 means a plain-text frame."""
        while True:
            payload = self._reader.read_frame(timeout)
            if payload is None:
                return None, None
            if len(payload) >= 2:
                cmd = int.from_bytes(payload[:2], "little")
                if cmd in _STRUCTURED_CMDS:
                    return cmd, payload[2:]
            return -1, payload

    def close(self) -> None:
        self.channel.close()


def remaining(deadline: float, floor: float = 0.05) -> float:
    return max(deadline - time.monotonic(), floor)


def _quote_arg(value: str) -> str:
    value = value.replace('"', '\\"')
    return '"%s"' % value if (" " in value or "\t" in value) else value


def _client_cwd() -> str:
    """Mirror main.cpp ``AppendCwdWhenTransfer``: cwd ends with a path separator."""
    cwd = os.getcwd()
    if not cwd.endswith(os.sep):
        cwd += os.sep
    return cwd


def _task_error(cmd: Optional[int], payload: Optional[bytes]) -> HdcCommandError:
    if cmd == -1 and payload is not None:
        text = payload.decode("utf-8", "replace").strip()
        if text.startswith("[Fail]"):
            return HdcCommandError(text[6:].strip(), output=text)
        return HdcCommandError("unexpected text response: %s" % text[:200])
    return HdcCommandError("unexpected command frame: cmd=%s payload=%r" % (cmd, payload))


def _raise_on_fail_text(payload: bytes) -> None:
    text = payload.decode("utf-8", "replace").strip()
    if text.startswith("[Fail]"):
        raise HdcCommandError(text[6:].strip(), output=text)


def _smart_slave_path(config: TransferConfig, fallback: str) -> str:
    """Approximation of transfer.cpp ``SmartSlavePath``: when the target is an
    existing directory or ends with a separator, append optional_name."""
    target = config.path or fallback
    if target.endswith(("/", "\\")):
        return target + config.optional_name
    if os.path.isdir(target):
        return os.path.join(target, config.optional_name)
    return target


class FileTask:
    """Pure-socket ``file send/recv`` (single files; directories are composed
        of single-file transfers at a higher layer)."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = 8710,
                 connect_timeout: float = 3.0, chunk_size: int = TRANSFER_CHUNK_SIZE,
                 connect_key: str = "any"):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.chunk_size = chunk_size
        self.connect_key = connect_key

    # ------------------------------------------------------------------
    def send_file(self, local: str, remote: str, timeout: float = 300.0,
                  hold_timestamp: bool = False, update_if_new: bool = False) -> str:
        """Push a local file to the device, return the daemon summary text."""
        if not os.path.isfile(local):
            raise HdcCommandError("local file not found: %s" % local)
        local = os.path.abspath(local)
        command = "file send %s %s remote -cwd \"%s\"" % (
            _quote_arg(local), _quote_arg(remote), _client_cwd())
        channel = TaskChannel.open(self.host, self.port, command, self.connect_key,
                                   connect_timeout=self.connect_timeout)
        try:
            return self._send_master(channel, local, remote, timeout,
                                     hold_timestamp, update_if_new)
        finally:
            channel.close()

    def recv_file(self, remote: str, local: str, timeout: float = 300.0) -> str:
        """Pull a device file to the local machine."""
        local = os.path.abspath(local)
        command = "file recv %s %s remote -cwd \"%s\"" % (
            _quote_arg(remote), _quote_arg(local), _client_cwd())
        channel = TaskChannel.open(self.host, self.port, command, self.connect_key,
                                   connect_timeout=self.connect_timeout)
        try:
            return self._recv_slave(channel, timeout)
        finally:
            channel.close()

    # ------------------------------------------------------------------
    def _send_master(self, channel: TaskChannel, local: str, remote: str,
                     timeout: float, hold_timestamp: bool, update_if_new: bool) -> str:
        deadline = time.monotonic() + timeout
        cmd, payload = channel.recv(remaining(deadline))
        if cmd is None:
            raise HdcServerError("connection closed before file init")
        if cmd != CMD_FILE_INIT:
            raise _task_error(cmd, payload)

        stat = os.stat(local)
        config = TransferConfig(
            file_size=stat.st_size,
            path=remote,
            optional_name=os.path.basename(local),
            update_if_new=update_if_new,
            compress_type=0,
            hold_timestamp=hold_timestamp,
            atime=int(stat.st_atime * 1e9) if hold_timestamp else 0,
            mtime=int(stat.st_mtime * 1e9) if hold_timestamp else 0,
            function_name="file send",
            client_cwd=_client_cwd(),
        )
        channel.send_cmd(CMD_FILE_CHECK, config.serialize())

        cmd, payload = channel.recv(remaining(deadline))
        if cmd is None:
            raise HdcServerError("connection closed waiting for file begin")
        if cmd != CMD_FILE_BEGIN:
            raise _task_error(cmd, payload)

        index = 0
        with open(local, "rb") as f:
            while True:
                data = f.read(self.chunk_size)
                if not data:
                    break
                head = TransferPayload(index=index, compress_type=0,
                                       compress_size=len(data), uncompress_size=len(data))
                channel.send_cmd(CMD_FILE_DATA, head.serialize() + data)
                index += len(data)
        channel.send_cmd(CMD_FILE_FINISH, b"\x01")
        # tail after FINISH(0): daemon summary text, or straight EOF
        summary = ""
        while True:
            try:
                cmd, payload = channel.recv(remaining(deadline, 1.0))
            except HdcTimeoutError:
                break
            if cmd is None:
                break
            if cmd == -1 and payload is not None:
                summary += payload.decode("utf-8", "replace")
            elif cmd == CMD_KERNEL_CHANNEL_CLOSE:
                break
        return summary.strip()

    def _recv_slave(self, channel: TaskChannel, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        summary = ""
        local_file = None
        try:
            while True:
                cmd, payload = channel.recv(remaining(deadline))
                if cmd is None:
                    break  # the daemon closed the channel after finishing
                if cmd == CMD_FILE_CHECK:
                    config = TransferConfig.parse(payload)
                    target = _smart_slave_path(config, config.path)
                    _ensure_parent_dir(target)
                    local_file = open(target, "wb")
                    channel.send_cmd(CMD_FILE_BEGIN, _feature_flags())
                elif cmd == CMD_FILE_DATA:
                    header = TransferPayload.parse(payload)
                    data = payload[TRANSFER_PAYLOAD_PREFIX:
                                   TRANSFER_PAYLOAD_PREFIX + header.compress_size]
                    if local_file is not None:
                        local_file.seek(header.index)
                        local_file.write(data)
                elif cmd == CMD_FILE_FINISH:
                    if payload and payload[0] == 1:
                        if local_file is not None:
                            local_file.close()
                            local_file = None
                        channel.send_cmd(CMD_FILE_FINISH, b"\x00")
                    else:
                        break
                elif cmd == -1 and payload is not None:
                    text = payload.decode("utf-8", "replace")
                    if text.startswith("[Fail]"):
                        raise HdcCommandError(text[6:].strip(), output=text)
                    summary = text
        finally:
            if local_file is not None:
                local_file.close()
        return summary.strip()


def _feature_flags() -> bytes:
    """8-byte FeatureFlagsUnion: hugeBuf=1, reserveBits1=1 (AddFeatures)."""
    return bytes([0b00000011]) + b"\x00" * 7


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


class AppTask:
    """Pure-socket ``install/uninstall``."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = 8710,
                 connect_timeout: float = 3.0, chunk_size: int = TRANSFER_CHUNK_SIZE,
                 connect_key: str = "any"):
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self.chunk_size = chunk_size
        self.connect_key = connect_key

    def install(self, path: str, options: str = "-r", timeout: float = 600.0) -> str:
        """Install a local .hap/.hsp/.app package; raises on failure."""
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise HdcCommandError("install package not found: %s" % path)
        ext = os.path.splitext(path)[1].lower()
        if ext not in (".hap", ".hsp", ".app"):
            raise HdcCommandError(
                "unsupported package type %r (expect .hap/.hsp/.app)" % ext)
        opts = " ".join(options.split()) if options else ""
        command = "install %s %s remote -cwd \"%s\"" % (
            opts, _quote_arg(path), _client_cwd())
        channel = TaskChannel.open(self.host, self.port, command, self.connect_key,
                                   connect_timeout=self.connect_timeout)
        try:
            return self._install_flow(channel, path, opts, ext, timeout)
        finally:
            channel.close()

    def uninstall(self, bundle_name: str, keep_data: bool = False,
                  timeout: float = 300.0) -> str:
        """Uninstall an app (``bm uninstall -n <bundle>`` via a server-side task)."""
        command = "uninstall%s %s" % (" -k" if keep_data else "", bundle_name)
        channel = TaskChannel.open(self.host, self.port, command, self.connect_key,
                                   connect_timeout=self.connect_timeout)
        try:
            return self._plain_result(channel, timeout)
        finally:
            channel.close()

    # ------------------------------------------------------------------
    def _plain_result(self, channel: TaskChannel, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        texts = []
        while True:
            cmd, payload = channel.recv(remaining(deadline))
            if cmd is None:
                break
            if cmd == CMD_APP_FINISH and len(payload) >= 2:
                level = "OK" if payload[1] else "FAIL"
                texts.append("%s %s" % (level,
                            payload[2:].decode("utf-8", "replace").strip()))
            elif cmd == -1 and payload is not None:
                texts.append(payload.decode("utf-8", "replace").strip())
            elif cmd == CMD_KERNEL_CHANNEL_CLOSE:
                break
        return "\n".join(t for t in texts if t.strip())

    def _install_flow(self, channel: TaskChannel, local: str, options: str, ext: str,
                      timeout: float) -> str:
        deadline = time.monotonic() + timeout
        cmd, payload = channel.recv(remaining(deadline))
        if cmd is None:
            raise HdcServerError("connection closed before app init")
        if cmd != CMD_APP_INIT:
            raise _task_error(cmd, payload)

        config = TransferConfig(
            file_size=os.path.getsize(local),
            options=options,
            optional_name=random_package_name(ext),
            compress_type=0,
            function_name="install",
            client_cwd=_client_cwd(),
        )
        channel.send_cmd(CMD_APP_CHECK, config.serialize())

        cmd, payload = channel.recv(remaining(deadline))
        if cmd is None:
            raise HdcServerError("connection closed waiting for app begin")
        if cmd != CMD_APP_BEGIN:
            raise _task_error(cmd, payload)

        index = 0
        with open(local, "rb") as f:
            while True:
                data = f.read(self.chunk_size)
                if not data:
                    break
                head = TransferPayload(index=index, compress_type=0,
                                       compress_size=len(data), uncompress_size=len(data))
                channel.send_cmd(CMD_APP_DATA, head.serialize() + data)
                index += len(data)

        result = ""
        success: Optional[bool] = None
        while True:
            cmd, payload = channel.recv(remaining(deadline))
            if cmd is None:
                break
            if cmd == CMD_APP_FINISH and len(payload) >= 2:
                success = bool(payload[1])
                result = payload[2:].decode("utf-8", "replace").strip()
            elif cmd == -1 and payload is not None:
                text = payload.decode("utf-8", "replace")
                if text.startswith("[Fail]"):
                    raise HdcCommandError(text[6:].strip(), output=text)
                text = text.strip()
                if text:
                    result = result or text
                if "AppMod finish" in text:
                    break
        if success is False:
            raise HdcCommandError(result or "install failed", output=result)
        return result
