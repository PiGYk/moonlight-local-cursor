"""The scanout blocker against a real X server (Xvfb): it must never ask for keyboard focus. Skipped without Xvfb.

Regression test for 1.0: on labwc 0.20 the blocker, an override-redirect window without a type, took keyboard focus
when a client connected, and mouse clicks stopped reaching the game.
"""
import importlib.util
import pathlib
import shutil
import subprocess

import pytest
from Xlib import Xatom, Xutil, display
from Xlib.ext import shape

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def xvfb():
    if not shutil.which("Xvfb"):
        pytest.skip("Xvfb not installed")
    proc = subprocess.Popen(["Xvfb", "-displayfd", "1", "-screen", "0", "640x480x24", "-nolisten", "tcp"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        yield ":" + proc.stdout.readline().decode().strip()
    finally:
        proc.terminate()
        proc.wait()


def test_blocker_never_wants_focus(xvfb):
    host = load("mlc_host", "host/mlc-host.py")
    xs = host.XSession(xvfb, "", True)
    xs.connect()
    xs.set_hidden(True)
    assert xs.blocker, "blocker was not created"

    d = display.Display(xvfb)
    w = d.create_resource_object("window", xs.blocker[0].id)
    assert w.get_attributes().override_redirect
    types = w.get_full_property(d.intern_atom("_NET_WM_WINDOW_TYPE"), Xatom.ATOM)
    assert types and d.intern_atom("_NET_WM_WINDOW_TYPE_NOTIFICATION") in types.value, "blocker has no notification type"
    hints = w.get_wm_hints()
    assert hints.flags & Xutil.InputHint and hints.input == 0
    assert w.shape_get_rectangles(shape.SK.Input).rectangles == []   # clicks pass through

    xs.set_hidden(False)
    assert xs.blocker is None
    xs.close()
