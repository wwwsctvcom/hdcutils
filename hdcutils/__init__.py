# -*- coding: utf-8 -*-
"""hdcutils -- a pure-python HarmonyOS hdc library (modeled after adbutils).

Quick start::

    import hdcutils

    hdc = hdcutils.HdcClient()
    hdc.list_targets()            # ['2JAV4C1579000596', ...]
    d = hdc.device()              # auto-select when a single device is attached
    d.shell("param get const.product.model")
    d.send_file("local.hap", "/data/local/tmp/app.hap")
    d.install("app.hap")
    d.screenshot("screen.jpeg")

See README.md for the protocol notes and reference sources.
"""
from .exceptions import (
    HdcError,
    HdcServerError,
    HdcServerNotRunning,
    HdcProtocolError,
    HdcTimeoutError,
    HdcCommandError,
    HdcDeviceNotFoundError,
)
from .core import HdcClient, TargetInfo
from ._device import HdcDevice, DeviceInfo, KeyCode, WindowSize, AppCurrentInfo, Prop
from ._connection import ShellSession

__version__ = "0.3.0"

__all__ = [
    "__version__",
    "HdcClient",
    "HdcDevice",
    "DeviceInfo",
    "Prop",
    "KeyCode",
    "WindowSize",
    "AppCurrentInfo",
    "TargetInfo",
    "ShellSession",
    "HdcError",
    "HdcServerError",
    "HdcServerNotRunning",
    "HdcProtocolError",
    "HdcTimeoutError",
    "HdcCommandError",
    "HdcDeviceNotFoundError",
]
