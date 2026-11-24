"""The client against a real X server (Xvfb): our cursor goes on Moonlight's stream window itself, as a device cursor
that outranks the window's own, and no window of ours covers the stream. Skipped without Xvfb.

Regression test for 1.0: the cursor sat on an InputOnly child window over the stream. With the pointer in a child,
each wheel step's button press made the X server send Moonlight an Enter event, SDL 3 restarted its smooth-scroll
tracking on every one, and the scroll wheel did nothing on Xorg.
"""
import argparse
import importlib.util
import json
import pathlib
import struct
import zlib

from Xlib import X, display
from Xlib.ext import xfixes  # noqa: F401  (xfixes_get_cursor_image)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shown(d):
    """The cursor the X server shows right now: (width, height, any opaque pixel)."""
    img = d.xfixes_get_cursor_image(d.screen().root)
    return img.width, img.height, any(p >> 24 for p in img.cursor_image)


def test_cursor_on_stream_window_and_nothing_over_it(xvfb, monkeypatch):
    monkeypatch.setenv("DISPLAY", xvfb)
    client = load("mlc_client", "client/mlc-client.py")

    # Moonlight's side: the stream window with the blank cursor SDL gives it
    d = display.Display(xvfb)
    d.xfixes_query_version()
    root = d.screen().root
    win = root.create_window(100, 100, 320, 200, 0, X.CopyFromParent)
    win.set_wm_class("moonlight", "com.moonlight_stream.Moonlight")
    win.set_wm_name("testhost - Moonlight")
    bits = win.create_pixmap(1, 1, 1)
    bits.fill_rectangle(bits.create_gc(foreground=0), 0, 0, 1, 1)
    blank = bits.create_cursor(bits, (0, 0, 0), (0, 0, 0), 0, 0)
    win.change_attributes(cursor=blank)
    win.map()
    root.warp_pointer(260, 200)
    d.sync()
    assert shown(d) == (1, 1, False)

    # the helper, fed what the host sends
    helper = client.Helper(argparse.Namespace(host="", port=1, pid=0, scale=0, moonlight_conf="", verbose=False))
    helper.check_window()
    assert helper.window == win.id
    pixels = [0xFF00C000] * (24 * 24)
    helper.handle(b"V", json.dumps({"version": client.PROTOCOL_VERSION, "width": 1920, "height": 1080}).encode())
    helper.handle(b"F", struct.pack(">IHHHH", 7, 24, 24, 3, 3) + zlib.compress(struct.pack("<576I", *pixels)))
    helper.handle(b"S", struct.pack(">I", 7))
    client.x11.XSync(helper.x.dpy, 0)

    assert win.query_tree().children == []          # 1.0 put a child here and broke the scroll wheel
    assert shown(d) == (24, 24, True)

    win.change_attributes(cursor=blank)              # SDL hides its cursor again: ours still wins
    d.sync()
    assert shown(d) == (24, 24, True)

    helper.disconnect("test")                        # host gone: the window's own cursor is back
    client.x11.XSync(helper.x.dpy, 0)
    assert shown(d) == (1, 1, False)
