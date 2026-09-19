# -*- coding: utf-8 -*-
"""End-to-end tests against the mock hdc server (socket channel)."""
import base64
import os
import socket
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _mock import FAKE_JPEG, MockHdcServer  # noqa: E402

import hdcutils  # noqa: E402
from hdcutils.exceptions import (  # noqa: E402
    HdcCommandError,
    HdcError,
    HdcProtocolError,
    HdcTimeoutError,
)


@pytest.fixture(params=[False, True], ids=["handshake44", "handshake108"])
def mock_port(request):
    """Start the mock server (covers both 44/108 handshake variants)."""
    server = MockHdcServer(port=0, use_version_handshake=request.param)
    port = server.start()
    yield port
    server.stop()


@pytest.fixture(autouse=True)
def no_hdc_binary(monkeypatch):
    """The host may have a real hdc installed; block binary discovery so tests
    never trigger the real CLI."""
    monkeypatch.setattr(hdcutils.core, "find_hdc_binary", lambda explicit=None: None)


@pytest.fixture()
def client(mock_port):
    return hdcutils.HdcClient(port=mock_port, auto_start=False)


# ---------------------------------------------------------------------
# Connection and device enumeration
# ---------------------------------------------------------------------
def test_server_version(client):
    assert client.server_version().startswith("hdc mock")


def test_list_targets(client):
    assert client.list_targets() == ["MOCKSERIAL1", "MOCKSERIAL2"]


def test_list_targets_verbose(client):
    targets = client.list_targets(verbose=True)
    assert targets[0].connect_key == "MOCKSERIAL1"
    assert targets[0].type == "USB"
    assert targets[0].state == "Connected"
    assert targets[0].addr == ""
    assert targets[1].state == "Connected"
    assert targets[1].addr == "192.168.1.3:10123"
    assert targets[1].type == "TCP"


def test_multi_device_requires_serial(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    # the mock has two devices -> multiple devices must raise and list them
    with pytest.raises(HdcError) as exc:
        client.device()
    assert "MOCKSERIAL1" in str(exc.value)
    # explicit serial works
    d = client.device("MOCKSERIAL1")
    assert d.serial == "MOCKSERIAL1"


def test_wait(client):
    device = client.wait("MOCKSERIAL2", timeout=5)
    assert device.serial == "MOCKSERIAL2"


def test_wait_for_device_timeout(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    with pytest.raises(HdcTimeoutError):
        client.wait("NOEXIST", timeout=1.5, poll_interval=0.2)


# ---------------------------------------------------------------------
# shell
# ---------------------------------------------------------------------
def test_shell(client):
    d = client.device("MOCKSERIAL1")
    assert d.shell("echo hello") == "hello"


def test_shell_list_args(client):
    d = client.device("MOCKSERIAL1")
    assert d.shell(["echo", "abc"]) == "abc"


def test_shell_prop(client):
    d = client.device("MOCKSERIAL1")
    assert d.get_prop("const.product.model") == "HUAWEI Mock Phone"


def test_get_props(client):
    d = client.device("MOCKSERIAL1")
    props = d.get_props()
    assert props["const.product.manufacturer"] == "Huawei"
    assert props["const.ohos.apiversion"] == "12"


def test_device_info(client):
    d = client.device("MOCKSERIAL1")
    info = d.device_info()
    assert info.model == "HUAWEI Mock Phone"
    assert info.api_version == "12"


def test_shell_ex_returncode(client):
    d = client.device("MOCKSERIAL1")
    out, rc = d.shell_ex("echo hello")
    assert out == "hello"
    assert rc == 0


def test_shell_fail_when_no_device(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    d = client.device("NOSUCHSERIAL")
    out = d.shell("echo x")
    assert "No device connected" in out


# ---------------------------------------------------------------------
# Streaming commands
# ---------------------------------------------------------------------
def test_hilog_stream(client):
    d = client.device("MOCKSERIAL1")
    lines = list(d.hilog(timeout=10))
    assert lines == ["hilog line 0", "hilog line 1", "hilog line 2"]


def test_stream_shell_frames(client):
    d = client.device("MOCKSERIAL1")
    frames = list(d.stream_shell("hilog", timeout=10))
    assert len(frames) == 3


def test_shell_sleep_no_output(client):
    """A streaming shell with a long silent period must not break early."""
    d = client.device("MOCKSERIAL1")
    start = time.monotonic()
    out = d.shell("sleep 1.2")
    assert time.monotonic() - start >= 1.0
    assert out == ""


# ---------------------------------------------------------------------
# Connection / forwarding / system
# ---------------------------------------------------------------------
def test_tconn(client):
    out = client.connect("192.168.1.3:10123", timeout=10)
    assert "Connect OK" in out


def test_disconnect(client):
    out = client.disconnect("192.168.1.3:10123")
    assert "Remove OK" in out


def test_fport_roundtrip(client):
    d = client.device("MOCKSERIAL1")
    d.fport("tcp:7000", "tcp:8012")
    assert d.fport_list() == ["tcp:7000 tcp:8012"]
    d.fport_remove("tcp:7000 tcp:8012")


def test_fport_list(client):
    d = client.device("MOCKSERIAL1")
    assert d.fport_list() == []          # no rules yet ("(empty)" filtered)
    d.fport("tcp:7000", "tcp:8012")
    assert d.fport_list() == ["tcp:7000 tcp:8012"]


def test_reboot(client):
    client.device("MOCKSERIAL1").target_boot()  # no exception is the assertion


def test_unknown_command_raises(client):
    with pytest.raises(HdcCommandError) as exc:
        client._execute("frobnicate")
    assert "Unknown command" in str(exc.value)


# ---------------------------------------------------------------------
# Interactive shell
# ---------------------------------------------------------------------
def test_open_shell_roundtrip(client):
    d = client.device("MOCKSERIAL1")
    with d.open_shell() as sh:
        sh.send("ps -ef")
        out = sh.recv(timeout=2)
        assert out is not None and b"ps -ef" in out
    # the session is closed after leaving the with block


def test_open_shell_exit(client):
    d = client.device("MOCKSERIAL1")
    sh = d.open_shell()
    sh.send("exit")
    time.sleep(0.2)
    sh.recv(timeout=1)
    sh.close()


# ---------------------------------------------------------------------
# Screenshot and files (base64 over socket)
# ---------------------------------------------------------------------
def test_screenshot_bytes(client):
    d = client.device("MOCKSERIAL1")
    data = d.screenshot()
    assert data == FAKE_JPEG


def test_screenshot_save(tmp_path, client):
    d = client.device("MOCKSERIAL1")
    target = tmp_path / "shot.jpeg"
    d.screenshot(str(target))
    assert target.read_bytes() == FAKE_JPEG


def test_read_file(client):
    d = client.device("MOCKSERIAL1")
    assert d.read_file("/data/local/tmp/x.jpeg") == FAKE_JPEG


def test_write_file(client):
    d = client.device("MOCKSERIAL1")
    d.write_file("/data/local/tmp/w.txt", b"payload-123")
    assert d.read_file("/data/local/tmp/w.txt") == b"payload-123"


def test_read_file_missing(client):
    d = client.device("MOCKSERIAL1")
    with pytest.raises(HdcCommandError):
        d.read_file("/no/such/file")


# ---------------------------------------------------------------------
# App management (the mock only simulates bm/aa text output)
# ---------------------------------------------------------------------
def test_list_apps(client):
    d = client.device("MOCKSERIAL1")
    assert d.list_apps() == ["com.example.mock", "com.example.other"]


def test_app_info_json(client):
    d = client.device("MOCKSERIAL1")
    info = d.app_info("com.example.mock")
    assert isinstance(info, dict)
    assert info["versionName"] == "1.0.0"
    assert d.app_version("com.example.mock") == "1.0.0"


def test_app_start_stop(client):
    d = client.device("MOCKSERIAL1")
    d.aa_start("com.example.mock")  # success raises nothing
    d.aa_force_stop("com.example.mock")


def test_app_start_fail(client, monkeypatch):
    d = client.device("MOCKSERIAL1")
    monkeypatch.setattr(d, "shell", lambda *a, **kw: "[Fail]Operation failed")
    with pytest.raises(HdcCommandError):
        d.aa_start("com.example.mock")


# ---------------------------------------------------------------------
# Protocol robustness
# ---------------------------------------------------------------------
def test_connect_closed_server():
    """auto_start=False against a closed port must raise a server error."""
    client = hdcutils.HdcClient(port=1, auto_start=False)
    with pytest.raises((HdcError, OSError, HdcProtocolError)):
        client.server_version()


def test_large_frame(mock_port):
    """Large payload frames must arrive intact (fragment reassembly)."""
    big = "x" * 300000
    d = hdcutils.HdcClient(port=mock_port, auto_start=False).device("MOCKSERIAL1")
    out = d.shell("echo " + big)  # the mock echoes the whole frame
    assert len(out) >= 300000


def test_idle_window_completion(client):
    """After a local command (tconn) the server keeps the connection open;
    idle-window completion must still finish it."""
    start = time.monotonic()
    out = client.connect("192.168.1.3:10123", timeout=10)
    assert "Connect OK" in out
    assert time.monotonic() - start < 5  # must not hit the full timeout


# ---------------------------------------------------------------------
# Short-connection model (adbutils-style: one connection per command,
# ---------------------------------------------------------------------
def test_local_command_no_idle_tail(client):
    """Hot-path commands finish on first frame: lingering must be far below
    the old 1s idle window."""
    start = time.monotonic()
    client.list_targets()
    elapsed = time.monotonic() - start
    # the mock answers instantly; with the old 1s idle window elapsed >= 1.0
    assert elapsed < 0.9, "list targets kept connection idle for %.2fs" % elapsed


def test_checkserver_no_idle_tail(client):
    start = time.monotonic()
    client.server_version()
    assert time.monotonic() - start < 0.9


def test_slow_response_first_frame_completion(mock_port):
    """The server answers after 0.3s and keeps the connection open; finish
    right after the response (no idle-window wait)."""
    from _mock import MockHdcServer
    import threading as _th

    server = _running_server(mock_port)
    orig = MockHdcServer.__dict__["_frame"]
    counter = {"n": 0}

    def delayed_frame(payload: bytes) -> bytes:
        counter["n"] += 1
        if counter["n"] == 2:  # the first list targets response frame
            time.sleep(0.3)
        return MockHdcServer._frame_orig(payload)

    MockHdcServer._frame_orig = staticmethod(orig)
    MockHdcServer._frame = staticmethod(delayed_frame)
    try:
        client = hdcutils.HdcClient(port=mock_port, auto_start=False)
        start = time.monotonic()
        targets = client.list_targets()
        elapsed = time.monotonic() - start
        assert targets == ["MOCKSERIAL1", "MOCKSERIAL2"]
        # disconnect right after the first frame: no idle tail
        assert elapsed < 0.45, "response took %.2fs (idle tail not trimmed?)" % elapsed
    finally:
        MockHdcServer._frame = orig
        del MockHdcServer._frame_orig
        _ = server


def test_probe_cache_skips_redundant_probe(mock_port, monkeypatch):
    """Liveness cache: the second command must not probe again."""
    calls = []

    import hdcutils._server as server_mod

    orig_probe = server_mod.probe_server

    def counting_probe(*args, **kwargs):
        calls.append(1)
        return True

    monkeypatch.setattr(server_mod, "probe_server", counting_probe)
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    monkeypatch.setattr(hdcutils.core, "probe_server", counting_probe)
    client.list_targets()
    n_first = len(calls)
    client.list_targets()
    client.server_version()
    assert len(calls) == n_first  # cache hit: no further probes


def test_streams_are_opt_in_long_connections(client):
    """Streaming APIs are explicit long connections; ordinary APIs hold no
    lingering connection objects."""
    d = client.device("MOCKSERIAL1")
    d.shell("echo quick")  # ends on EOF, nothing lingers
    lines = list(d.hilog(timeout=5))
    assert lines == ["hilog line 0", "hilog line 1", "hilog line 2"]
    assert d.client._server_ok_until > 0


def _running_server(mock_port):
    import gc
    from _mock import MockHdcServer

    for obj in gc.get_objects():
        if isinstance(obj, MockHdcServer) and obj.port == mock_port:
            return obj
    raise RuntimeError("mock server not found")
