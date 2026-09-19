# -*- coding: utf-8 -*-
"""Device API, modeled after ``adbutils.AdbDevice``.

All operations talk to the hdc server over plain sockets -- **no hdc.exe
involved** (its binary version and install location do not matter):

* shell / interactive shell / streaming logs -- daemon-side commands, end on EOF;
* file send/recv and app install/uninstall -- the wire-level file/app task
  protocol (``_task.py``, verified against the open-source hdc sources);
* input / screenshot / power -- plain shell commands (``uitest uiInput``,
  ``snapshot_display``, ``power-shell``), the same approach adbutils uses with
  ``input``/``am``/``pm``. This is low-level input injection only; there is no
  UI automation layer (no element selectors, no device-side test agent).

Not implemented (by design): app sandbox (-b bundlename), lz4 compression (-z),
and the binary directory-mode protocol; directory transfers are composed from
single-file transfers plus ``mkdir`` in ``send_dir``/``pull_dir``.
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

__all__ = ["HdcDevice", "DeviceInfo", "KeyCode", "WindowSize", "AppCurrentInfo", "Prop"]


class KeyCode:
    """Common OpenHarmony key codes (foundation/multimodalinput KeyCodes).

    Only frequent keys are listed; pass raw ints for anything else. The
    ``uitest uiInput keyEvent`` command also accepts names such as ``"Back"``.
    """

    HOME = 1
    BACK = 2
    MENU = 3
    VOLUME_UP = 16
    VOLUME_DOWN = 17
    POWER = 18


class WindowSize(NamedTuple):
    """Screen size in pixels (adbutils-compatible named tuple)."""

    width: int
    height: int


class AppCurrentInfo(NamedTuple):
    """Foreground app info (adbutils-compatible fields)."""

    package: str
    activity: str


class Prop:
    """adbutils-style property accessor: ``d.prop.get("name")`` / ``d.prop["name"]``."""

    def __init__(self, device: "HdcDevice"):
        self._device = device

    def get(self, name: str, timeout: float = 15.0) -> str:
        return self._device.get_prop(name, timeout=timeout)

    __call__ = get

    def __getitem__(self, name: str) -> str:
        return self.get(name)


class DeviceInfo:
    """Basic device facts from ``param get`` system parameters."""

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


class HdcSync:
    """adbutils-style ``d.sync`` namespace (one short connection per call)."""

    def __init__(self, device: "HdcDevice"):
        self._device = device

    def push(self, src: str, dst: str, timeout: float = 300.0) -> str:
        """Push a local file to the device."""
        return self._device.send_file(src, dst, timeout=timeout)

    def pull(self, rpath: str, lpath: str, timeout: float = 300.0) -> str:
        """Pull a device file to the local machine."""
        return self._device.recv_file(rpath, lpath, timeout=timeout)

    def read_bytes(self, rpath: str, timeout: float = 60.0) -> bytes:
        """Read a device file into memory (base64 over socket; small files)."""
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
        """Stream device file content via ``cat`` (one short-lived connection)."""
        return self._device.stream_shell("cat %s" % rpath, timeout=timeout)


class HdcDevice:
    """A single HarmonyOS device. Obtain instances via :meth:`HdcClient.device`."""

    def __init__(self, client: "HdcClient", serial: str):
        self.client = client
        self.serial = serial
        self.prop = Prop(self)

    def __repr__(self) -> str:
        return "HdcDevice(serial=%r)" % self.serial

    # ------------------------------------------------------------------
    # Sync namespace (adbutils parity)
    # ------------------------------------------------------------------
    @property
    def sync(self) -> HdcSync:
        """File-transfer namespace mirroring ``adbutils.AdbDevice.sync``."""
        return HdcSync(self)

    # ------------------------------------------------------------------
    # Internal execution
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

    # ------------------------------------------------------------------
    # shell (adbutils: shell / shell2 / open_shell / stream)
    # ------------------------------------------------------------------
    def shell(self, cmd, stream: bool = False, timeout: Optional[float] = None,
              encoding: str = "utf-8"):
        """Run a command on the device, returning merged stdout/stderr text.

        ``cmd`` accepts a str or a list (joined with spaces; pass a str when
        quoting is needed). hdc does not propagate exit codes -- use
        :meth:`shell2` when the return code matters. With ``stream=True``
        returns a generator of raw output chunks instead (explicit long
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
        """Same as :meth:`shell`, returning raw bytes."""
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(c) for c in cmd)
        cmd = str(cmd)
        if cmd.startswith("shell "):
            cmd = cmd[len("shell "):]
        return self._execute("shell " + cmd, completion="stream",
                             check_fail=False, timeout=timeout)

    def shell2(self, cmd, timeout: Optional[float] = None) -> Tuple[str, int]:
        """Run a command, returning ``(output, returncode)``.

        hdc does not report exit codes; this appends ``; echo __RC__$?`` to the
        command, which works for virtually all commands.
        """
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(c) for c in cmd)
        text = self.shell("%s; echo __RC__$?" % cmd, timeout=timeout)
        match = re.search(r"__RC__(\d+)\s*$", text)
        if match:
            return text[: match.start()].rstrip("\r\n"), int(match.group(1))
        return text, -1

    def open_shell(self, initial: Optional[str] = None) -> "ShellSession":
        """Open an interactive shell session (long connection; ``close()`` after use).

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
        """Run a command and yield raw output chunks while it runs."""
        if isinstance(cmd, (list, tuple)):
            cmd = " ".join(str(c) for c in cmd)
        return self.client.stream_command("shell " + str(cmd), serial=self.serial,
                                          timeout=timeout)

    def stream_lines(self, cmd, timeout: Optional[float] = None,
                     encoding: str = "utf-8") -> Iterator[str]:
        """Run a command and yield output line by line."""
        buf = b""
        for chunk in self.stream_shell(cmd, timeout=timeout):
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r").decode(encoding, "replace")
        if buf.strip():
            yield buf.rstrip(b"\r").decode(encoding, "replace")

    def hilog(self, *args, timeout: Optional[float] = None) -> Iterator[str]:
        """Stream device logs (native ``hdc hilog``).

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

    def logcat(self, *args, timeout: Optional[float] = None) -> Iterator[str]:
        """adbutils-compatible alias of :meth:`hilog`."""
        yield from self.hilog(*args, timeout=timeout)

    # ------------------------------------------------------------------
    # Files (pure socket file-task protocol; adbutils sync parity)
    # ------------------------------------------------------------------
    def send_file(self, local: str, remote: str, timeout: float = 300.0,
                  hold_timestamp: bool = False, update_if_new: bool = False) -> str:
        """Push a local file to the device (``hdc file send`` protocol)."""
        return self._file_task().send_file(
            local, remote, timeout=timeout,
            hold_timestamp=hold_timestamp, update_if_new=update_if_new)

    def recv_file(self, remote: str, local: str, timeout: float = 300.0) -> str:
        """Pull a device file to the local machine (``hdc file recv`` protocol)."""
        return self._file_task().recv_file(remote, local, timeout=timeout)

    push = send_file
    pull = recv_file

    def send_dir(self, local_dir: str, remote_dir: str, timeout: float = 600.0) -> int:
        """Push a directory recursively (single-file sends + ``mkdir``;
        the binary directory-mode protocol is intentionally not used).
        Returns the number of files transferred."""
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
        """Pull a device directory recursively (file by file via ``find``)."""
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
        """Read a device file into memory (base64 over socket; small files)."""
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

    # ------------------------------------------------------------------
    # Apps (pure socket app-task protocol)
    # ------------------------------------------------------------------
    def install(self, path: str, *args: str, timeout: float = 600.0) -> str:
        """Install a .hap/.hsp/.app package (``hdc install [-r] <path>``).

        Extra args pass through, e.g. ``d.install("app.hap", "-r", "-g")``.
        """
        opts = " ".join(a for a in args if a) if args else "-r"
        return self._app_task().install(path, options=opts, timeout=timeout)

    def uninstall(self, bundle_name: str, keep_data: bool = False,
                  timeout: float = 300.0) -> str:
        """Uninstall an app (``hdc uninstall [-k] <bundle>``)."""
        result = self._app_task().uninstall(bundle_name, keep_data=keep_data,
                                            timeout=timeout)
        if "[Fail]" in result or result.startswith("FAIL"):
            raise HdcCommandError(result, output=result)
        return result

    def list_apps(self, timeout: float = 30.0) -> List[str]:
        """List installed app bundle names (``bm dump -a``)."""
        out = self.shell("bm dump -a", timeout=timeout)
        apps = [line.strip() for line in out.replace("\r\n", "\n").split("\n")]
        return [a for a in apps if a and " " not in a and not a.startswith("[")]

    list_packages = list_apps

    def app_info(self, bundle_name: str, timeout: float = 30.0):
        """App details (``bm dump -n <bundle>``); dict when parseable JSON."""
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
        """Get the app versionName."""
        info = self.app_info(bundle_name, timeout=timeout)
        if isinstance(info, dict):
            return str(info.get("versionName", "") or "")
        match = re.search(r'"versionName"\s*:\s*"([^"]+)"', info)
        return match.group(1) if match else ""

    def app_start(self, bundle_name: str, ability: Optional[str] = None,
                  url: Optional[str] = None, timeout: float = 30.0) -> None:
        """Start an app (``aa start -b <bundle> [-a <ability>] [-U <url>]``)."""
        args = ["aa", "start", "-b", bundle_name]
        if ability:
            args += ["-a", ability]
        if url:
            args += ["-U", url]
        out = self.shell(args, timeout=timeout)
        level, message = strip_message_prefix(out)
        if level == "fail" or re.search(r"[Ee]rror|fail", out):
            raise HdcCommandError(message or out, output=out)

    def open_browser(self, url: str, timeout: float = 30.0) -> None:
        """Open a URL through the system route (``aa start -U <url>``)."""
        out = self.shell(["aa", "start", "-U", url], timeout=timeout)
        if re.search(r"[Ee]rror|[Ff]ail", out):
            raise HdcCommandError(out, output=out)

    open_url = open_browser

    def app_stop(self, bundle_name: str, timeout: float = 30.0) -> None:
        """Stop an app (``aa force-stop <bundle>``)."""
        self.shell(["aa", "force-stop", bundle_name], timeout=timeout)

    def app_clear(self, bundle_name: str, timeout: float = 60.0) -> None:
        """Clear app data (``bm clean -n <bundle> -d``)."""
        self.shell(["bm", "clean", "-n", bundle_name, "-d"], timeout=timeout)

    def app_current(self, timeout: float = 15.0) -> AppCurrentInfo:
        """Get the foreground app (``hidumper -s AbilityManagerService``)."""
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

    # ------------------------------------------------------------------
    # Port forwarding (adbutils: forward / reverse)
    # ------------------------------------------------------------------
    def fport(self, local: str, remote: str, timeout: float = 30.0) -> None:
        """Add a forward rule (``hdc -t key fport tcp:<lport> tcp:<rport>``)."""
        self._forward_op(["fport", local, remote], timeout)

    def rport(self, remote: str, local: str, timeout: float = 30.0) -> None:
        """Add a reverse rule (``rport tcp:<rport> tcp:<lport>``)."""
        self._forward_op(["rport", remote, local], timeout)

    def fport_list(self, timeout: float = 15.0) -> List[str]:
        """List forward rules (``fport ls``)."""
        out = self._execute("fport ls", check_fail=False, timeout=timeout)
        text = out.decode("utf-8", "replace")
        lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
        return [l for l in lines if l and not l.startswith("[") and l.lower() != "(empty)"]

    def fport_remove(self, task: str, timeout: float = 15.0) -> None:
        """Remove one forward rule (``fport rm <rule>``)."""
        out = self._execute("fport rm %s" % task, check_fail=False, timeout=timeout)
        text = out.decode("utf-8", "replace")
        if not re.search(r"Success|success", text):
            raise HdcCommandError(text.strip() or "fport rm failed", output=text)

    def fport_remove_all(self, timeout: float = 15.0) -> None:
        """Remove all forward rules."""
        for rule in self.fport_list(timeout=timeout):
            self.fport_remove(rule, timeout=timeout)

    forward = fport
    reverse = rport
    forward_list = fport_list

    def forward_remove(self, task: str, timeout: float = 15.0) -> None:
        """adbutils-compatible alias of :meth:`fport_remove`."""
        self.fport_remove(task, timeout=timeout)

    def _forward_op(self, args: List[str], timeout: float) -> None:
        out = self._execute(" ".join(str(a) for a in args),
                            check_fail=False, timeout=timeout)
        text = out.decode("utf-8", "replace")
        if not re.search(r"Success|success", text):
            raise HdcCommandError(text.strip() or "fport failed", output=text)

    def create_connection(self, what: str, port=None,
                          timeout: Optional[float] = None) -> "ForwardedSocket":
        """adbutils-compatible tunnel: a socket connected to a device endpoint.

        ``create_connection("tcp", 8010)``          -> local port -> device port
        ``create_connection("unix", "sockname")``   -> localabstract on device
        The fport rule is removed automatically when the socket is closed.
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

    # ------------------------------------------------------------------
    # Device info (adbutils: prop / window_size / battery)
    # ------------------------------------------------------------------
    def get_prop(self, name: str, timeout: float = 15.0) -> str:
        """Read a system parameter (``param get <name>``)."""
        out = self.shell("param get %s" % name, timeout=timeout)
        return _parse_param_value(out, name)

    def get_props(self, timeout: float = 30.0) -> dict:
        """Read all system parameters (``param get``) as ``{name: value}``."""
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
        """Device info (model / brand / OS version / API version ...)."""
        return DeviceInfo(self.serial, self.get_props(timeout=timeout))

    def window_size(self, timeout: float = 30.0) -> WindowSize:
        """Screen resolution (width, height), parsed from the screenshot JPEG
        SOF marker -- rotation-aware."""
        data = self.screenshot_data(timeout=timeout)
        width, height = _jpeg_size(data)
        return WindowSize(width, height)

    def battery(self, timeout: float = 15.0) -> dict:
        """Battery info (``hidumper -s BatteryService``; best-effort parse)."""
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

    def reboot(self, mode: Optional[str] = None, timeout: float = 15.0) -> None:
        """Reboot the device (``mode`` may be ``"bootloader"``/``"recovery"``)."""
        command = "reboot" + (" %s" % mode if mode else "")
        self._execute(command, check_fail=False, timeout=timeout)

    def wait_for_device(self, timeout: float = 30.0, poll_interval: float = 1.0):
        """Wait until this device is back online (e.g. after a reboot)."""
        return self.client.wait_for_device(self.serial, timeout=timeout,
                                           poll_interval=poll_interval)

    # ------------------------------------------------------------------
    # Screen / power / root / tcpip (adbutils parity)
    # ------------------------------------------------------------------
    def screen_on(self, timeout: float = 15.0) -> None:
        """Turn the screen on (``power-shell wakeup``)."""
        self.shell(["power-shell", "wakeup"], timeout=timeout)

    def screen_off(self, timeout: float = 15.0) -> None:
        """Turn the screen off (``power-shell suspend``)."""
        self.shell(["power-shell", "suspend"], timeout=timeout)

    def is_screen_on(self, timeout: float = 15.0) -> bool:
        """Whether the screen is on (``hidumper -s PowerManagerService``)."""
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

    def unlock(self, timeout: float = 15.0) -> None:
        """Wake the screen and swipe up to unlock (password-free lock only)."""
        self.screen_on(timeout=timeout)
        time.sleep(0.3)
        size = self.window_size(timeout=timeout)
        self.swipe(size.width / 2, size.height * 0.8,
                   size.width / 2, size.height * 0.2, timeout=timeout)

    def volume_up(self, timeout: float = 15.0) -> None:
        self.keyevent(KeyCode.VOLUME_UP, timeout=timeout)

    def volume_down(self, timeout: float = 15.0) -> None:
        self.keyevent(KeyCode.VOLUME_DOWN, timeout=timeout)

    def root(self, timeout: float = 15.0) -> str:
        """Switch the daemon to root mode (``hdc smode``; the adbutils
        ``root()`` analogue). Requires an image that permits it."""
        out = self._execute("smode", check_fail=False, timeout=timeout)
        return out.decode("utf-8", "replace").strip()

    def tcpip(self, port: int = 10123, timeout: float = 30.0) -> str:
        """Switch the daemon to TCP mode (``hdc tmode port <port>``);
        afterwards call ``client.connect("ip:port")``."""
        out = self._execute("tmode port %d" % int(port), check_fail=False,
                            timeout=timeout)
        return out.decode("utf-8", "replace").strip()

    # ------------------------------------------------------------------
    # Input (uitest uiInput -- the OpenHarmony equivalent of adb ``input``;
    # plain shell commands, no UI automation layer involved)
    # ------------------------------------------------------------------
    def click(self, x: float, y: float, timeout: float = 15.0) -> None:
        """Tap the screen (``uitest uiInput click``)."""
        self.shell(["uitest", "uiInput", "click", int(x), int(y)], timeout=timeout)

    def double_click(self, x: float, y: float, timeout: float = 15.0) -> None:
        self.shell(["uitest", "uiInput", "doubleClick", int(x), int(y)], timeout=timeout)

    def long_click(self, x: float, y: float, timeout: float = 15.0) -> None:
        self.shell(["uitest", "uiInput", "longClick", int(x), int(y)], timeout=timeout)

    def swipe(self, x1: float, y1: float, x2: float, y2: float,
              speed: Optional[int] = None, timeout: float = 15.0) -> None:
        """Swipe (``uitest uiInput swipe x1 y1 x2 y2 [speed]``)."""
        args = ["uitest", "uiInput", "swipe", int(x1), int(y1), int(x2), int(y2)]
        if speed is not None:
            args.append(int(speed))
        self.shell(args, timeout=timeout)

    def drag(self, x1: float, y1: float, x2: float, y2: float,
             speed: Optional[int] = None, timeout: float = 15.0) -> None:
        args = ["uitest", "uiInput", "drag", int(x1), int(y1), int(x2), int(y2)]
        if speed is not None:
            args.append(int(speed))
        self.shell(args, timeout=timeout)

    def keyevent(self, key, timeout: float = 15.0) -> None:
        """Press a key (``uitest uiInput keyEvent``); ``key`` accepts a
        :class:`KeyCode`, an int, or a name such as ``"Back"``."""
        self.shell(["uitest", "uiInput", "keyEvent", key], timeout=timeout)

    def send_keys(self, text: str, x: Optional[float] = None, y: Optional[float] = None,
                  timeout: float = 15.0) -> None:
        """adbutils-compatible text input; see :meth:`input_text`."""
        self.input_text(text, x, y, timeout=timeout)

    def input_text(self, text: str, x: Optional[float] = None, y: Optional[float] = None,
                   timeout: float = 15.0) -> None:
        """Type text at a position (``uitest uiInput inputText x y text``);
        without coordinates the screen center is tapped first."""
        if x is None or y is None:
            size = self.window_size(timeout=timeout)
            x, y = size.width / 2, size.height / 2
            self.click(x, y, timeout=timeout)
        self.shell(["uitest", "uiInput", "inputText", int(x), int(y), text],
                   timeout=timeout)

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------
    def screenshot(
        self,
        save_path: Optional[str] = None,
        display_id: Optional[int] = None,
        timeout: float = 30.0,
    ):
        """Take a screenshot.

        Prefers ``snapshot_display -f <remote>``, falls back to
        ``uitest screenCap``; the file travels back over the socket protocol.
        Returns ``PIL.Image`` when Pillow is installed, otherwise raw JPEG
        bytes; saves to ``save_path`` when provided.
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
        """Take a screenshot and return raw JPEG bytes."""
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


class ForwardedSocket(socket.socket):
    """adbutils-style tunnelled socket: drops the fport rule on close."""

    def __init__(self, sock: socket.socket, rule: str, device: "HdcDevice"):
        self._forward_rule = rule
        self._device = device
        fd = sock.fileno()
        sock.detach()
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
