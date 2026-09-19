# -*- coding: utf-8 -*-
"""Per-API coverage tests.

Every public method of ``HdcClient`` / ``HdcDevice`` / ``HdcSync`` / ``Prop``
is exercised at least once against the mock hdc server. Shell-command based
APIs (input / power / hidumper family) are verified by capturing the exact
command line sent, which also pins the uitest uiInput command shapes.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _mock import FAKE_JPEG  # noqa: E402

import hdcutils  # noqa: E402
from hdcutils import AppCurrentInfo, DeviceInfo, KeyCode, TargetInfo, WindowSize  # noqa: E402
from hdcutils.exceptions import (  # noqa: E402
    HdcCommandError,
    HdcDeviceNotFoundError,
    HdcError,
    HdcTimeoutError,
)


@pytest.fixture(params=[False, True], ids=["handshake44", "handshake108"])
def mock_port(request):
    from _mock import MockHdcServer

    server = MockHdcServer(port=0, use_version_handshake=request.param)
    port = server.start()
    yield port
    server.stop()


@pytest.fixture()
def client(mock_port):
    return hdcutils.HdcClient(port=mock_port, auto_start=False)


@pytest.fixture()
def device(client):
    return client.device("MOCKSERIAL1")


@pytest.fixture()
def captured(device, monkeypatch):
    """Capture every shell command the device sends."""
    calls = []
    original = device.shell_bytes

    def spy(cmd, timeout=None):
        calls.append(cmd)
        return original(cmd, timeout=timeout)

    monkeypatch.setattr(device, "shell_bytes", spy)
    return calls


# =====================================================================
# HdcClient
# =====================================================================
def test_client_repr_and_context_manager(mock_port):
    with hdcutils.HdcClient(port=mock_port, auto_start=False) as hdc:
        assert repr(hdc) == "HdcClient(host='127.0.0.1', port=%d)" % mock_port


def test_client_hdc_path_property(client):
    # Resolution is blocked in tests; the property must still be callable.
    assert client.hdc_path is None or os.path.isfile(client.hdc_path)


def test_client_list_and_device_list(client):
    assert client.list_targets() == ["MOCKSERIAL1", "MOCKSERIAL2"]
    devices = client.device_list()
    assert [d.serial for d in devices] == ["MOCKSERIAL1", "MOCKSERIAL2"]
    assert [d.serial for d in client.list()] == ["MOCKSERIAL1", "MOCKSERIAL2"]


def test_client_device_and_wait_for_aliases(client):
    assert client.device("MOCKSERIAL1").serial == "MOCKSERIAL1"
    assert client.wait_for(timeout=5).serial in ("MOCKSERIAL1", "MOCKSERIAL2")
    assert client.wait_for("MOCKSERIAL2", timeout=5).serial == "MOCKSERIAL2"


def test_client_wait_for_device_timeout(client):
    with pytest.raises(HdcTimeoutError):
        client.wait_for_device("NOPE", timeout=1.0, poll_interval=0.2)


def test_client_stream_command(client, device):
    chunks = list(client.stream_command("shell echo hi", serial="MOCKSERIAL1", timeout=10))
    assert b"".join(chunks).strip() == b"hi"


def test_client_server_version(client):
    assert client.server_version().startswith("hdc mock")


def test_client_start_kill_restart_server(client, monkeypatch):
    calls = []
    monkeypatch.setattr(hdcutils.core, "find_hdc_binary",
                        lambda explicit=None: "D:/fake/hdc.exe")
    monkeypatch.setattr(hdcutils.core, "_start_server",
                        lambda binary, port, timeout=15.0: calls.append(("start", binary)))
    monkeypatch.setattr(hdcutils.core, "_kill_server_pid",
                        lambda port, timeout=10.0: calls.append(("kill", port)))
    client._hdc_binary = "D:/fake/hdc.exe"
    client.start_server()
    client.kill_server()
    client.restart_server()
    assert calls[0] == ("start", "D:/fake/hdc.exe")
    assert calls[1] == ("kill", client.port)


# =====================================================================
# shell family
# =====================================================================
def test_shell_str_and_list(device):
    assert device.shell("echo hi") == "hi"
    assert device.shell(["echo", "hi"]) == "hi"
    assert device.shell("shell echo hi") == "hi"  # double prefix tolerated


def test_shell_bytes_and_shell2(device):
    assert device.shell_bytes("echo raw") == b"raw\r\n"
    out, rc = device.shell2("echo hi")
    assert (out, rc) == ("hi", 0)


def test_shell_stream_and_stream_lines(device):
    chunks = list(device.shell("echo streamed", stream=True, timeout=10))
    assert b"".join(chunks).strip() == b"streamed"
    lines = list(device.stream_lines("hilog", timeout=10))
    assert lines == ["hilog line 0", "hilog line 1", "hilog line 2"]


def test_stream_shell_frames(device):
    frames = list(device.stream_shell("hilog", timeout=10))
    assert len(frames) == 3


def test_hilog_and_logcat(device):
    assert list(device.hilog(timeout=10))[-1] == "hilog line 2"
    assert list(device.logcat(timeout=10))[0] == "hilog line 0"


def test_open_shell_session(device):
    session = device.open_shell()
    try:
        session.send("ps -ef")
        payload = session.recv(timeout=2)
        assert payload and b"ps -ef" in payload
        assert session.eof is False
    finally:
        session.close()


# =====================================================================
# files
# =====================================================================
def test_send_recv_and_aliases(tmp_path, device):
    local = tmp_path / "a.bin"
    local.write_bytes(b"payload-123")
    device.send_file(str(local), "/data/local/tmp/a.bin")
    back = tmp_path / "back.bin"
    device.recv_file("/data/local/tmp/a.bin", str(back))
    assert back.read_bytes() == b"payload-123"
    device.push(str(local), "/data/local/tmp/push.bin")
    device.pull("/data/local/tmp/push.bin", str(tmp_path / "pull.bin"))
    assert (tmp_path / "pull.bin").read_bytes() == b"payload-123"


def test_send_file_options(tmp_path, device):
    local = tmp_path / "opt.bin"
    local.write_bytes(b"x")
    device.send_file(str(local), "/data/local/tmp/opt.bin",
                     hold_timestamp=True, update_if_new=True)


def test_read_write_file(device):
    device.write_file("/data/local/tmp/t.txt", b"hello")
    assert device.read_file("/data/local/tmp/t.txt") == b"hello"  # mock default payload
    assert device.read_file("/data/local/tmp/x.jpeg") == FAKE_JPEG


def test_sync_namespace(tmp_path, device):
    local = tmp_path / "s.bin"
    local.write_bytes(b"sync-data")
    device.sync.push(str(local), "/data/local/tmp/s.bin")
    device.sync.pull("/data/local/tmp/s.bin", str(tmp_path / "s_back.bin"))
    assert (tmp_path / "s_back.bin").read_bytes() == b"sync-data"
    device.sync.write_text("/data/local/tmp/t.txt", "text!")
    assert device.sync.read_bytes("/data/local/tmp/x.jpeg") == FAKE_JPEG
    assert isinstance(device.sync.read_text("/data/local/tmp/x.txt"), str)
    assert list(device.sync.iter_content("/data/local/tmp/x.txt", timeout=10))


# =====================================================================
# apps
# =====================================================================
def test_list_apps_and_list_packages(device):
    assert device.list_apps() == ["com.example.mock", "com.example.other"]
    assert device.list_packages() == device.list_apps()


def test_app_info_and_version(device):
    info = device.app_info("com.example.mock")
    assert info["versionName"] == "1.0.0"
    assert device.app_version("com.example.mock") == "1.0.0"


def test_app_start_variants(device, captured):
    device.app_start("com.example.mock")
    device.app_start("com.example.mock", ability="EntryAbility")
    device.app_start("com.example.mock", url="https://example.com")
    assert "aa start -b com.example.mock" in captured[0]
    assert "aa start -b com.example.mock -a EntryAbility" in captured[1]
    assert "aa start -b com.example.mock -U https://example.com" in captured[2]


def test_app_start_failure_raises(device, monkeypatch):
    monkeypatch.setattr(device, "shell", lambda *a, **k: "[Fail]Operation failed")
    with pytest.raises(HdcCommandError):
        device.app_start("com.example.mock")


def test_open_browser_and_open_url(device, captured):
    device.open_browser("https://example.com")
    device.open_url("https://example.com")
    assert captured == ["aa start -U https://example.com"] * 2


def test_app_stop_and_clear(device, captured):
    device.app_stop("com.example.mock")
    device.app_clear("com.example.mock")
    assert captured[0] == "aa force-stop com.example.mock"
    assert captured[1] == "bm clean -n com.example.mock -d"


def test_app_current(device):
    current = device.app_current()
    assert isinstance(current, AppCurrentInfo)
    assert current.package == "com.example.mock"
    assert current.activity == "EntryAbility"


def test_install_and_uninstall(tmp_path, device):
    pkg = tmp_path / "app.hap"
    pkg.write_bytes(b"HAP" * 50)
    assert "AppMod finish" in device.install(str(pkg))
    assert "AppMod finish" in device.install(str(pkg), "-r", "-g")
    assert "AppMod finish" in device.uninstall("com.example.mock")
    device.uninstall("com.example.mock", keep_data=True)


# =====================================================================
# input family (exact uitest command shapes)
# =====================================================================
def test_click_family(device, captured):
    device.click(10, 20)
    device.double_click(10, 20)
    device.long_click(10, 20)
    assert captured == [
        "uitest uiInput click 10 20",
        "uitest uiInput doubleClick 10 20",
        "uitest uiInput longClick 10 20",
    ]


def test_swipe_and_drag(device, captured):
    device.swipe(1, 2, 3, 4)
    device.swipe(1, 2, 3, 4, speed=600)
    device.drag(5, 6, 7, 8)
    assert captured[0] == "uitest uiInput swipe 1 2 3 4"
    assert captured[1] == "uitest uiInput swipe 1 2 3 4 600"
    assert captured[2] == "uitest uiInput drag 5 6 7 8"


def test_keyevent_int_name_and_keycode(device, captured):
    device.keyevent(KeyCode.BACK)
    device.keyevent("Home")
    device.keyevent(2)
    assert captured == [
        "uitest uiInput keyEvent 2",
        "uitest uiInput keyEvent Home",
        "uitest uiInput keyEvent 2",
    ]


def test_send_keys_and_input_text(device, monkeypatch, captured):
    # without coordinates: taps the screen center first (window_size = 100x50)
    device.send_keys("hello")
    device.input_text("world", 10, 20)
    uitest = [c for c in captured if c.startswith("uitest")]
    assert uitest == [
        "uitest uiInput click 50 25",               # send_keys taps the center first
        "uitest uiInput inputText 50 25 hello",
        "uitest uiInput inputText 10 20 world",      # explicit coordinates: no tap
    ]
    assert any(c.startswith("snapshot_display") for c in captured)  # center from window_size


def test_volume_keys(device, captured):
    device.volume_up()
    device.volume_down()
    assert captured == [
        "uitest uiInput keyEvent 16",
        "uitest uiInput keyEvent 17",
    ]


# =====================================================================
# screen / power / root / tcpip
# =====================================================================
def test_screen_on_off_and_state(device, captured):
    device.screen_on()
    device.screen_off()
    assert device.is_screen_on() is True
    assert captured[0] == "power-shell wakeup"
    assert captured[1] == "power-shell suspend"
    assert captured[2].startswith("hidumper -s PowerManagerService")


def test_unlock_sequence(device, captured):
    device.unlock()
    assert captured[0] == "power-shell wakeup"
    assert captured[1].startswith("snapshot_display")   # window_size -> screenshot
    assert captured[-1] == "uitest uiInput swipe 50 40 50 10"  # 100x50 screen


def test_root_and_tcpip(device):
    assert "root run mode" in device.root()
    assert "Tmode" in device.tcpip(10123)


def test_battery(device):
    info = device.battery()
    assert info["capacity"] == "85"
    assert info["charge_state"] == "1"


def test_reboot_modes(device, monkeypatch):
    commands = []
    original = device._execute

    def spy(command, **kwargs):
        commands.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(device, "_execute", spy)
    device.reboot()
    device.reboot("recovery")
    assert commands == ["reboot", "reboot recovery"]


def test_device_info_and_props(device):
    info = device.device_info()
    assert isinstance(info, DeviceInfo)
    assert info.model == "HUAWEI Mock Phone"
    props = device.get_props()
    assert props["const.ohos.apiversion"] == "12"
    assert device.prop.get("const.product.model") == "HUAWEI Mock Phone"
    assert device.prop["const.product.model"] == "HUAWEI Mock Phone"
    assert device.prop("const.product.model") == "HUAWEI Mock Phone"


def test_wait_for_device_specific(device):
    assert device.wait_for_device(timeout=5).serial in ("MOCKSERIAL1", "MOCKSERIAL2")


# =====================================================================
# screenshot
# =====================================================================
def test_screenshot_data_and_file(tmp_path, device):
    data = device.screenshot_data()
    assert data == FAKE_JPEG
    target = tmp_path / "shot.jpeg"
    device.screenshot(str(target))
    assert target.read_bytes() == FAKE_JPEG


def test_window_size(device):
    size = device.window_size()
    assert isinstance(size, WindowSize)
    assert (size.width, size.height) == (100, 50)


# =====================================================================
# port forwarding + tunnels
# =====================================================================
def test_forward_aliases(device):
    device.forward("tcp:17170", "tcp:8012")
    assert "tcp:17170 tcp:8012" in device.fport_list()
    assert device.fport_list() == device.forward_list()
    device.forward_remove("tcp:17170 tcp:8012")
    device.fport_remove_all()


def test_reverse_alias(device):
    device.reverse("tcp:8012", "tcp:17171")


def test_create_connection_tunnel(device):
    """create_connection: fport rule + socket; rule removed on close."""
    import socket as _socket

    sock = device.create_connection("tcp", 8012, timeout=5)
    local_port = int(sock._forward_rule.split()[0].split(":")[1])
    try:
        banner = sock.recv(64)
        assert banner.startswith(b"mock-forward tcp:8012")
    finally:
        sock.close()

    # The rule must be gone: the mock listener is torn down. Closing a
    # listening socket releases the port asynchronously on Windows, so retry.
    deadline = time.monotonic() + 5
    while True:
        try:
            probe = _socket.create_connection(("127.0.0.1", local_port), timeout=1)
        except OSError:
            break  # refused: the forward rule is gone
        probe.close()
        if time.monotonic() >= deadline:
            pytest.fail("forward rule still alive on port %d after close" % local_port)
        time.sleep(0.1)


def test_create_connection_rejects_unknown_type(device):
    with pytest.raises(ValueError):
        device.create_connection("bogus", 1)


def test_fport_remove_all(device):
    device.fport("tcp:17172", "tcp:8012")
    device.fport_remove_all()
    assert device.fport_list() == []


# =====================================================================
# exceptions / misc
# =====================================================================
def test_exception_hierarchy():
    assert issubclass(HdcDeviceNotFoundError, HdcCommandError)
    assert issubclass(HdcCommandError, HdcError)
    assert issubclass(HdcTimeoutError, HdcError)


def test_unknown_command_raises(client):
    with pytest.raises(HdcCommandError):
        client._execute("frobnicate")


def test_device_repr_and_serial(device):
    assert repr(device) == "HdcDevice(serial='MOCKSERIAL1')"
    assert device.serial == "MOCKSERIAL1"


def test_target_info_repr(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    target = client.list_targets(verbose=True)[0]
    assert isinstance(target, TargetInfo)
    assert target.is_connected
    assert "MOCKSERIAL1" in str(target) and "MOCKSERIAL1" in repr(target)
