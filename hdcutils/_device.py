# -*- coding: utf-8 -*-
"""Device API, named after the **official hdc / OpenHarmony tool commands**.

Every public method maps onto a documented command, grouped the way the
official reference groups them
([hdc tool](https://gitee.com/openharmony/docs/blob/master/zh-cn/application-dev/dfx/hdc.md),
[aa](https://gitee.com/openharmony/docs/blob/master/zh-cn/application-dev/tools/aa-tool.md),
[bm](https://gitee.com/openharmony/docs/blob/master/zh-cn/application-dev/tools/bm-tool.md),
[param](https://gitee.com/openharmony/docs/blob/master/zh-cn/application-dev/tools/param-tool.md),
[uitest](https://gitee.com/openharmony/docs/blob/master/zh-cn/application-dev/application-test/uitest-guidelines.md)):

======================  =========================================================
Section                 Official command
======================  =========================================================
Device connection       ``hdc list targets`` / ``hdc wait`` / ``hdc tconn`` /
                        ``hdc tmode``
Shell                   ``hdc shell``
File transfer           ``hdc file send`` / ``hdc file recv``
App management          ``hdc install`` / ``hdc uninstall`` /
                        ``aa start`` / ``aa force-stop`` / ``bm clean`` /
                        ``bm dump``
Port forwarding         ``hdc fport`` / ``hdc rport`` / ``hdc fport ls`` /
                        ``hdc fport rm``
Service process         ``hdc start`` / ``hdc kill`` / ``hdc checkserver``
Device operations       ``hdc hilog`` / ``hdc jpid`` / ``hdc track-jpid`` /
                        ``hdc target boot`` / ``hdc bugreport``
System parameters       ``param get`` / ``param ls`` / ``param set`` /
                        ``param wait`` / ``param save``
UI input                ``uitest uiInput click|doubleClick|longClick|fling|
                        swipe|drag|dircFling|inputText|text|keyEvent``
Screen & power          ``snapshot_display`` / ``uitest screenCap`` /
                        ``power-shell`` / ``hidumper``
======================  =========================================================

There is no hdc.exe dependency in any of these calls: the command text goes
straight to the hdc server over a socket (one short connection per command).

Not implemented (by design): app sandbox (-b bundlename), lz4 compression
(-z) and the binary directory-mode protocol; directory transfers are composed
from single-file transfers in :meth:`send_dir` / :meth:`pull_dir`.
"""
from __future__ import annotations

import base64
import binascii
import io
import json
import os
import re
import socket
import time
from typing import Iterator, List, NamedTuple, Optional, Tuple, TYPE_CHECKING

from ._connection import ShellSession, DEFAULT_IDLE_WINDOW
from ._proto import strip_message_prefix
from ._task import AppTask, FileTask
from .exceptions import HdcCommandError, HdcError

if TYPE_CHECKING:  # pragma: no cover
    from .core import HdcClient

__all__ = [
    "HdcDevice",
    "DeviceInfo",
    "KeyCode",
    "WindowSize",
    "AppCurrentInfo",
    "SyncSession",
    "ForwardedSocket",
]


class KeyCode:
    """OpenHarmony key codes for ``uitest uiInput keyEvent``.

    Values come from ``@ohos.multimodalInput.keyCode``; the command also
    accepts the documented names (``"Back"``, ``"Home"`` ...).
    """

    HOME = 1
    BACK = 2
    MENU = 3
    VOLUME_UP = 16
    VOLUME_DOWN = 17
    POWER = 18


class WindowSize(NamedTuple):
    """Screen size in pixels."""

    width: int
    height: int


class AppCurrentInfo(NamedTuple):
    """Foreground app info (from ``hidumper -s AbilityManagerService``)."""

    bundle_name: str
    ability_name: str

    @property
    def package(self) -> str:
        """Alias kept for callers used to the Android naming."""
        return self.bundle_name

    @property
    def activity(self) -> str:
        return self.ability_name


class DeviceInfo:
    """Device facts read through ``param get`` (system parameters)."""

    __slots__ = ("serial", "model", "brand", "manufacturer", "product",
                 "software_version", "os_version", "api_version", "props")

    def __init__(self, serial: str, props: dict):
        self.serial = serial
        self.props = props
        self.model = props.get("const.product.model", "")
        self.brand = props.get("const.product.brand", "")
        self.manufacturer = props.get("const.product.manufacturer", "")
        self.product = props.get("const.product.name", "")
        self.software_version = props.get("const.product.software.version", "")
        self.os_version = props.get("const.ohos.versionid", "")
        self.api_version = props.get("const.ohos.apiversion", "")

    def __repr__(self) -> str:
        return "DeviceInfo(serial=%r, model=%r, os=%r, api=%r)" % (
            self.serial, self.model, self.os_version, self.api_version)


class SyncSession:
    """File-transfer session (``hdc file send`` / ``hdc file recv``).

    One short connection per call; reach it through :attr:`HdcDevice.sync`.
    """

    def __init__(self, device: "HdcDevice"):
        self._device = device

    def push(self, src: str, dst: str, timeout: float = 300.0) -> str:
        """``hdc file send <src> <dst>`` (local -> device)."""
        return self._device.send_file(src, dst, timeout=timeout)

    def pull(self, rpath: str, lpath: str, timeout: float = 300.0) -> str:
        """``hdc file recv <rpath> <lpath>`` (device -> local)."""
        return self._device.recv_file(rpath, lpath, timeout=timeout)

    def read_bytes(self, rpath: str, timeout: float = 60.0) -> bytes:
        """Read a device file into memory (base64 over a shell connection)."""
        return self._device.read_file(rpath, timeout=timeout)

    def read_text(self, rpath: str, timeout: float = 60.0,
                  encoding: str = "utf-8") -> str:
        return self.read_bytes(rpath, timeout=timeout).decode(encoding, "replace")

    def write_bytes(self, rpath: str, data: bytes, timeout: float = 60.0) -> None:
        """Write bytes to a device file (base64 relay; use push for big data)."""
        self._device.write_file(rpath, data, timeout=timeout)

    def write_text(self, rpath: str, text: str, timeout: float = 60.0,
                   encoding: str = "utf-8") -> None:
        self.write_bytes(rpath, text.encode(encoding), timeout=timeout)

    def iter_content(self, rpath: str, timeout: Optional[float] = None) -> Iterator[bytes]:
        """Stream a device file with ``cat`` (one short-lived connection)."""
        return self._device.stream_shell("cat %s" % rpath, timeout=timeout)


class HdcDevice:
    """A single HarmonyOS device.

    Obtain instances through :meth:`HdcClient.device`. Methods are grouped and
    named after the official hdc tool commands (see the module docstring).
    """

    def __init__(self, client: "HdcClient", serial: str):
        self.client = client
        self.serial = serial

    def __repr__(self) -> str:
        return "HdcDevice(serial=%r)" % self.serial

    # ==================================================================
    # Shell -- `hdc shell`
    # ==================================================================
    def shell(self, cmd, stream: bool = False, timeout: Optional[float] = None,
              encoding: str = "utf-8"):
        """``hdc shell <cmd>``: run a command on the device.

        ``cmd`` accepts a str or a list (joined with spaces; pass a str when
        quoting matters). hdc does not propagate exit codes -- use
        :meth:`shell2` when the return code matters. With ``stream=True`` a
        generator of raw output chunks is returned instead (explicit long
        connection; close it when done).
        """
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(c) for c in cmd)
        cmd = str(cmd)
        if cmd.startswith("shell "):
            cmd = cmd[len("shell "):]
        if stream:
            return self.stream_shell(cmd, timeout=timeout)
        return self.shell_bytes(cmd, timeout=timeout).decode(
            encoding, "replace").rstrip("\r\n")

    def shell_bytes(self, cmd, timeout: Optional[float] = None) -> bytes:
        """``hdc shell <cmd>``, returning raw bytes."""
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(c) for c in cmd)
        cmd = str(cmd)
        if cmd.startswith("shell "):
            cmd = cmd[len("shell "):]
        return self._execute("shell " + cmd, completion="stream",
                             check_fail=False, timeout=timeout)

    def shell2(self, cmd, timeout: Optional[float] = None) -> Tuple[str, int]:
        """``hdc shell`` plus a return code: ``(output, returncode)``.

        hdc does not report exit codes, so ``; echo __RC__$?`` is appended;
        this works for virtually all commands.
        """
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(c) for c in cmd)
        text = self.shell("%s; echo __RC__$?" % cmd, timeout=timeout)
        match = re.search(r"__RC__(\d+)\s*$", text)
        if match:
            return text[: match.start()].rstrip("\r\n"), int(match.group(1))
        return text, -1

    def open_shell(self, initial: Optional[str] = None) -> "ShellSession":
        """``hdc shell`` with no command: interactive device terminal.

        This is an explicit long connection; close it when done.

        Usage::

            with d.open_shell() as sh:
                sh.send("ps -ef")
                print(sh.recv(timeout=2))
        """
        self.client._ensure()
        session = ShellSession(self.client.host, self.client.port, self.serial,
                               connect_timeout=self.client.connect_timeout)
        if initial:
            session.send(initial)
        return session

    def stream_shell(self, cmd, timeout: Optional[float] = None) -> Iterator[bytes]:
        """``hdc shell`` with streaming output (raw chunks)."""
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(c) for c in cmd)
        return self.client.stream_command("shell " + str(cmd), serial=self.serial,
                                          timeout=timeout)

    def stream_lines(self, cmd, timeout: Optional[float] = None,
                     encoding: str = "utf-8") -> Iterator[str]:
        """``hdc shell`` with streaming output, line by line."""
        buf = b""
        for chunk in self.stream_shell(cmd, timeout=timeout):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r").decode(encoding, "replace")
        if buf.strip():
            yield buf.rstrip(b"\r").decode(encoding, "replace")

    # ==================================================================
    # Device operations -- `hdc hilog` / `hdc jpid` / `hdc track-jpid` /
    # `hdc target boot` / `hdc bugreport`
    # ==================================================================
    def hilog(self, *args, timeout: Optional[float] = None) -> Iterator[str]:
        """``hdc hilog [-h]``: stream device logs line by line.

        Explicit long connection: break out of the generator or pass a
        timeout, and close it between capture sessions to save power.
        """
        command = "hilog" + (" " + " ".join(args) if args else "")
        buf = b""
        for chunk in self.client.stream_command(command, serial=self.serial, timeout=timeout):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r").decode("utf-8", "replace")
        if buf.strip():
            yield buf.rstrip(b"\r").decode("utf-8", "replace")

    def jpid(self, timeout: float = 15.0) -> str:
        """``hdc jpid``: pids of apps with open abilities."""
        return self._execute("jpid", check_fail=False, timeout=timeout).decode(
            "utf-8", "replace").strip()

    def track_jpid(self, *args, timeout: Optional[float] = None) -> Iterator[str]:
        """``hdc track-jpid [-a|-p]``: stream app pid/bundle changes.

        Explicit long connection: break out of the generator or pass a timeout.
        """
        command = "track-jpid" + (" " + " ".join(args) if args else "")
        buf = b""
        for chunk in self.client.stream_command(command, serial=self.serial,
                                               timeout=timeout):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r").decode("utf-8", "replace")
        if buf.strip():
            yield buf.rstrip(b"\r").decode("utf-8", "replace")

    def target_boot(self, mode: Optional[str] = None, timeout: float = 15.0) -> None:
        """``hdc target boot [-bootloader|-recovery]``: reboot the device.

        ``mode`` may be ``bootloader`` / ``recovery`` or any argument
        accepted by ``/bin/begetctl reboot``. A bare ``reboot`` is rejected
        by the real server (``Unknown operation command``), which is why the
        official name is used here.
        """
        command = "target boot" + (" %s" % mode if mode else "")
        self._execute(command, check_fail=False, timeout=timeout)

    def bugreport(self, path: Optional[str] = None, timeout: float = 600.0) -> str:
        """``hdc bugreport [FILE]``: export system information.

        ``path`` is a local file; when omitted the report is returned as text.
        """
        if path:
            local = os.path.abspath(path)
            parent = os.path.dirname(local)
            command = "bugreport %s" % local.replace("\\", "/")
            out = self._execute(command, check_fail=False, timeout=timeout)
            text = out.decode("utf-8", "replace").strip()
            if not os.path.isfile(local) and text:
                # some builds stream the report back instead of writing locally
                with open(local, "w", encoding="utf-8") as f:
                    f.write(text)
            _ = parent
            return local
        return self._execute("bugreport", check_fail=False, timeout=timeout).decode(
            "utf-8", "replace").strip()

    # ==================================================================
    # File transfer -- `hdc file send` / `hdc file recv`
    # ==================================================================
    @property
    def sync(self) -> SyncSession:
        """File-transfer namespace (``hdc file send`` / ``hdc file recv``)."""
        return SyncSession(self)

    def send_file(self, local: str, remote: str, timeout: float = 300.0,
                  hold_timestamp: bool = False, update_if_new: bool = False) -> str:
        """``hdc file send [-a|-sync] SOURCE DEST``.

        ``hold_timestamp`` is the ``-a`` flag, ``update_if_new`` the ``-sync``
        flag. The transfer uses the wire-level file-task protocol over a
        socket (no hdc.exe).
        """
        return self._file_task().send_file(
            local, remote, timeout=timeout,
            hold_timestamp=hold_timestamp, update_if_new=update_if_new)

    def recv_file(self, remote: str, local: str, timeout: float = 300.0) -> str:
        """``hdc file recv DEST SOURCE`` (device -> local)."""
        return self._file_task().recv_file(remote, local, timeout=timeout)

    def send_dir(self, local_dir: str, remote_dir: str, timeout: float = 600.0) -> int:
        """Push a directory recursively (single-file sends + ``mkdir``).

        The binary directory-mode protocol is intentionally not used. Returns
        the number of files transferred.
        """
        if not os.path.isdir(local_dir):
            raise HdcError("local directory not found: %s" % local_dir)
        count = 0
        local_dir = os.path.abspath(local_dir)
        self.shell("mkdir -p %s" % remote_dir, timeout=30)
        for root, _dirs, files in os.walk(local_dir):
            rel = os.path.relpath(root, local_dir).replace("\\", "/")
            target = remote_dir.rstrip("/") if rel == "." else \
                remote_dir.rstrip("/") + "/" + rel
            if rel != ".":
                self.shell("mkdir -p %s" % target, timeout=30)
            for name in files:
                src = os.path.join(root, name)
                dst = target.rstrip("/") + "/" + name
                self.send_file(src, dst, timeout=timeout)
                count += 1
        return count

    def pull_dir(self, remote_dir: str, local_dir: str, timeout: float = 600.0) -> int:
        """Pull a directory recursively (file by file)."""
        listing = self.shell("find %s -type f" % remote_dir, timeout=60)
        files = [line.strip() for line in listing.split("\n") if line.strip()]
        if not files:
            raise HdcCommandError("no files under %s" % remote_dir)
        remote_dir = remote_dir.rstrip("/")
        count = 0
        for remote_path in files:
            rel = remote_path[len(remote_dir):].lstrip("/")
            target = os.path.join(local_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
            self.recv_file(remote_path, target, timeout=timeout)
            count += 1
        return count

    def read_file(self, remote: str, timeout: float = 60.0) -> bytes:
        """Read a device file into memory (base64 over a shell connection)."""
        out = self.shell_bytes("base64 %s" % remote, timeout=timeout).decode(
            "ascii", "replace")
        text = "".join(out.split())
        try:
            return base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HdcCommandError(
                "read_file(%s) failed: base64 decode error (%r...)"
                % (remote, out[:120]), output=out) from exc

    def write_file(self, remote: str, data: bytes, timeout: float = 60.0) -> None:
        """Write bytes to a device file (base64 relay; use send_file for big data)."""
        encoded = base64.b64encode(data).decode("ascii")
        out = self.shell("echo %s | base64 -d > %s" % (encoded, remote), timeout=timeout)
        level, message = strip_message_prefix(out)
        if level == "fail":
            raise HdcCommandError(message, output=out)

    # ==================================================================
    # App management -- `hdc install` / `hdc uninstall` / `aa` / `bm`
    # ==================================================================
    def install(self, path: str, *args: str, timeout: float = 600.0) -> str:
        """``hdc install [-r|-s|-w|-u|-p|-g] src``.

        Extra arguments pass straight through to the daemon's ``bm install``,
        e.g. ``d.install("app.hap", "-r", "-g")``.
        """
        opts = " ".join(a for a in args if a) if args else "-r"
        return self._app_task().install(path, options=opts, timeout=timeout)

    def uninstall(self, bundle_name: str, keep_data: bool = False,
                  timeout: float = 300.0) -> str:
        """``hdc uninstall [-n|-k|-s] bundlename``.

        ``keep_data=True`` passes ``-k`` (keep the application data).
        """
        result = self._app_task().uninstall(bundle_name, keep_data=keep_data,
                                            timeout=timeout)
        if "[Fail]" in result or result.startswith("FAIL"):
            raise HdcCommandError(result, output=result)
        return result

    def bm_dump(self, bundle_name: Optional[str] = None, *args: str,
                timeout: float = 30.0) -> str:
        """``bm dump``: bundle manager dump.

        With ``bundle_name`` this is ``bm dump -n <bundle>``; without it the
        full dump (``-a`` for the app list) is returned.
        """
        parts = ["bm", "dump"]
        if bundle_name:
            parts += ["-n", bundle_name]
        parts += [str(a) for a in args]
        return self.shell(parts, timeout=timeout)

    def bm_clean(self, bundle_name: str, *args: str, timeout: float = 60.0) -> None:
        """``bm clean -n <bundle> -d``: clear app data (``-d`` for data, ``-c`` for cache)."""
        parts = ["bm", "clean", "-n", bundle_name]
        parts += [str(a) for a in args] or ["-d"]
        self.shell(parts, timeout=timeout)

    def bm_get(self, *args: str, timeout: float = 30.0) -> str:
        """``bm get --udid``: device udid (and other ``bm get`` variants)."""
        parts = ["bm", "get"] + ([str(a) for a in args] or ["--udid"])
        return self.shell(parts, timeout=timeout)

    def list_apps(self, timeout: float = 30.0) -> List[str]:
        """``bm dump -a``: bundle names of installed apps."""
        out = self.shell("bm dump -a", timeout=timeout)
        apps = [line.strip() for line in out.replace("\r\n", "\n").split("\n")]
        return [a for a in apps if a and " " not in a and not a.startswith("[")]

    def app_info(self, bundle_name: str, timeout: float = 30.0):
        """``bm dump -n <bundle>``: app details (dict when parseable JSON)."""
        out = self.shell("bm dump -n %s" % bundle_name, timeout=timeout)
        if not out or "Fail" in out.split("\n")[0]:
            raise HdcCommandError("app %s not found" % bundle_name, output=out)
        text = out.strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except ValueError:
                pass
        return out

    def app_version(self, bundle_name: str, timeout: float = 30.0) -> str:
        """``bm dump -n <bundle>`` -> ``versionName``."""
        info = self.app_info(bundle_name, timeout=timeout)
        if isinstance(info, dict):
            return str(info.get("versionName", "") or "")
        match = re.search(r'"versionName"\s*:\s*"([^"]+)"', info)
        return match.group(1) if match else ""

    def aa_start(self, bundle_name: str, ability: Optional[str] = None,
                 url: Optional[str] = None, timeout: float = 30.0) -> None:
        """``aa start -b <bundle> [-a <ability>] [-U <url>]``."""
        args = ["aa", "start", "-b", bundle_name]
        if ability:
            args += ["-a", ability]
        if url:
            args += ["-U", url]
        out = self.shell(args, timeout=timeout)
        level, message = strip_message_prefix(out)
        if level == "fail" or re.search(r"[Ee]rror|fail", out):
            raise HdcCommandError(message or out, output=out)

    def aa_force_stop(self, bundle_name: str, timeout: float = 30.0) -> None:
        """``aa force-stop <bundle>``: force-stop an app."""
        self.shell(["aa", "force-stop", bundle_name], timeout=timeout)

    def aa_dump(self, *args: str, timeout: float = 30.0) -> str:
        """``aa dump`` (deprecated in the official docs; kept for completeness)."""
        parts = ["aa", "dump"] + [str(a) for a in args]
        return self.shell(parts, timeout=timeout)

    def app_current(self, timeout: float = 15.0) -> AppCurrentInfo:
        """Foreground app, from ``hidumper -s AbilityManagerService``."""
        out = self.shell(
            ["hidumper", "-s", "AbilityManagerService", "-a", "-a"], timeout=timeout)
        bundle = ""
        ability = ""
        for line in out.splitlines():
            line = line.strip()
            m = re.match(r"Bundle Name\s*\[(.+)\]", line)
            if m and not bundle:
                bundle = m.group(1).strip()
                continue
            m = re.match(r"Ability Name\s*\[(.+)\]", line)
            if m and not ability:
                ability = m.group(1).strip()
        if not bundle:
            raise HdcCommandError(
                "app_current parse failed (hidumper output changed?):\n%s" % out[:400],
                output=out)
        return AppCurrentInfo(bundle, ability)

    # ==================================================================
    # Port forwarding -- `hdc fport` / `hdc rport`
    # ==================================================================
    def fport(self, local: str, remote: str, timeout: float = 30.0) -> None:
        """``hdc fport <localnode> <remotenode>``: forward host port -> device port."""
        self._forward_op(["fport", local, remote], timeout)

    def rport(self, remote: str, local: str, timeout: float = 30.0) -> None:
        """``hdc rport <remotenode> <localnode>``: reverse (device -> host)."""
        self._forward_op(["rport", remote, local], timeout)

    def fport_list(self, timeout: float = 15.0) -> List[str]:
        """``hdc fport ls``: list all forwarding tasks."""
        out = self._execute("fport ls", check_fail=False, timeout=timeout)
        text = out.decode("utf-8", "replace")
        lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
        return [l for l in lines if l and not l.startswith("[") and l.lower() != "(empty)"]

    def fport_remove(self, task: str, timeout: float = 15.0) -> None:
        """``hdc fport rm <task>``: remove one forwarding task."""
        out = self._execute("fport rm %s" % task, check_fail=False, timeout=timeout)
        text = out.decode("utf-8", "replace")
        if not re.search(r"Success|success", text):
            raise HdcCommandError(text.strip() or "fport rm failed", output=text)

    def fport_remove_all(self, timeout: float = 15.0) -> None:
        """``hdc fport rm`` for every listed task."""
        for rule in self.fport_list(timeout=timeout):
            self.fport_remove(rule, timeout=timeout)

    def _forward_op(self, args: List[str], timeout: float) -> None:
        out = self._execute(" ".join(str(a) for a in args),
                            check_fail=False, timeout=timeout)
        text = out.decode("utf-8", "replace")
        if not re.search(r"Success|success", text):
            raise HdcCommandError(text.strip() or "fport failed", output=text)

    def create_connection(self, what: str, port=None,
                          timeout: Optional[float] = None) -> "ForwardedSocket":
        """Set up an ``hdc fport`` rule and return a socket to the device endpoint.

        ``create_connection("tcp", 8010)``        -> local port -> device port
        ``create_connection("unix", "sockname")`` -> device ``localabstract``
        The forwarding task is removed automatically when the socket closes.
        """
        if what == "tcp":
            remote = "tcp:%s" % port
        elif what in ("unix", "localabstract", "localreserved"):
            remote = "localabstract:%s" % port
        else:
            raise ValueError("unsupported connection type: %r" % what)
        lport = _free_tcp_port()
        rule = "tcp:%d %s" % (lport, remote)
        self.fport("tcp:%d" % lport, remote)
        try:
            sock = socket.create_connection(("127.0.0.1", lport), timeout=timeout)
        except OSError:
            self.fport_remove(rule)
            raise
        return ForwardedSocket(sock, rule, self)

    # ==================================================================
    # System parameters -- `param get|ls|set|wait|save`
    # ==================================================================
    def param_get(self, name: Optional[str] = None, timeout: float = 30.0) -> str:
        """``param get [name]``: value of one parameter, or all of them."""
        command = "param get" + (" %s" % name if name else "")
        return self.shell(command, timeout=timeout)

    def param_ls(self, name: Optional[str] = None, recursive: bool = False,
                 timeout: float = 30.0) -> List[str]:
        """``param ls [-r] [name]``: matching parameter lines."""
        command = "param ls" + (" -r" if recursive else "")
        if name:
            command += " %s" % name
        out = self.shell(command, timeout=timeout)
        return [line.strip() for line in out.replace("\r\n", "\n").split("\n") if line.strip()]

    def param_set(self, name: str, value, timeout: float = 30.0) -> None:
        """``param set <name> <value>``."""
        out = self.shell("param set %s %s" % (name, value), timeout=timeout)
        level, message = strip_message_prefix(out)
        if level == "fail":
            raise HdcCommandError(message, output=out)

    def param_wait(self, name: str, value: Optional[str] = None,
                   timeout: float = 30.0) -> bool:
        """``param wait <name> [value] [timeout]``: wait for a value match.

        Returns ``True`` when the parameter matched; ``False`` on timeout
        (the device prints a fail message in that case).
        """
        command = "param wait %s" % name
        if value is not None:
            command += " %s" % value
        command += " %d" % int(timeout)
        out = self.shell(command, timeout=timeout + 10)
        return not re.search(r"[Ff]ail|timeout", out)

    def param_save(self, timeout: float = 30.0) -> None:
        """``param save``: persist ``persist.*`` parameters."""
        self.shell("param save", timeout=timeout)

    def get_prop(self, name: str, timeout: float = 15.0) -> str:
        """``param get <name>`` parsed down to the value."""
        out = self.shell("param get %s" % name, timeout=timeout)
        return _parse_param_value(out, name)

    def get_props(self, timeout: float = 30.0) -> dict:
        """``param get`` parsed into ``{name: value}``."""
        out = self.shell("param get", timeout=timeout)
        props = {}
        for line in out.replace("\r\n", "\n").split("\n"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            name, _, value = line.partition("=")
            props[name.strip()] = value.strip()
        return props

    def device_info(self, timeout: float = 30.0) -> DeviceInfo:
        """Device info assembled from ``param get`` (model/brand/OS/API ...)."""
        return DeviceInfo(self.serial, self.get_props(timeout=timeout))

    # ==================================================================
    # UI input -- `uitest uiInput <click|doubleClick|longClick|fling|swipe|
    # drag|dircFling|inputText|text|keyEvent>`
    # ==================================================================
    def click(self, x: float, y: float, timeout: float = 15.0) -> None:
        """``uitest uiInput click <x> <y>``."""
        self.shell(["uitest", "uiInput", "click", int(x), int(y)], timeout=timeout)

    def double_click(self, x: float, y: float, timeout: float = 15.0) -> None:
        """``uitest uiInput doubleClick <x> <y>``."""
        self.shell(["uitest", "uiInput", "doubleClick", int(x), int(y)], timeout=timeout)

    def long_click(self, x: float, y: float, timeout: float = 15.0) -> None:
        """``uitest uiInput longClick <x> <y>``."""
        self.shell(["uitest", "uiInput", "longClick", int(x), int(y)], timeout=timeout)

    def fling(self, x1: float, y1: float, x2: float, y2: float,
              speed: int = 500, timeout: float = 15.0) -> None:
        """``uitest uiInput fling <x1> <y1> <x2> <y2> <speed>`` (quick swipe)."""
        self.shell(["uitest", "uiInput", "fling", int(x1), int(y1),
                    int(x2), int(y2), int(speed)], timeout=timeout)

    def swipe(self, x1: float, y1: float, x2: float, y2: float,
              speed: int = 500, timeout: float = 15.0) -> None:
        """``uitest uiInput swipe <x1> <y1> <x2> <y2> [speed]``."""
        args = ["uitest", "uiInput", "swipe", int(x1), int(y1), int(x2), int(y2)]
        if speed is not None:
            args.append(int(speed))
        self.shell(args, timeout=timeout)

    def drag(self, x1: float, y1: float, x2: float, y2: float,
             speed: int = 500, timeout: float = 15.0) -> None:
        """``uitest uiInput drag <x1> <y1> <x2> <y2> [speed]``."""
        args = ["uitest", "uiInput", "drag", int(x1), int(y1), int(x2), int(y2)]
        if speed is not None:
            args.append(int(speed))
        self.shell(args, timeout=timeout)

    def dirc_fling(self, direction: int, speed: int = 500,
                   timeout: float = 15.0) -> None:
        """``uitest uiInput dircFling <direction> [speed]``.

        Direction: 0 = left, 1 = right, 2 = up, 3 = down.
        """
        self.shell(["uitest", "uiInput", "dircFling", int(direction), int(speed)],
                   timeout=timeout)

    def input_text(self, x: float, y: float, text: str,
                   timeout: float = 15.0) -> None:
        """``uitest uiInput inputText <x> <y> <text>`` (focus the field first)."""
        self.shell(["uitest", "uiInput", "inputText", int(x), int(y), text],
                   timeout=timeout)

    def text(self, content: str, timeout: float = 15.0) -> None:
        """``uitest uiInput text <content>``: type into the focused field."""
        self.shell(["uitest", "uiInput", "text", content], timeout=timeout)

    def key_event(self, key, timeout: float = 15.0) -> None:
        """``uitest uiInput keyEvent <key>``.

        ``key`` accepts a :class:`KeyCode`, an int, or a documented name such
        as ``"Back"`` / ``"Home"``.
        """
        self.shell(["uitest", "uiInput", "keyEvent", key], timeout=timeout)

    def volume_up(self, timeout: float = 15.0) -> None:
        """``uitest uiInput keyEvent 16`` (KEYCODE_VOLUME_UP)."""
        self.key_event(KeyCode.VOLUME_UP, timeout=timeout)

    def volume_down(self, timeout: float = 15.0) -> None:
        """``uitest uiInput keyEvent 17`` (KEYCODE_VOLUME_DOWN)."""
        self.key_event(KeyCode.VOLUME_DOWN, timeout=timeout)

    def uitest_screen_cap(self, path: Optional[str] = None,
                          display_id: Optional[int] = None,
                          timeout: float = 30.0) -> str:
        """``uitest screenCap [-p <path>] [-d <displayId>]``.

        The path must be under ``/data/local/tmp/``; returns it.
        """
        target = path or "/data/local/tmp/hdcutils_screencap_%d.png" % int(
            time.time() * 1000) % 100000
        parts = ["uitest", "screenCap", "-p", target]
        if display_id is not None:
            parts += ["-d", str(display_id)]
        out = self.shell(parts, timeout=timeout)
        if re.search(r"[Ee]rror|[Ff]ail", out):
            raise HdcCommandError(out.strip(), output=out)
        return target

    # ==================================================================
    # Screen & power -- `snapshot_display` / `power-shell` / `hidumper`
    # ==================================================================
    def screenshot(
        self,
        save_path: Optional[str] = None,
        display_id: Optional[int] = None,
        timeout: float = 30.0,
    ):
        """Take a screenshot.

        Prefers ``snapshot_display -f <remote>``, falling back to
        ``uitest screenCap -p <remote>``; the file comes back over the socket
        file protocol. Returns ``PIL.Image`` when Pillow is installed,
        otherwise raw JPEG bytes; saves to ``save_path`` when given.
        """
        data = self.screenshot_data(display_id=display_id, timeout=timeout)
        if save_path:
            with open(save_path, "wb") as f:
                f.write(data)
        try:
            from PIL import Image
        except ImportError:
            return data if save_path is None else None
        return Image.open(io.BytesIO(data))

    def screenshot_data(self, display_id: Optional[int] = None,
                        timeout: float = 30.0) -> bytes:
        """Screenshot as raw JPEG bytes (``snapshot_display`` / ``uitest screenCap``)."""
        remote = "/data/local/tmp/hdcutils_shot_%d_%d.jpeg" % (
            os.getpid(), int(time.time() * 1000) % 100000)
        if display_id is None:
            commands = ["snapshot_display -f %s" % remote]
        else:
            commands = ["snapshot_display -f %s -i %d" % (remote, display_id)]
        commands.append("uitest screenCap -p %s" % remote)
        last_error = None
        data = None
        for cmd in commands:
            try:
                out = self.shell(cmd, timeout=timeout)
                if re.search(r"[Ee]rror|not found|No such|[Ff]ail", out):
                    last_error = HdcCommandError(out)
                    continue
                data = self.read_file(remote, timeout=timeout)
                break
            except (HdcCommandError, HdcError) as exc:
                last_error = exc
        if data is None:
            raise HdcCommandError(
                "screenshot failed: both snapshot_display and uitest screenCap failed"
                + (" (%s)" % last_error if last_error else ""))
        self.shell("rm -f %s" % remote, timeout=10)
        return data

    def window_size(self, timeout: float = 30.0) -> WindowSize:
        """Screen resolution (width, height), parsed from the screenshot JPEG."""
        data = self.screenshot_data(timeout=timeout)
        width, height = _jpeg_size(data)
        return WindowSize(width, height)

    def power_shell(self, command: str, timeout: float = 15.0) -> str:
        """``power-shell <command>``: device power-state transitions.

        Documented commands include ``wakeup``, ``suspend``, ``setmode``,
        ``timeout``; see the power-shell tool reference.
        """
        return self.shell(["power-shell", command], timeout=timeout)

    def screen_on(self, timeout: float = 15.0) -> None:
        """``power-shell wakeup``: turn the screen on."""
        self.power_shell("wakeup", timeout=timeout)

    def screen_off(self, timeout: float = 15.0) -> None:
        """``power-shell suspend``: turn the screen off."""
        self.power_shell("suspend", timeout=timeout)

    def unlock(self, timeout: float = 15.0) -> None:
        """Wake the screen and swipe up (works on password-free lock screens)."""
        self.screen_on(timeout=timeout)
        time.sleep(0.3)
        size = self.window_size(timeout=timeout)
        self.swipe(size.width / 2, size.height * 0.8,
                   size.width / 2, size.height * 0.2, timeout=timeout)

    def hidumper(self, *args, timeout: float = 30.0) -> str:
        """``hidumper [-s <service>] [-a] ...``: system information export."""
        return self.shell(["hidumper"] + [str(a) for a in args], timeout=timeout)

    def battery(self, timeout: float = 15.0) -> dict:
        """Battery info from ``hidumper -s BatteryService`` (best-effort parse)."""
        out = self.shell(
            ["hidumper", "-s", "BatteryService", "-a", "-i"], timeout=timeout)
        info = {}
        for line in out.splitlines():
            if ":" in line:
                key, _, value = line.partition(":")
                key, value = key.strip(), value.strip()
                low = key.lower()
                if low in ("capacity", "charge state", "voltage", "battery capacity"):
                    info[low.replace(" ", "_")] = value
        if not info:
            raise HdcCommandError(
                "battery parse failed (hidumper output changed?):\n%s" % out[:400],
                output=out)
        return info

    def is_screen_on(self, timeout: float = 15.0) -> bool:
        """Screen state from ``hidumper -s PowerManagerService``."""
        out = self.shell(
            ["hidumper", "-s", "PowerManagerService", "-a", "-s"], timeout=timeout)
        match = re.search(r"[Ss]creen\s*[Ss]tate[:\s]+(\w+)", out)
        if match:
            return match.group(1).upper() in ("ON", "AWAKE")
        match = re.search(r"[\s\"](off|on)[\s\"]", out, re.IGNORECASE)
        if match:
            return match.group(1).lower() == "on"
        raise HdcCommandError(
            "is_screen_on parse failed:\n%s" % out[:400], output=out)

    # ==================================================================
    # Device connection helpers (the client-level commands live on HdcClient)
    # ==================================================================
    def tmode_port(self, port: int = 10123, timeout: float = 30.0) -> str:
        """``hdc tmode port <port>``: open the device network channel.

        Afterwards connect with :meth:`HdcClient.connect`. ``tmode usb`` is
        deprecated since hdc 3.1.0e -- use the device's USB toggle instead.
        """
        out = self._execute("tmode port %d" % int(port), check_fail=False,
                            timeout=timeout)
        return out.decode("utf-8", "replace").strip()

    def tmode_port_close(self, timeout: float = 15.0) -> str:
        """``hdc tmode port close``: close the device network channel."""
        out = self._execute("tmode port close", check_fail=False, timeout=timeout)
        return out.decode("utf-8", "replace").strip()

    def smode(self, timeout: float = 15.0) -> str:
        """``hdc smode``: restart the daemon in root mode (needs a permissive image)."""
        out = self._execute("smode", check_fail=False, timeout=timeout)
        return out.decode("utf-8", "replace").strip()

    def wait(self, timeout: float = 30.0, poll_interval: float = 1.0):
        """``hdc wait``: block until this device is online again."""
        return self.client.wait(self.serial, timeout=timeout,
                                poll_interval=poll_interval)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _execute(self, command: str, completion: Optional[str] = None,
                 check_fail: bool = True, timeout: Optional[float] = None,
                 idle_window: float = DEFAULT_IDLE_WINDOW) -> bytes:
        return self.client._execute(
            command, serial=self.serial, completion=completion,
            check_fail=check_fail, timeout=timeout, idle_window=idle_window,
        )

    def _file_task(self) -> FileTask:
        self.client._ensure()
        return FileTask(self.client.host, self.client.port,
                        connect_timeout=self.client.connect_timeout,
                        connect_key=self.serial)

    def _app_task(self) -> AppTask:
        self.client._ensure()
        return AppTask(self.client.host, self.client.port,
                       connect_timeout=self.client.connect_timeout,
                       connect_key=self.serial)


class ForwardedSocket(socket.socket):
    """Socket returned by :meth:`HdcDevice.create_connection`.

    Adopts the wrapped socket's file descriptor so it behaves like any
    socket; closing it also removes the ``hdc fport`` task. The descriptor's
    non-blocking flag is reset first, otherwise ``recv`` can raise
    ``BlockingIOError`` (WinError 10035) on Windows.
    """

    def __init__(self, sock: socket.socket, rule: str, device: "HdcDevice"):
        self._forward_rule = rule
        self._device = device
        sock.settimeout(None)
        fd = sock.detach()
        super().__init__(fileno=fd)

    def close(self) -> None:
        try:
            super().close()
        finally:
            try:
                self._device.fport_remove(self._forward_rule)
            except Exception:
                pass


def _free_tcp_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _parse_param_value(out: str, name: str) -> str:
    for line in out.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


def _jpeg_size(data: bytes) -> Tuple[int, int]:
    """Parse the JPEG SOF marker to obtain (width, height)."""
    if len(data) < 4 or data[0] != 0xFF:
        raise HdcCommandError("screenshot data is not a JPEG stream")
    idx = 2
    while idx + 9 < len(data):
        if data[idx] != 0xFF:
            idx += 1
            continue
        marker = data[idx + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD9:
            idx += 2
            continue
        if idx + 4 > len(data):
            break
        seg_len = int.from_bytes(data[idx + 2: idx + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[idx + 5: idx + 7], "big")
            width = int.from_bytes(data[idx + 7: idx + 9], "big")
            return width, height
        idx += 2 + seg_len
    raise HdcCommandError("no JPEG SOF marker found in screenshot data")
