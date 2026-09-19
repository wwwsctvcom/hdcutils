# -*- coding: utf-8 -*-
"""Server-management tests: PID-file kill (pure python, no hdc.exe) and
auto-pullup."""
import os
import subprocess
import sys
import time

import pytest

import hdcutils
from hdcutils._server import (
    ensure_server, find_hdc_binary, find_server_pid, kill_server, probe_server,
    server_pid_file, start_server,
)

# Mirror the official hdc client PullupServer: detach a server when absent, then exit
SPAWNER_CLI = '''
import os, subprocess, sys
port = os.environ.get("OHOS_HDC_SERVER_PORT") or "8710"
here = os.path.dirname(os.path.abspath(__file__))
kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
          "stdin": subprocess.DEVNULL}
if sys.platform == "win32":
    kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
else:
    kwargs["start_new_session"] = True
subprocess.Popen([sys.executable, os.path.join(here, "_mock_main.py"), port], **kwargs)
print("MOCKSERIAL1")
'''

MOCK_MAIN = '''
import sys, threading
sys.path.insert(0, %(tests)r)
from _mock import MockHdcServer
server = MockHdcServer(int(sys.argv[1]))
server.start()
print("mock on", server.port)
threading.Event().wait(60)  # 60s TTL so no orphan process lingers after tests
'''


@pytest.fixture()
def spawner_hdc(tmp_path):
    mock_main = tmp_path / "_mock_main.py"
    mock_main.write_text(MOCK_MAIN % {"tests": os.path.dirname(__file__)}, encoding="utf-8")
    script = tmp_path / "spawner.py"
    script.write_text(SPAWNER_CLI, encoding="utf-8")
    if os.name == "nt":
        wrapper = tmp_path / "hdc.bat"
        wrapper.write_text('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, script),
                           encoding="ascii")
    else:
        wrapper = tmp_path / "hdc"
        wrapper.write_text("#!/bin/sh\nexec '%s' '%s' \"$@\"\n" % (sys.executable, script))
        wrapper.chmod(0o755)
    return str(wrapper)


def _free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_probe_server():
    assert probe_server("127.0.0.1", 1, timeout=0.5) in (True, False)


def test_pid_file_path(monkeypatch, tmp_path):
    monkeypatch.setenv("TEMP", str(tmp_path))
    assert server_pid_file().endswith(".HDCServer.pid")


def test_find_server_pid_via_file(monkeypatch, tmp_path):
    import threading

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setattr(hdcutils._server, "server_pid_file",
                            lambda: str(tmp_path / ".HDCServer.pid"))
        (tmp_path / ".HDCServer.pid").write_text(str(proc.pid))
        assert find_server_pid(port=1) == proc.pid
    finally:
        proc.kill()


def test_find_server_pid_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(hdcutils._server, "server_pid_file",
                        lambda: str(tmp_path / ".HDCServer.pid"))
    assert find_server_pid(port=1) is None  # port 1 is never listening


def test_kill_server_via_pid_file(monkeypatch, tmp_path):
    """Replicate hdc kill: read .HDCServer.pid -> SIGKILL -> process exits."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setattr(hdcutils._server, "server_pid_file",
                            lambda: str(tmp_path / ".HDCServer.pid"))
        monkeypatch.setattr(hdcutils._server, "_pid_listening_on", lambda port: None)
        (tmp_path / ".HDCServer.pid").write_text(str(proc.pid))
        kill_server(port=12345, timeout=5)
        assert proc.poll() is not None  # the process is gone
    finally:
        if proc.poll() is None:
            proc.kill()


def test_kill_server_not_running(monkeypatch, tmp_path):
    monkeypatch.setattr(hdcutils._server, "server_pid_file",
                        lambda: str(tmp_path / ".HDCServer.pid"))
    monkeypatch.setattr(hdcutils._server, "_pid_listening_on", lambda port: None)
    kill_server(port=12345)  # no server: must not raise


def test_start_server_via_spawner(spawner_hdc):
    """The fake hdc pulls the mock server up on the given port (the only exe
    use: launching the server)."""
    port = _free_port()
    assert not probe_server("127.0.0.1", port, timeout=0.5)
    start_server(spawner_hdc, port, timeout=20)
    assert probe_server("127.0.0.1", port, timeout=1.0)
    client = hdcutils.HdcClient(port=port, auto_start=False)
    assert "MOCKSERIAL1" in client.list_targets()


def test_ensure_server_already_running(mock_running_port):
    binary = ensure_server("127.0.0.1", mock_running_port, hdc_path="/no/such/hdc")
    assert binary is None  # port already listening: no binary needed


def test_ensure_server_via_spawner(spawner_hdc):
    port = _free_port()
    binary = ensure_server("127.0.0.1", port, hdc_path=spawner_hdc, timeout=20)
    assert binary == spawner_hdc
    assert probe_server("127.0.0.1", port, timeout=1.0)


def test_ensure_server_no_binary():
    port = _free_port()
    with pytest.raises(Exception):
        ensure_server("127.0.0.1", port, hdc_path=None, timeout=5)


def test_find_hdc_binary_explicit(spawner_hdc):
    assert find_hdc_binary(spawner_hdc) == spawner_hdc
    assert find_hdc_binary("/no/such/hdc") is None


@pytest.fixture()
def mock_running_port(monkeypatch):
    sys.path.insert(0, os.path.dirname(__file__))
    from _mock import MockHdcServer

    server = MockHdcServer(0)
    port = server.start()
    yield port
    server.stop()
