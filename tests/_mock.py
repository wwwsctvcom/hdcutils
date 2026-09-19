# -*- coding: utf-8 -*-
"""Mock hdc server -- strictly implements the reverse-engineered wire protocol,
including the daemon side of file/app tasks.

The mock mirrors the open-source hdc source semantics:

* the server sends its handshake first (44 bytes, optionally 108 with version);
* frames of ``[4B big-endian length][payload]``; the client sends the command text (+\0);
* daemon-side commands (shell etc.) close the connection when finished (EOF);
* local commands (list targets etc.) keep the connection open; the client decides completion by idleness;
* checkserver replies with a u16 LE command prefix (CMD_CHECK_SERVER = 13);
* **file/app tasks**: the daemon role is simulated in client-task mode
  (protobuf serialization reuses ``hdcutils._serial`` -- an implicit
  consistency check between client and mock).
"""
from __future__ import annotations

import base64
import os
import shlex
import socket
import struct
import threading
import time
from typing import Optional, Tuple

import sys as _sys
import os as _os

_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from hdcutils._serial import TransferConfig, TransferPayload  # noqa: E402

SERVER_HANDSHAKE_MIN = 44
SERVER_HANDSHAKE_FULL = 108
CMD_KERNEL_CHANNEL_CLOSE = 2
CMD_CHECK_SERVER = 13
CMD_FILE_INIT = 3000
CMD_FILE_CHECK = 3001
CMD_FILE_BEGIN = 3002
CMD_FILE_DATA = 3003
CMD_FILE_FINISH = 3004
CMD_APP_INIT = 3500
CMD_APP_CHECK = 3501
CMD_APP_BEGIN = 3502
CMD_APP_DATA = 3503
CMD_APP_FINISH = 3504

SERIALS = ["MOCKSERIAL1", "MOCKSERIAL2"]
VERSION = b"hdc mock 0.1 mock-hash"

# minimal JPEG with a real SOF0 header (100x50) for screenshot/window-size tests
FAKE_JPEG = (
    b"\xff\xd8"  # SOI
    b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"  # APP0
    b"\xff\xc0\x00\x11\x08\x00\x32\x00\x64\x03"  # SOF0: prec=8 h=50 w=100, 3 comps
    b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    b"\xff\xd9"  # EOI
)

FEATURE_FLAGS = bytes([0b00000011]) + b"\x00" * 7


class MockHdcServer:
    """Threaded mock server. ``port=0`` picks a free port; with
    ``use_version_handshake=True`` a 108-byte handshake is sent (simulating
    official version-check builds)."""

    def __init__(self, port: int = 0, use_version_handshake: bool = False):
        self.port = port
        self.use_version_handshake = use_version_handshake
        self.device_files: dict = {}  # emulated device file system
        self.installed_packages: list = []
        self.uninstalled: list = []
        self.forward_rules: dict = {}  # "tcp:7000 tcp:8012" -> listener socket
        self.reverse_rules: dict = {}
        self._channel_seq = 0
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self.stop_event = threading.Event()

    def start(self) -> int:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(16)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()
        return self.port

    def stop(self) -> None:
        self.stop_event.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
        for rule in list(self.forward_rules):
            listener = self.forward_rules.pop(rule, None)
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass

    # ------------------------------------------------------------------
    def _start_forward_listener(self, local_node: str, remote_node: str) -> None:
        """Emulate port forwarding: listen on the local node and answer with a
        fixed banner so create_connection() has something to talk to."""
        rule = "%s %s" % (local_node, remote_node)
        if rule in self.forward_rules:
            return
        port = int(local_node.split(":")[1])
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            listener.close()  # port taken: behave like a failed forward request
            return
        listener.listen(4)
        self.forward_rules[rule] = listener

        def serve():
            while rule in self.forward_rules:
                try:
                    client, _ = listener.accept()
                except OSError:
                    return
                try:
                    client.sendall(b"mock-forward " + remote_node.encode() + b"\r\n")
                except OSError:
                    pass
                finally:
                    try:
                        client.close()
                    except OSError:
                        pass

        threading.Thread(target=serve, daemon=True).start()

    def _stop_forward_listener(self, local_node: str, remote_node: str) -> None:
        rule = "%s %s" % (local_node, remote_node)
        listener = self.forward_rules.pop(rule, None)
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass

    def _serve(self) -> None:
        while not self.stop_event.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        try:
            with self._lock:
                self._channel_seq += 1
                channel_id = self._channel_seq
            self._send_handshake(conn, channel_id)
            reply = self._read_frame(conn)  # client reply is length-framed
            if reply is None or reply[:8] != b"OHOS HDC":
                conn.close()
                return
            connect_key = reply[12:44].split(b"\x00", 1)[0].decode("utf-8", "replace") or "any"
            while True:
                payload = self._read_frame(conn)
                if payload is None:
                    return
                command = payload.rstrip(b"\x00").decode("utf-8", "replace")
                if not self._dispatch(conn, command, connect_key):
                    return
        except OSError:
            pass
        finally:
            self._graceful_close(conn)

    @staticmethod
    def _graceful_close(conn: socket.socket) -> None:
        """libuv-style graceful close: SHUT_WR first, drain, then close (avoids
    Windows RST dropping pending data)."""
        try:
            conn.shutdown(socket.SHUT_WR)
            conn.settimeout(2)
            while conn.recv(65536):
                pass
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _send_handshake(self, conn: socket.socket, channel_id: int) -> None:
        banner = b"OHOS HDC\x00\x00KH"  # banner[10]='K', banner[11]='H'
        payload = banner + struct.pack(">I", channel_id) + b"\x00" * 28
        if self.use_version_handshake:
            payload += VERSION + b"\x00" * (64 - len(VERSION))
        conn.sendall(struct.pack(">I", len(payload)) + payload)  # length-framed

    @staticmethod
    def _recv_exact(conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise OSError("closed")
            buf += chunk
        return buf

    @staticmethod
    def _read_frame(conn: socket.socket) -> Optional[bytes]:
        buf = b""
        while len(buf) < 4:
            chunk = conn.recv(4 - len(buf))
            if not chunk:
                return None
            buf += chunk
        (size,) = struct.unpack(">I", buf)
        buf = b""
        while len(buf) < size:
            chunk = conn.recv(size - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    @staticmethod
    def _frame(payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + payload

    def _send_cmd(self, conn: socket.socket, command: int, payload: bytes) -> None:
        conn.sendall(self._frame(command.to_bytes(2, "little") + payload))

    # ------------------------------------------------------------------
    def _dispatch(self, conn: socket.socket, command: str, connect_key: str) -> bool:
        if command == "checkserver":
            conn.sendall(self._frame(struct.pack("<H", CMD_CHECK_SERVER) + VERSION))
            return True
        if command == "list targets":
            conn.sendall(self._frame(("\r\n".join(SERIALS) + "\r\n").encode()))
            return True
        if command == "list targets -v":
            conn.sendall(self._frame(
                b"MOCKSERIAL1 USB Connected\r\n"
                b"MOCKSERIAL2 TCP Connected 192.168.1.3:10123\r\n"))
            return True
        if command.startswith("file send"):
            return self._task_file_send(conn, command, connect_key)
        if command.startswith("file recv"):
            return self._task_file_recv(conn, command, connect_key)
        if command.startswith("install "):
            return self._task_install(conn, command, connect_key)
        if command.startswith("uninstall"):
            return self._task_uninstall(conn, command, connect_key)
        if command.startswith("shell"):
            return self._dispatch_shell(conn, command, connect_key)
        if command.startswith("tconn"):
            rest = command[len("tconn"):].strip()
            if rest.endswith("-remove"):
                conn.sendall(self._frame(b"Remove OK\r\n"))
            else:
                time.sleep(0.2)
                conn.sendall(self._frame(b"Connect OK\r\n"))
            return True
        if command == "fport ls":
            rules = list(self.forward_rules)
            conn.sendall(self._frame((("\r\n".join(rules) if rules else "(empty)") + "\r\n").encode()))
            return True
        if command.startswith(("fport", "rport")):
            reverse = command.startswith("rport")
            parts = command.split()
            if len(parts) == 3:
                if reverse:
                    self.reverse_rules[parts[1]] = parts[2]
                    conn.sendall(self._frame(b"Reverse forward set success\r\n"))
                else:
                    self._start_forward_listener(parts[1], parts[2])
                    conn.sendall(self._frame(b"Forward set success\r\n"))
            elif len(parts) == 4 and parts[1] == "rm":
                self._stop_forward_listener(parts[2], parts[3])
                conn.sendall(self._frame(b"Remove forward success\r\n"))
            else:
                conn.sendall(self._frame(b"[Fail]Invalid fport rule\r\n"))
            return True
        if command == "jpid":
            conn.sendall(self._frame(b"12345  com.example.mock\r\n"))
            return True
        if command == "track-jpid" or command.startswith("track-jpid "):
            for i in range(2):
                conn.sendall(self._frame(("1234%d  com.example.mock\r\n" % i).encode()))
                time.sleep(0.02)
            return False
        if command == "tmode port close":
            conn.sendall(self._frame(b"Tmode port close success\r\n"))
            return False
        if command == "hilog":
            for i in range(3):
                conn.sendall(self._frame(("hilog line %d\r\n" % i).encode()))
                time.sleep(0.02)
            return False
        if command == "target boot" or command.startswith("target boot "):
            return False
        if command == "reboot":
            # official server rejects a bare reboot; mirror that
            conn.sendall(self._frame(b"Unknown operation command...\r\n"))
            return False
        if command == "smode" or command.startswith("smode "):
            conn.sendall(self._frame(b"Set root run mode success\r\n"))
            return False
        if command.startswith("tmode"):
            reply = "Tmode %s success\r\n" % command[len("tmode"):].strip()
            conn.sendall(self._frame(reply.encode()))
            return False
        conn.sendall(self._frame(("[Fail]Unknown command: %s\r\n" % command).encode()))
        return False

    # ------------------------------------------------------------------
    # file tasks
    # ------------------------------------------------------------------
    @staticmethod
    def _split_params(params: str):
        try:
            parts = shlex.split(params)
        except ValueError:
            parts = params.split()
        return parts

    def _task_file_send(self, conn: socket.socket, command: str, connect_key: str) -> bool:
        """daemon as SLAVE: echo INIT -> wait CHECK -> open the "file" -> BEGIN ->
        collect DATA -> wait FINISH(1) -> reply FINISH(0) + summary."""
        params = command[len("file send"):].strip()
        init_payload = params.encode()
        self._send_cmd(conn, CMD_FILE_INIT, init_payload)
        if connect_key not in ("any",) + tuple(SERIALS):
            conn.sendall(self._frame(b"[Fail]No device connected\r\n"))
            return False
        payload = self._read_task_frame(conn, CMD_FILE_CHECK)
        if payload is None:
            return False
        config = TransferConfig.parse(payload)
        remote_path = config.path or "/data/local/tmp/mock_send.bin"
        self._send_cmd(conn, CMD_FILE_BEGIN, FEATURE_FLAGS)  # slave replies BEGIN
        chunks: dict = {}
        while True:
            cmd, payload = self._read_any_task_frame(conn)
            if cmd == CMD_FILE_DATA:
                head = TransferPayload.parse(payload)
                chunks[head.index] = payload[64:64 + head.compress_size]
            elif cmd == CMD_FILE_FINISH and payload and payload[0] == 1:
                break
            elif cmd is None:
                return False
            else:
                conn.sendall(self._frame(b"[Fail]Unexpected frame in send task\r\n"))
                return False
        content = b"".join(chunks[k] for k in sorted(chunks))
        if len(content) != config.file_size:
            conn.sendall(self._frame(b"[Fail]size mismatch\r\n"))
            return False
        self.device_files[remote_path] = content
        self._send_cmd(conn, CMD_FILE_FINISH, b"\x00")
        conn.sendall(self._frame(
            ("FileTransfer finish, Size:%d, File count = 1, time:1ms rate:0.00kB/s\r\n"
             % len(content)).encode()))
        return False

    def _task_file_recv(self, conn: socket.socket, command: str, connect_key: str) -> bool:
        """daemon as MASTER: send CHECK -> wait BEGIN -> stream DATA -> FINISH(1)
        -> wait FINISH(0) -> summary."""
        params = command[len("file recv"):].strip()
        parts = self._split_params(params)
        paths = [p for p in parts if not p.startswith("-") and p != "-cwd"]
        if connect_key not in ("any",) + tuple(SERIALS):
            conn.sendall(self._frame(b"[Fail]No device connected\r\n"))
            return False
        remote_path = paths[0]
        content = self._read_device_file(remote_path)
        if content is None:
            conn.sendall(self._frame(b"[Fail]File not found on device\r\n"))
            return False
        config = TransferConfig(
            file_size=len(content),
            path=paths[1] if len(paths) > 1 else "mock_recv.bin",
            optional_name=os.path.basename(remote_path) or "mock_recv.bin",
            compress_type=0,
            function_name="file recv",
        )
        self._send_cmd(conn, CMD_FILE_CHECK, config.serialize())
        if self._read_task_frame(conn, CMD_FILE_BEGIN) is None:
            return False
        index = 0
        chunk = 256 * 1024
        while index < len(content):
            data = content[index:index + chunk]
            head = TransferPayload(index=index, compress_type=0,
                                   compress_size=len(data), uncompress_size=len(data))
            self._send_cmd(conn, CMD_FILE_DATA, head.serialize() + data)
            index += len(data)
        self._send_cmd(conn, CMD_FILE_FINISH, b"\x01")
        if self._read_task_frame(conn, CMD_FILE_FINISH) is None:
            return False
        conn.sendall(self._frame(
            ("FileTransfer finish, Size:%d, File count = 1, time:1ms rate:0.00kB/s\r\n"
             % len(content)).encode()))
        return False

    def _read_task_frame(self, conn: socket.socket, want_cmd: int) -> Optional[bytes]:
        payload = self._read_frame(conn)
        if payload is None or len(payload) < 2:
            return None
        cmd = int.from_bytes(payload[:2], "little")
        if cmd != want_cmd:
            return None
        return payload[2:]

    def _read_any_task_frame(self, conn: socket.socket):
        payload = self._read_frame(conn)
        if payload is None:
            return None, None
        if len(payload) >= 2:
            cmd = int.from_bytes(payload[:2], "little")
            if cmd >= 2500 or cmd == CMD_KERNEL_CHANNEL_CLOSE:
                return cmd, payload[2:]
        return -1, payload

    # ------------------------------------------------------------------
    # app tasks
    # ------------------------------------------------------------------
    def _task_install(self, conn: socket.socket, command: str, connect_key: str) -> bool:
        """daemon as SLAVE: echo APP_INIT -> wait APP_CHECK -> BEGIN -> collect
        DATA; when file_size bytes arrived run "bm install" ->
        APP_FINISH([mode][ok][msg])."""
        params = command[len("install"):].strip()
        self._send_cmd(conn, CMD_APP_INIT, params.encode())
        if connect_key not in ("any",) + tuple(SERIALS):
            conn.sendall(self._frame(b"[Fail]No device connected\r\n"))
            return False
        payload = self._read_task_frame(conn, CMD_APP_CHECK)
        if payload is None:
            return False
        config = TransferConfig.parse(payload)
        self._send_cmd(conn, CMD_APP_BEGIN, FEATURE_FLAGS)
        chunks: dict = {}
        got = 0
        while got < config.file_size:
            cmd, chunk_payload = self._read_any_task_frame(conn)
            if cmd == CMD_APP_DATA:
                head = TransferPayload.parse(chunk_payload)
                chunks[head.index] = chunk_payload[64:64 + head.compress_size]
                got += head.compress_size
            else:
                conn.sendall(self._frame(b"[Fail]Unexpected frame in install\r\n"))
                return False
        content = b"".join(chunks[k] for k in sorted(chunks))
        self.installed_packages.append({
            "name": config.optional_name,
            "options": config.options,
            "size": len(content),
        })
        result = bytes([1, 1]) + b"AppMod finish"
        self._send_cmd(conn, CMD_APP_FINISH, result)
        conn.sendall(self._frame(b"AppMod finish\r\n"))
        return False

    def _task_uninstall(self, conn: socket.socket, command: str, connect_key: str) -> bool:
        parts = command.split()
        bundle = parts[-1] if len(parts) > 1 else ""
        self.uninstalled.append(bundle)
        self._send_cmd(conn, CMD_APP_FINISH, bytes([2, 1]) + b"AppMod finish")
        conn.sendall(self._frame(b"AppMod finish\r\n"))
        return False

    # ------------------------------------------------------------------
    # shell
    # ------------------------------------------------------------------
    def _dispatch_shell(self, conn: socket.socket, command: str, connect_key: str) -> bool:
        if connect_key not in ("any",) and connect_key not in SERIALS:
            conn.sendall(self._frame(b"[Fail]No device connected\r\n"))
            return False
        if command == "shell":  # interactive shell
            return self._interactive_shell(conn)
        cmd = command[len("shell"):].strip()
        returncode = None
        if cmd.endswith("; echo __RC__$?"):
            cmd = cmd[: -len("; echo __RC__$?")].strip()
            returncode = 0
        if cmd == "hilog" or cmd.startswith("hilog "):  # streamed via shell form
            for i in range(3):
                conn.sendall(self._frame(("hilog line %d\r\n" % i).encode()))
                time.sleep(0.02)
            return False
        out = self._shell_impl(cmd)
        if returncode is not None:
            out += "__RC__%d\r\n" % returncode
        if out:
            conn.sendall(self._frame(out.encode("utf-8")))
        return False

    def _shell_impl(self, cmd: str) -> str:
        if cmd == "param get const.product.model":
            return "const.product.model = HUAWEI Mock Phone\r\n"
        if cmd == "param get":
            return ("const.product.model = HUAWEI Mock Phone\r\n"
                    "const.product.manufacturer = Huawei\r\n"
                    "const.ohos.apiversion = 12\r\n")
        if cmd.startswith("param ls"):
            return "const.product.model = HUAWEI Mock Phone" + r + n
        if cmd.startswith("param set "):
            return ""
        if cmd.startswith("param wait "):
            return "wait param match success" + r + n
        if cmd == "param save":
            return ""
        if cmd.startswith("uitest uiInput "):
            return ""
        if cmd.startswith("uitest screenCap"):
            target = cmd.split("-p ")[-1].split()[0] if "-p " in cmd else                 "/data/local/tmp/mock.png"
            self.device_files[target] = FAKE_JPEG
            return ""
        if cmd.startswith("power-shell"):
            return ""
        if cmd.startswith("bm get"):
            return "udid: MOCK-UDID-1234" + r + n
        if cmd.startswith("bugreport"):
            parts = cmd.split()
            if len(parts) > 1:
                self.device_files[parts[1]] = b"[base] MockReport" + r.encode()
                return ""
            return "[base] MockReport" + r + n
        if cmd.startswith("test -e "):
            path = cmd[len("test -e "):].split("&&")[0].strip()
            if self._fake_exists(path):
                return "__YES__\r\n"
            return ""
        if cmd.startswith("test -f "):
            path = cmd[len("test -f "):].split("&&")[0].strip()
            if self._fake_exists(path):
                return "__OK__\r\n"
            return "test: No such file or directory\r\n"
        if cmd.startswith("base64 "):
            path = cmd[len("base64 "):].strip()
            if self._fake_exists(path):
                return base64.b64encode(self._read_device_file(path)).decode() + "\r\n"
            return "base64: No such file or directory\r\n"
        if cmd.startswith("rm -f "):
            self.device_files.pop(cmd[len("rm -f "):].strip(), None)
            return ""
        if cmd.startswith("snapshot_display") or cmd.startswith("uitest screenCap"):
            self.device_files[_snapshot_path(cmd)] = FAKE_JPEG
            return ""
        if cmd == "bm dump -a":
            return "com.example.mock\ncom.example.other\r\n"
        if cmd.startswith("bm dump -n"):
            return '{"name":"com.example.mock","versionName":"1.0.0"}\r\n'
        if cmd.startswith("aa force-stop") or cmd.startswith("bm clean"):
            return ""
        if cmd.startswith("aa start"):
            return "start ability successfully.\r\n"
        if cmd.startswith("echo ") and " | base64 -d > " in cmd:
            # write_file: "echo <b64> | base64 -d > /path"
            encoded, _, target = cmd[len("echo "):].partition(" | base64 -d > ")
            try:
                self.device_files[target.strip()] = base64.b64decode(encoded.strip())
            except Exception:
                return "base64: invalid input\r\n"
            return ""
        if cmd.startswith("echo "):
            return cmd[len("echo "):] + "\r\n"
        if cmd == "hilog" or cmd.startswith("hilog "):
            return "hilog: stream handled elsewhere\r\n"
        if cmd.startswith("sleep"):
            time.sleep(min(float(cmd.split()[1]), 3.0))
            return ""
        if cmd.startswith("uitest uiInput") or cmd.startswith("power-shell"):
            return ""
        if cmd.startswith("hidumper"):
            if "AbilityManagerService" in cmd:
                return ("User ID   : 100\r\n"
                        "\tBundle Name [com.example.mock]\r\n"
                        "\tAbility Name [EntryAbility]\r\n")
            if "BatteryService" in cmd:
                return "Capacity: 85\r\nCharge state: 1\r\n"
            if "PowerManagerService" in cmd:
                return "Screen state: ON\r\n"
            return ""
        if cmd.startswith("find "):
            query = cmd[5:].split()[0] if len(cmd[5:].split()) else ""
            files = sorted(self.device_files)
            if not files and query.startswith("/data/local/tmp"):
                files = ["/data/local/tmp/x.jpeg"]  # default mock test file
            files = [f for f in files if f.startswith(query)]
            if not files:
                return "find: no such directory\r\n"
            return "\n".join(files) + "\r\n"
        if cmd.startswith("mkdir -p "):
            return ""
        return "mock shell: %s\r\n" % cmd

    def _interactive_shell(self, conn: socket.socket) -> bool:
        while True:
            payload = self._read_frame(conn)
            if payload is None:
                return False
            text = payload.decode("utf-8", "replace").strip()
            if text == "exit":
                return False
            conn.sendall(self._frame(("mock> %s\n" % text).encode()))

    def _fake_exists(self, path: str) -> bool:
        if path in self.device_files:
            return True
        if path.startswith("/data/local/tmp/"):
            return "no/such" not in path and "/no/" not in path
        return "no/such" not in path and "/no/" not in path

    def _read_device_file(self, path: str):
        if path in self.device_files:
            return self.device_files[path]
        if path.startswith("/data/local/tmp/") and "no/such" not in path and "/no/" not in path:
            return FAKE_JPEG
        return None


def _snapshot_path(cmd: str) -> str:
    for token in cmd.split():
        if token.startswith("/data/"):
            return token
    return "/data/local/tmp/mock.jpeg"


def main() -> None:  # pragma: no cover - standalone mode (fake-hdc pullup test)
    import sys

    port = int(sys.argv[1])
    server = MockHdcServer(port)
    server.start()
    print("mock hdc server on %d" % port, flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
