# -*- coding: utf-8 -*-
"""Smoke tests for the ``python -m hdcutils`` CLI."""
import subprocess
import sys
import os

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _mock import MockHdcServer  # noqa: E402

import hdcutils  # noqa: E402


@pytest.fixture()
def mock_port(monkeypatch):
    from _mock import MockHdcServer

    server = MockHdcServer(0)
    port = server.start()
    monkeypatch.delenv("HDC_SERVER_PORT", raising=False)
    monkeypatch.delenv("OHOS_HDC_SERVER_PORT", raising=False)
    yield port
    server.stop()


def _run(mock_port, *args):
    env = dict(os.environ)
    env["OHOS_HDC_SERVER_PORT"] = str(mock_port)
    return subprocess.run(
        [sys.executable, "-m", "hdcutils", *args],
        capture_output=True, text=True, timeout=30, env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )


def test_cli_version():
    result = subprocess.run(
        [sys.executable, "-m", "hdcutils", "version"], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert result.stdout.strip() == hdcutils.__version__


def test_cli_list(mock_port):
    result = _run(mock_port, "list")
    assert result.returncode == 0, result.stderr
    assert "MOCKSERIAL1" in result.stdout
    assert "MOCKSERIAL2" in result.stdout


def test_cli_list_verbose(mock_port):
    result = _run(mock_port, "list", "-v")
    assert result.returncode == 0
    assert "USB Connected" in result.stdout


def test_cli_shell(mock_port):
    result = _run(mock_port, "-t", "MOCKSERIAL1", "shell", "param", "get",
                  "const.product.model")
    assert result.returncode == 0
    assert "HUAWEI Mock Phone" in result.stdout


def test_cli_checkserver(mock_port):
    result = _run(mock_port, "checkserver")
    assert result.returncode == 0
    assert "hdc mock" in result.stdout


def test_cli_error_exit_code(mock_port):
    result = _run(mock_port, "fport", "a", "b", "c")
    assert result.returncode != 0
