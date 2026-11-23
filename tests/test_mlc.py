"""Tests for the pure parts: protocol, shape registry, allow-list, Moonlight.conf, cursor scaling. No X, no network."""
import importlib.util
import pathlib
import struct
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


host = load("mlc_host", "host/mlc-host.py")
client = load("mlc_client", "client/mlc-client.py")


def test_shape_roundtrip():
    pixels = [0xFF102030, 0x00000000, 0x80404040, 0xFFFFFFFF]
    data = host.shape_frame(7, 2, 2, 1, 0, pixels) + host.state_frame(7)
    frames, rest = client.split_frames(data)
    assert rest == b"" and [k for k, _ in frames] == [b"F", b"S"]
    payload = frames[0][1]
    assert struct.unpack_from(">IHHHH", payload) == (7, 2, 2, 1, 0)
    assert list(struct.unpack("<4I", zlib.decompress(payload[12:]))) == pixels
    assert struct.unpack(">I", frames[1][1])[0] == 7


def test_frames_split_anywhere():
    stream = host.hello_frame(3840, 2160) + host.state_frame(3)
    buffer, got = b"", []
    for i in range(len(stream)):                       # TCP may cut anywhere
        frames, buffer = client.split_frames(buffer + stream[i:i + 1])
        got += frames
    assert [k for k, _ in got] == [b"V", b"S"] and buffer == b""
    assert b'"width": 3840' in got[0][1]


def test_shape_registry():
    registry = {}
    arrow = [0xFF000000] * 4
    assert host.register_shape(registry, 2, 2, 0, 0, arrow) == (1, True)
    assert host.register_shape(registry, 2, 2, 0, 0, arrow) == (1, False)       # known shape: not sent again
    assert host.register_shape(registry, 2, 2, 1, 1, arrow) == (2, True)       # other hotspot, other shape
    assert host.register_shape(registry, 2, 2, 0, 0, [0x00FFFFFF] * 4) == (0, False)   # fully transparent
    assert host.register_shape(registry, 300, 1, 0, 0, [0xFF000000] * 300) == (0, False)  # too large


def test_allow_list():
    nets = host.parse_networks(host.DEFAULT_ALLOW)
    assert host.is_allowed("192.168.1.20", nets)
    assert host.is_allowed("100.101.102.103", nets)                             # Tailscale
    assert host.is_allowed("::ffff:10.0.0.5", nets)                             # IPv4 on a dual-stack socket
    assert host.is_allowed("fe80::1%wlan0", nets)
    assert not host.is_allowed("8.8.8.8", nets)
    assert host.is_allowed("8.8.8.8", host.parse_networks(["any"]))
    only = host.parse_networks(["10.0.0.7"])
    assert host.is_allowed("10.0.0.7", only) and not host.is_allowed("10.0.0.8", only)


def test_pick_monitor():
    monitors = [("HDMI-A-1", False, 3840, 0, 1920, 1080), ("DP-1", True, 0, 0, 3840, 2160)]
    assert host.pick_monitor(monitors, "HDMI-A-1")[0] == "HDMI-A-1"
    assert host.pick_monitor(monitors, "")[0] == "DP-1"                         # primary
    assert host.pick_monitor(monitors, "nope")[0] == "DP-1"
    assert host.pick_monitor([], "") is None


def test_title_and_conf():
    assert client.host_name_from_title("gaming-pc - Moonlight") == "gaming-pc"
    assert client.host_name_from_title("my pc - Moonlight") == "my pc"
    assert client.host_name_from_title("Moonlight") is None
    conf = "\n".join([
        "[General]", "mouseacceleration=true", "[hosts]",
        "1\\hostname=gaming-pc", "1\\localaddress=192.168.1.51", "1\\manualaddress=10.0.0.2",
        "1\\remoteaddress=", "2\\hostname=other", "2\\localaddress=192.168.1.9", "size=2",
    ])
    assert client.moonlight_addresses(conf, "gaming-pc") == ["10.0.0.2", "192.168.1.51"]
    assert client.moonlight_addresses(conf, "other") == ["192.168.1.9"]
    assert client.moonlight_addresses(conf, "missing") == []


def test_video_rect():
    assert client.video_rect(1920, 1080, 3840, 2160) == (0, 0, 1920, 1080)
    assert client.video_rect(1920, 1200, 3840, 2160) == (0, 60, 1920, 1080)   # bars top and bottom
    assert client.video_rect(2560, 1080, 3840, 2160) == (320, 0, 1920, 1080)  # bars left and right


def test_cursor_scale():
    assert client.cursor_scale(1920, 3840, 32, 32) == 0.75     # 4K host, 1080p window: 16 px → kept at 24 px
    assert client.cursor_scale(1920, 3840, 64, 64) == 0.5      # big HiDPI cursor: as in the stream
    assert client.cursor_scale(1920, 1920, 32, 32) == 1.0      # 1:1 stream
    assert client.cursor_scale(1920, 3840, 16, 16) == 1.0      # small cursor is not blown up past its size
    assert client.cursor_scale(1920, 3840, 32, 32, forced=1.5) == 1.5


def test_resample():
    pixels, w, h = client.resample([0xFF336699] * 16, 4, 4, 0.5)
    assert (w, h) == (2, 2) and pixels == [0xFF336699] * 4
    # Half transparent, half white (premultiplied) → half-transparent grey, no dark fringe.
    pixels, w, h = client.resample([0xFFFFFFFF, 0x00000000], 2, 1, 0.5)
    assert (w, h) == (1, 1) and pixels == [0x7F7F7F7F]
    assert client.resample([1, 2, 3, 4], 2, 2, 1.0) == ([1, 2, 3, 4], 2, 2)
