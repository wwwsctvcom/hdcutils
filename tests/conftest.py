# -*- coding: utf-8 -*-
"""Shared fixtures: block real hdc binaries from being discovered."""
import importlib
import os

import pytest


@pytest.fixture(autouse=True)
def block_hdc_search(monkeypatch):
    """The host may have a real hdc installed (e.g. bundled with DevEco);
    keep tests from triggering the real CLI."""

    def fake_find(explicit=None):
        # Hermetic tests: honor only explicit paths, never the host PATH or
        # HDCUTILS_HDC_PATH (a real hdc may be installed on this machine).
        return explicit if (explicit and os.path.isfile(explicit)) else None

    for name in ("hdcutils.core", "hdcutils._server"):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "find_hdc_binary", fake_find)
