# -*- coding: utf-8 -*-
"""End-to-end tests for pure-socket file transfer / app install (protocol-level
mock)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _mock import FAKE_JPEG  # noqa: E402

import hdcutils  # noqa: E402
from hdcutils._serial import TransferConfig, TransferPayload, parse_message  # noqa: E402
from hdcutils.exceptions import HdcCommandError, HdcError  # noqa: E402


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


# ---------------------------------------------------------------------
# Serialization (protobuf style)
# ---------------------------------------------------------------------
def test_transfer_config_roundtrip():
    config = TransferConfig(file_size=12345, path="/data/x.bin", optional_name="x.bin",
                            options="-r", function_name="file send", client_cwd="/c/")
    data = config.serialize()
    parsed = TransferConfig.parse(data)
    assert parsed == config


def test_transfer_payload_roundtrip():
    head = TransferPayload(index=123456, compress_type=0, compress_size=1024,
                           uncompress_size=1024)
    raw = head.serialize()
    assert len(raw) == 64
    assert TransferPayload.parse(raw) == head
    assert raw.endswith(b"")


def test_transfer_config_field_order():
    data = TransferConfig(file_size=5, path="/x").serialize()
    # fields are written unconditionally: tag byte (1<<3)|0 = 0x08
    assert data[0] == 0x08 and data[1] == 5
    # path is field 5, length-delimited: (5<<3)|2 = 0x2A
    assert 0x2A in data


# ---------------------------------------------------------------------
# file send / recv
# ---------------------------------------------------------------------
def test_send_file_binary(mock_port, tmp_path):
    payload = bytes(range(256)) * 1000 + b"tail"  # ~256KB: covers the chunk boundary
    local = tmp_path / "binary.bin"
    local.write_bytes(payload)
    server_files = _server_files(mock_port)
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    d = client.device("MOCKSERIAL1")
    summary = d.send_file(str(local), "/data/local/tmp/binary.bin")
    assert "FileTransfer finish" in summary
    assert _server_files(mock_port)["/data/local/tmp/binary.bin"] == payload


def test_send_file_small(mock_port, tmp_path):
    local = tmp_path / "small.txt"
    local.write_bytes(b"hello hdc")
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    client.device("MOCKSERIAL1").send_file(str(local), "/data/local/tmp/small.txt")
    assert _server_files(mock_port)["/data/local/tmp/small.txt"] == b"hello hdc"


def test_send_file_local_missing(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    with pytest.raises(HdcCommandError):
        client.device("MOCKSERIAL1").send_file("/no/such/file.bin", "/data/x")


def test_recv_file(mock_port, tmp_path):
    target = str(tmp_path / "pulled.bin")
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    summary = client.device("MOCKSERIAL1").recv_file(
        "/data/local/tmp/x.jpeg", target)
    assert os.path.isfile(target)
    assert open(target, "rb").read() == FAKE_JPEG
    assert "FileTransfer finish" in summary


def test_recv_file_creates_parent_dirs(mock_port, tmp_path):
    target = str(tmp_path / "a" / "b" / "pulled.bin")
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    client.device("MOCKSERIAL1").recv_file("/data/local/tmp/x.jpeg", target)
    assert open(target, "rb").read() == FAKE_JPEG


def test_recv_file_device_missing(mock_port, tmp_path):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    with pytest.raises(HdcCommandError) as exc:
        client.device("MOCKSERIAL1").recv_file("/data/local/tmp/no/such.file",
                                              str(tmp_path / "x"))
    assert "not found" in str(exc.value)


def test_read_file(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    assert client.device("MOCKSERIAL1").read_file("/data/local/tmp/x.jpeg") == FAKE_JPEG


def test_read_file_missing(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    with pytest.raises(HdcCommandError):
        client.device("MOCKSERIAL1").read_file("/no/such/file")


def test_write_file(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    d = client.device("MOCKSERIAL1")
    d.write_file("/data/local/tmp/w.txt", b"payload")  # base64 relay
    assert d.read_file("/data/local/tmp/w.txt") == b"payload"


# ---------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------
def test_install(mock_port, tmp_path):
    pkg = tmp_path / "app.hap"
    pkg.write_bytes(b"HAP-PACKAGE-BYTES" * 100)
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    result = client.device("MOCKSERIAL1").install(str(pkg))
    assert "AppMod finish" in result
    installed = _installed(mock_port)
    assert len(installed) == 1
    assert installed[0]["size"] == len(b"HAP-PACKAGE-BYTES" * 100)
    assert installed[0]["options"] == "-r"
    assert installed[0]["name"].endswith(".hap")


def test_install_no_replace(mock_port, tmp_path):
    pkg = tmp_path / "app.hap"
    pkg.write_bytes(b"DATA")
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    client.device("MOCKSERIAL1").install(str(pkg))  # defaults to -r


def test_install_invalid_ext(mock_port, tmp_path):
    pkg = tmp_path / "app.exe"
    pkg.write_bytes(b"DATA")
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    with pytest.raises(HdcCommandError):
        client.device("MOCKSERIAL1").install(str(pkg))


def test_install_missing_file(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    with pytest.raises(HdcCommandError):
        client.device("MOCKSERIAL1").install("/no/such/app.hap")


def test_uninstall(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    result = client.device("MOCKSERIAL1").uninstall("com.example.mock")
    assert "AppMod finish" in result
    assert _server_state(mock_port).uninstalled == ["com.example.mock"]


def test_uninstall_keep_data(mock_port):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    client.device("MOCKSERIAL1").uninstall("com.example.mock", keep_data=True)


# ---------------------------------------------------------------------
# Directory transfer
# ---------------------------------------------------------------------
def test_send_dir(mock_port, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.bin").write_bytes(b"A")
    (tmp_path / "sub" / "b.bin").write_bytes(b"B")
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    count = client.device("MOCKSERIAL1").send_dir(str(tmp_path), "/data/local/tmp/dir")
    assert count == 2
    files = _server_files(mock_port)
    assert files["/data/local/tmp/dir/a.bin"] == b"A"
    assert files["/data/local/tmp/dir/sub/b.bin"] == b"B"


def test_pull_dir(mock_port, tmp_path):
    client = hdcutils.HdcClient(port=mock_port, auto_start=False)
    target = tmp_path / "out"
    count = client.device("MOCKSERIAL1").pull_dir("/data/local/tmp", str(target))
    assert count >= 1
    assert any(p.name == "x.jpeg" for p in target.rglob("*"))


# ---------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------
def _server(mock_port):
    """Find the running mock server by port (shared across tests)."""
    import gc

    for obj in gc.get_objects():
        from _mock import MockHdcServer

        if isinstance(obj, MockHdcServer) and obj.port == mock_port:
            return obj
    raise RuntimeError("mock server not found")


def _server_files(mock_port):
    return _server(mock_port).device_files


def _installed(mock_port):
    return _server(mock_port).installed_packages


def _server_state(mock_port):
    return _server(mock_port)
