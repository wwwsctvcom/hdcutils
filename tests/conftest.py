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
        if explicit and os.path.isfile(explicit):
            return explicit
        env = os.environ.get("HDCUTILS_HDC_PATH")
        if env and os.path.isfile(env):
            return env
        return None

    for name in ("hdcutils.core", "hdcutils._server"):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "find_hdc_binary", fake_find)
