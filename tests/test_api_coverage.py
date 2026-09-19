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
        # normalize list-form commands to the command text actually sent
        calls.append(" ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else cmd)
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
    assert not hasattr(client, "list")          # official name is list_targets


def test_client_device_and_wait(client):
    """``hdc wait`` is the official name (no wait_for_device alias)."""
    assert client.device("MOCKSERIAL1").serial == "MOCKSERIAL1"
    assert client.wait(timeout=5).serial in ("MOCKSERIAL1", "MOCKSERIAL2")
    assert client.wait("MOCKSERIAL2", timeout=5).serial == "MOCKSERIAL2"
    assert not hasattr(client, "wait_for_device")
    assert not hasattr(client, "wait_for")


def test_client_wait_for_device_timeout(client):
    with pytest.raises(HdcTimeoutError):
        client.wait("NOPE", timeout=1.0, poll_interval=0.2)


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
    out, rc = device.shell_ex("echo hi")
    assert (out, rc) == ("hi", 0)


def test_stream_shell(device):
    """stream_shell is the only streaming entry (long connection)."""
    chunks = list(device.stream_shell("echo streamed", timeout=10))
    assert b"".join(chunks).strip() == b"streamed"
    frames = list(device.stream_shell("hilog", timeout=10))
    assert len(frames) == 3
    assert not hasattr(device, "stream_lines")    # split lines yourself


def test_stream_shell_frames(device):
    frames = list(device.stream_shell("hilog", timeout=10))
    assert len(frames) == 3


def test_hilog_and_logcat(device):
    assert list(device.hilog(timeout=10))[-1] == "hilog line 2"
    assert list(device.hilog(timeout=10))[0] == "hilog line 0"


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
def test_send_recv_file(tmp_path, device):
    """``hdc file send`` / ``hdc file recv`` and the sync namespace."""
    local = tmp_path / "a.bin"
    local.write_bytes(b"payload-123")
    device.send_file(str(local), "/data/local/tmp/a.bin")
    back = tmp_path / "back.bin"
    device.recv_file("/data/local/tmp/a.bin", str(back))
    assert back.read_bytes() == b"payload-123"

    device.sync.push(str(local), "/data/local/tmp/push.bin")
    device.sync.pull("/data/local/tmp/push.bin", str(tmp_path / "pull.bin"))
    assert (tmp_path / "pull.bin").read_bytes() == b"payload-123"
    assert not hasattr(device, "push")          # use d.sync.push / send_file
    assert not hasattr(device, "pull")


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
    assert device.list_apps() == device.list_apps()


def test_app_info_and_version(device):
    info = device.app_info("com.example.mock")
    assert info["versionName"] == "1.0.0"
    assert device.app_version("com.example.mock") == "1.0.0"


def test_app_start_variants(device, captured):
    device.aa_start("com.example.mock")
    device.aa_start("com.example.mock", ability="EntryAbility")
    device.aa_start("com.example.mock", url="https://example.com")
    assert "aa start -b com.example.mock" in captured[0]
    assert "aa start -b com.example.mock -a EntryAbility" in captured[1]
    assert "aa start -b com.example.mock -U https://example.com" in captured[2]


def test_app_start_failure_raises(device, monkeypatch):
    monkeypatch.setattr(device, "shell", lambda *a, **k: "[Fail]Operation failed")
    with pytest.raises(HdcCommandError):
        device.aa_start("com.example.mock")


def test_aa_start_with_url(device, captured):
    """``aa start -U <url>`` is the official way to open a URL."""
    device.aa_start("com.example.mock", url="https://example.com")
    assert captured == ["aa start -b com.example.mock -U https://example.com"]
    assert not hasattr(device, "open_browser")
    assert not hasattr(device, "open_url")


def test_app_stop_and_clear(device, captured):
    device.aa_force_stop("com.example.mock")
    device.bm_clean("com.example.mock")
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


def test_swipe_drag_fling(device, captured):
    """Official signatures: swipe/drag/fling all take [speed] (default 500)."""
    device.swipe(1, 2, 3, 4)
    device.swipe(1, 2, 3, 4, speed=600)
    device.drag(5, 6, 7, 8)
    device.fling(1, 2, 3, 4)
    device.dirc_fling(2, 600)
    assert captured == [
        "uitest uiInput swipe 1 2 3 4 500",
        "uitest uiInput swipe 1 2 3 4 600",
        "uitest uiInput drag 5 6 7 8 500",
        "uitest uiInput fling 1 2 3 4 500",
        "uitest uiInput dircFling 2 600",
    ]


def test_keyevent_int_name_and_keycode(device, captured):
    device.key_event(KeyCode.BACK)
    device.key_event("Home")
    device.key_event(2)
    assert captured == [
        "uitest uiInput keyEvent 2",
        "uitest uiInput keyEvent Home",
        "uitest uiInput keyEvent 2",
    ]


def test_text_and_input_text(device, captured):
    """Official shapes: ``inputText <x> <y> <text>`` and ``text <content>``."""
    device.input_text(10, 20, "world")
    device.text("hello")
    assert captured == [
        "uitest uiInput inputText 10 20 world",
        "uitest uiInput text hello",
    ]


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
    assert captured[1].startswith("snapshot_display")          # window_size
    assert captured[-1] == "uitest uiInput swipe 50 40 50 10 500"  # 100x50 screen


def test_smode_and_tmode(device):
    """Official names only: ``hdc smode`` / ``hdc tmode port``."""
    assert "root run mode" in device.smode()
    assert "Tmode" in device.tmode_port(10123)
    assert "close success" in device.tmode_port_close()
    assert not hasattr(device, "root")
    assert not hasattr(device, "tcpip")


def test_official_command_name_aliases(device, monkeypatch):
    """Every official-name alias must issue the official hdc command."""
    sent = []
    original = device._execute

    def spy(command, **kwargs):
        sent.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(device, "_execute", spy)
    device.target_boot("recovery")
    device.smode()
    device.tmode_port(10123)
    device.tmode_port_close()
    device.jpid()
    assert sent == [
        "target boot recovery",
        "smode",
        "tmode port 10123",
        "tmode port close",
        "jpid",
    ]


def test_bare_reboot_is_not_used(device):
    """The official server rejects a bare `reboot`; make sure we never send it."""
    sent = []
    original = device._execute
    device._execute = lambda command, **kw: (sent.append(command), original(command, **kw))[1]
    try:
        device.target_boot()
        assert sent == ["target boot"]
        assert "reboot" not in [c for c in sent if c == "reboot"]
    finally:
        device._execute = original


def test_official_framework_style_names(device, tmp_path):
    """Names used by the official test framework (hypium) for the same ops.

    Verified against hypium's driver API: push_file/pull_file/has_file,
    start_app/stop_app/has_app/clear_app_data, current_app,
    wake_up_display/close_display. They must issue the official hdc commands.
    """
    local = tmp_path / "f.bin"
    local.write_bytes(b"x")
    device.push_file(str(local), "/data/local/tmp/f.bin")
    device.pull_file("/data/local/tmp/f.bin", str(tmp_path / "back.bin"))
    assert (tmp_path / "back.bin").read_bytes() == b"x"
    assert device.has_file("/data/local/tmp/f.bin") is True

    assert device.has_app("com.example.mock") is True
    assert device.has_app("no.such.app") is False
    device.start_app("com.example.mock")
    device.stop_app("com.example.mock")
    device.clear_app_data("com.example.mock")
    assert device.current_app() == ("com.example.mock", "EntryAbility")

    device.wake_up_display()
    device.close_display()
    assert not hasattr(device, "prop")   # no adb-style leftovers


def test_jpid_and_track_jpid(device):
    assert "com.example.mock" in device.jpid()
    lines = list(device.track_jpid(timeout=5))
    assert len(lines) == 2 and "com.example.mock" in lines[0]


def test_battery(device):
    info = device.battery()
    assert info["capacity"] == "85"
    assert info["charge_state"] == "1"


def test_reboot_modes(device, monkeypatch):
    """Official command name is ``target boot`` (a bare ``reboot`` is rejected
    by the real server -- verified against the official hdc binary)."""
    commands = []
    original = device._execute

    def spy(command, **kwargs):
        commands.append(command)
        return original(command, **kwargs)

    monkeypatch.setattr(device, "_execute", spy)
    device.target_boot()
    device.target_boot("recovery")
    device.target_boot("bootloader")
    assert commands == ["target boot", "target boot recovery", "target boot bootloader"]


def test_device_info_and_param(device):
    """``param get`` in parsed and raw forms; ``param ls/set/wait/save``."""
    info = device.device_info()
    assert isinstance(info, DeviceInfo)
    assert info.model == "HUAWEI Mock Phone"
    props = device.get_props()
    assert props["const.ohos.apiversion"] == "12"
    assert device.get_prop("const.product.model") == "HUAWEI Mock Phone"
    assert "const.product.model" in device.param_get("const.product.model")
    assert isinstance(device.param_ls("const.product"), list)
    device.param_set("persist.test", "1")
    assert device.param_wait("persist.test", "1", timeout=2) is True
    device.param_save()
    assert not hasattr(device, "prop")


def test_wait_specific_device(device):
    assert device.wait(timeout=5).serial in ("MOCKSERIAL1", "MOCKSERIAL2")


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
    device.fport("tcp:17170", "tcp:8012")
    assert "tcp:17170 tcp:8012" in device.fport_list()
    assert device.fport_list() == device.fport_list()
    device.fport_remove("tcp:17170 tcp:8012")
    device.fport_remove_all()


def test_reverse_alias(device):
    device.rport("tcp:8012", "tcp:17171")


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
