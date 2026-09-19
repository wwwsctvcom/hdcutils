# -*- coding: utf-8 -*-
"""hdcutils -- a pure-python HarmonyOS hdc library.

The API is named after the official hdc / OpenHarmony tool commands
(``hdc list targets``, ``hdc shell``, ``hdc file send``, ``hdc install``,
``hdc fport``, ``aa``/``bm``/``param``, ``uitest uiInput``, ``power-shell``).
Names used by the official test framework (hypium) for the same operations
are also provided -- ``push_file``/``pull_file``/``has_file``,
``start_app``/``stop_app``/``has_app``/``clear_app_data``, ``current_app``,
``wake_up_display``/``close_display`` -- so either vocabulary works.

No hdc.exe is involved in any device operation: commands go straight to the
hdc server over a socket (one short connection per command).

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
from ._device import (
    HdcDevice,
    DeviceInfo,
    KeyCode,
    WindowSize,
    AppCurrentInfo,
    SyncSession,
    ForwardedSocket,
)
from ._connection import ShellSession

__version__ = "0.4.0"

__all__ = [
    "__version__",
    "HdcClient",
    "HdcDevice",
    "DeviceInfo",
    "KeyCode",
    "WindowSize",
    "AppCurrentInfo",
    "SyncSession",
    "ForwardedSocket",
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
