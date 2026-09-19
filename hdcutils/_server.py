# -*- coding: utf-8 -*-
"""hdc server process management and auto-pullup (exe-free).

Based on the open-source hdc sources ``src/host/client.cpp``:

* ``hdc kill`` does **not** use the socket protocol -- the PC client reads
  ``$TMPDIR/.HDCServer.pid`` (``GetLastPID``) and SIGKILLs that PID. This
  library mirrors it: read the PID file first, then fall back to looking up
  the PID by port (netstat/proc), then kill.
* ``hdc start`` / auto-pullup -- ``PullupServer`` spawns ``hdc -m``. The server
  process carries the USB transport and physically must be provided by an hdc
  executable; this is kept as the **only** optional exe use: it pulls up the
  server when absent, and never participates in device operations.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import socket
import subprocess
import time
from typing import List, Optional

from .exceptions import HdcError, HdcServerNotRunning

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

__all__ = [
    "find_hdc_binary", "probe_server", "start_server", "kill_server", "ensure_server",
    "find_server_pid", "server_pid_file",
]

ENV_HDC_PATH = "HDCUTILS_HDC_PATH"

# Common DevEco Studio / Command Line Tools install locations (PATH fallback)
_KNOWN_HDC_PATTERNS: List[str] = [
    r"C:\Program Files\Huawei\DevEco Studio\sdk\default\openharmony\toolchains\hdc.exe",
    r"C:\Program Files\Huawei\DevEco Studio\sdk\*\openharmony\toolchains\hdc.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Huawei", "Sdk", "*", "toolchains", "hdc.exe"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Huawei", "*", "sdk", "*", "toolchains", "hdc.exe"),
    os.path.join(os.environ.get("HM_SDK_HOME", ""), "**", "toolchains", "hdc.exe"),
    "/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/toolchains/hdc",
    "/usr/local/bin/hdc",
]


def find_hdc_binary(explicit: Optional[str] = None) -> Optional[str]:
    """Locate the hdc executable: explicit arg > env var > PATH > common
    install locations.

    **Used only to pull the server up when it is missing**; no device
    operation in this library depends on the binary.
    """
    if explicit and os.path.isfile(explicit):
        return explicit
    env_path = os.environ.get("HDCUTILS_HDC_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    found = shutil.which("hdc")
    if found:
        return found
    for pattern in _KNOWN_HDC_PATTERNS:
        if not pattern:
            continue
        for candidate in glob.glob(pattern) or []:
            if candidate and os.path.isfile(candidate):
                return candidate
    return None


def server_pid_file() -> str:
    """Server PID file path (src/host/client.cpp GetLastPID:
    ``$TMPDIR/.HDCServer.pid``)."""
    tmp = os.environ.get("TMPDIR") or os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp"
    return os.path.join(tmp, ".HDCServer.pid")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def find_server_pid(port: int) -> Optional[int]:
    """Find the hdc server PID: prefer ``.HDCServer.pid``, then look up by port."""
    pid_path = server_pid_file()
    try:
        with open(pid_path, "r", encoding="ascii", errors="ignore") as f:
            pid = int(f.read().strip() or 0)
        if pid > 0 and _pid_alive(pid):
            return pid
    except (OSError, ValueError):
        pass
    return _pid_listening_on(port)


def _pid_listening_on(port: int) -> Optional[int]:
    """Look up a PID by listening port (Windows: netstat -ano; POSIX: ss/lsof/proc)."""
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                timeout=10, creationflags=CREATE_NO_WINDOW
            ).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return None
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(":%d" % port) and parts[3] == "LISTENING":
                return int(parts[4])
        return None
    # POSIX: prefer ss, then lsof, finally /proc/net/tcp
    for cmd in (["ss", "-tlnp"], ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN"]):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in (proc.stdout or "").splitlines():
            match = re.search(r"pid=(\d+)", line) or re.search(r"\b(\d+)\s*$", line)
            if match and (":%d " % port in line or ":%d " % port in line):
                return int(match.group(1))
    # /proc/net/tcp fallback: parse the hex port
    try:
        inode = None
        with open("/proc/net/tcp", "r", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if parts[1].endswith(":%04X" % port) and parts[3] == "0A":
                    inode = parts[9]
                    break
        if inode and inode != "0":
            for pid_dir in glob.glob("/proc/[0-9]*/fd"):
                for fd in glob.glob(os.path.join(pid_dir, "*")):
                    try:
                        target = os.readlink(fd)
                    except OSError:
                        continue
                    if target == "socket:[%s]" % inode:
                        return int(re.search(r"/proc/(\d+)/", fd).group(1))
    except OSError:
        pass
    return None


def probe_server(host: str, port: int, timeout: float = 1.0) -> bool:
    """Probe whether an hdc server is listening (TCP connect)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def start_server(hdc_path: str, port: int, timeout: float = 15.0) -> None:
    """Trigger the official PullupServer via ``hdc list targets`` and poll until
    the port is ready.

    Injects ``OHOS_HDC_SERVER_PORT``/``HDC_SERVER_PORT`` into the child env to
    pin the port. This is the **only** use of the hdc binary: launching the
    server process itself.
    """
    env = dict(os.environ)
    env["OHOS_HDC_SERVER_PORT"] = str(port)
    env["HDC_SERVER_PORT"] = str(port)
    subprocess.run(
        [hdc_path, "list targets"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=env,
        creationflags=CREATE_NO_WINDOW,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_server("127.0.0.1", port, timeout=0.5):
            return
        time.sleep(0.3)
    raise HdcServerNotRunning(
        "hdc server did not start on port %s within %.0fs. "
        "If another hdc server (e.g. DevEco's) is already running on a different port, "
        "kill it first (hdcutils.HdcClient(...).kill_server()), or set HDC_SERVER_PORT "
        "to match it." % (port, timeout)
    )


def kill_server(port: int, timeout: float = 10.0) -> None:
    """Kill the hdc server -- pure-python equivalent of ``hdc kill`` (SIGKILL).

    Order: read ``.HDCServer.pid`` -> look up the PID by port -> os.kill;
    a missing PID is treated as "already exited".
    """
    pid = find_server_pid(port)
    if pid is None:
        return  # no server running
    try:
        if os.name == "nt":
            os.kill(pid, 9)  # TerminateProcess
        else:
            os.kill(pid, 9)
    except OSError as exc:
        raise HdcError("kill hdc server (pid=%s) failed: %s" % (pid, exc)) from exc
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)


def ensure_server(
    host: str,
    port: int,
    hdc_path: Optional[str] = None,
    timeout: float = 15.0,
) -> Optional[str]:
    """Ensure an hdc server is available; returns the resolved binary path
    (possibly None).

    When the port is already listening -> return immediately; otherwise pull
    the server up with the located hdc binary, or raise
    :class:`HdcServerNotRunning` (the server process itself must come from
    an hdc executable).
    """
    if probe_server(host, port, timeout=1.0):
        return find_hdc_binary(hdc_path)
    binary = find_hdc_binary(hdc_path)
    if binary is None:
        raise HdcServerNotRunning(
            "hdc server %s:%s is not running and no hdc binary found "
            "(install HarmonyOS Command Line Tools or set HDCUTILS_HDC_PATH; "
            "the library itself performs all device operations over pure sockets)"
            % (host, port)
        )
    start_server(binary, port, timeout=timeout)
    return binary
