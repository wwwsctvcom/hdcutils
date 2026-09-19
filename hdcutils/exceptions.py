# -*- coding: utf-8 -*-
"""hdcutils exception hierarchy.

Hierarchy::

    HdcError                                  base class of all hdcutils errors
    ├── HdcServerNotRunning                   hdc server is not running and cannot be pulled up
    ├── HdcServerError                        server-level failure (connect / handshake)
    │   └── HdcProtocolError                  wire protocol mismatch (bad frames / handshake)
    ├── HdcTimeoutError                       command timed out
    └── HdcCommandError                       server/daemon reported failure ([Fail] ...)
        └── HdcDeviceNotFoundError            no available device
"""


class HdcError(Exception):
    """Base class of all hdcutils errors."""


class HdcServerNotRunning(HdcError):
    """hdc server is not running and could not be pulled up."""


class HdcServerError(HdcError):
    """hdc server connection/communication failure."""


class HdcProtocolError(HdcServerError):
    """Wire data does not match the hdc protocol (bad handshake, bad frame size)."""


class HdcTimeoutError(HdcError):
    """Command timed out."""


class HdcCommandError(HdcError):
    """hdc command failed (server/daemon reported a failure).

    ``message`` is the raw failure text with the ``[Fail]`` prefix removed.
    """

    def __init__(self, message: str, output: str = ""):
        self.output = output
        super().__init__(message)


class HdcDeviceNotFoundError(HdcCommandError):
    """No available device (or the given serial does not exist)."""
