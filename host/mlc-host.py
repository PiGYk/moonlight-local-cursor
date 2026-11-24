#!/usr/bin/env python3
"""moonlight-local-cursor host: send the game cursor to Moonlight clients instead of drawing it into the video.

Runs next to Sunshine on a Linux host (Wayland with XWayland, or plain X11). It reads the current X cursor through
XFixes (games under Wine/Proton and Steam set a real X cursor) and sends each connected client every new shape once,
then only its id (0 = hidden).

While at least one client is connected, the host cursor is hidden over X windows (XFixesHideCursor on the root
window). XWayland then gives the compositor an empty cursor, so the cursor never reaches Sunshine's capture, whether
the compositor draws it on a hardware plane or straight into the frame. XFixes keeps reporting the real shape. When the
last client leaves, or this process dies (the X server drops the hide together with the connection), the cursor
comes back.

Scanout blocker: with the cursor gone, a fullscreen game can be the only thing on the output, and the compositor may
hand the game's buffers straight to the display (direct scanout). Sunshine's KMS capture then reads buffers the game
is already rendering the next frames into, and the stream jumps a frame back and forth whenever the camera moves
(seen with labwc / wlroots 0.20 on NVIDIA). While clients are connected the host keeps a 1x1 override-redirect window
with alpha 1/255 in the corner of the streamed output: invisible, click-through, never focused, and enough to make the
compositor compose the frame itself again.

Wire protocol: see PROTOCOL.md.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import select
import signal
import socket
import struct
import sys
import time
import zlib

from Xlib import X, Xatom, Xutil, display
from Xlib import error as xerror
from Xlib.ext import shape, xfixes

PROTOCOL_VERSION = 1
CLIENT_TIMEOUT = 20          # seconds without anything from a client before it is dropped
MAX_SIDE = 256               # larger "cursors" are pictures, not cursors: treated as hidden
MAX_CLIENT_FRAME = 1 << 16
OUTPUT_CHECK = 5             # seconds between checks of the streamed output size
DEFAULT_ALLOW = ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10",
                 "::1/128", "fc00::/7", "fe80::/10"]


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)


# ---------- pure helpers (covered by tests) ----------
def frame(kind, payload=b""):
    return kind + struct.pack(">I", len(payload)) + payload


def shape_frame(shape_id, width, height, xhot, yhot, pixels):
    return frame(b"F", struct.pack(">IHHHH", shape_id, width, height, xhot, yhot)
                 + zlib.compress(struct.pack(f"<{len(pixels)}I", *pixels)))


def state_frame(shape_id):
    return frame(b"S", struct.pack(">I", shape_id))


def hello_frame(width, height):
    return frame(b"V", json.dumps({"version": PROTOCOL_VERSION, "width": width, "height": height}).encode())


def register_shape(registry, width, height, xhot, yhot, pixels):
    """Id of a cursor shape in `registry` ({fingerprint: id}); a new shape gets the next id.

    Returns (id, is_new). Id 0 means "no visible cursor": fully transparent or too large."""
    if not any(p >> 24 for p in pixels) or width > MAX_SIDE or height > MAX_SIDE:
        return 0, False
    key = hashlib.sha1(struct.pack(">HHHH", width, height, xhot, yhot)
                       + struct.pack(f"<{len(pixels)}I", *pixels)).digest()
    if key in registry:
        return registry[key], False
    registry[key] = len(registry) + 1
    return registry[key], True


def parse_networks(items):
    """--allow values → networks; "any" (or nothing) allows everyone."""
    if not items or "any" in items:
        return []
    return [ipaddress.ip_network(item, strict=False) for item in items]


def is_allowed(address, networks):
    if not networks:
        return True
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in networks if net.version == ip.version)


def pick_monitor(monitors, wanted):
    """monitors: [(name, primary, x, y, width, height)]. The named one, else the primary, else the first."""
    for m in monitors:
        if wanted and m[0] == wanted:
            return m
    return next((m for m in monitors if m[1]), monitors[0] if monitors else None)


# ---------- X side ----------
class XSession:
    """Connection to the X server: cursor shapes, hiding, the streamed output and the scanout blocker."""

    def __init__(self, display_name, output_name, use_blocker):
        self.display_name = display_name
        self.output_name = output_name
        self.use_blocker = use_blocker
        self.d = None
        self.shapes = {}             # fingerprint → id
        self.frames = {}             # id → ready F frame
        self.current = 0
        self.hidden = False
        self.blocker = None          # (window, colormap)
        self.output = (0, 0, 1920, 1080)
        self.warned_output = False

    def connect(self):
        self.d = display.Display(self.display_name)
        self.d.xfixes_query_version()
        self.d.xfixes_select_cursor_input(self.d.screen().root, xfixes.XFixesDisplayCursorNotifyMask)
        # python-xlib clones extension event classes, so isinstance() never matches: compare (type, sub_code).
        self.cursor_event = self.d.extension_event.DisplayCursorNotify
        self.hidden = False
        self.blocker = None
        self.read_output()
        self.refresh()
        log(f"X display {self.display_name}: connected, streamed output {self.output[2]}x{self.output[3]}"
            f"+{self.output[0]}+{self.output[1]}")

    def close(self):
        try:
            self.d.close()
        except Exception:
            pass
        self.d = None
        self.hidden = False
        self.blocker = None

    def fileno(self):
        return self.d.fileno()

    def read_output(self):
        """Geometry of the streamed output in X coordinates. True if it changed."""
        try:
            raw = self.d.screen().root.xrandr_get_monitors(is_active=True).monitors
            monitors = [(self.d.get_atom_name(m.name), bool(m.primary), m.x, m.y,
                         m.width_in_pixels, m.height_in_pixels) for m in raw]
        except Exception:
            monitors = []
        if self.output_name and not any(m[0] == self.output_name for m in monitors) and not self.warned_output:
            log(f"output {self.output_name!r} not found among {[m[0] for m in monitors]}; using the primary one")
            self.warned_output = True
        m = pick_monitor(monitors, self.output_name)
        if m:
            output = m[2:]
        else:
            s = self.d.screen()
            output = (0, 0, s.width_in_pixels, s.height_in_pixels)
        changed = output != self.output
        self.output = output
        return changed

    def set_hidden(self, hide):
        """Hide or restore the cursor over X windows. XFixes counts hides per client, so we hold at most one."""
        if not self.d or hide == self.hidden:
            return
        root = self.d.screen().root
        root.xfixes_hide_cursor() if hide else root.xfixes_show_cursor()
        self.d.sync()
        self.hidden = hide
        log("host cursor:", "hidden, clients draw it" if hide else "restored")
        if self.use_blocker:
            try:
                self.set_blocker(hide)
                self.d.sync()
            except xerror.XError as e:
                log(f"scanout blocker failed ({e}); going on without it")
                self.use_blocker, self.blocker = False, None

    def set_blocker(self, on):
        if on and self.blocker is None:
            s = self.d.screen()
            visual = next((v for depth in s.allowed_depths if depth.depth == 32 for v in depth.visuals
                           if v.visual_class == X.TrueColor), None)
            if visual is None:
                log("no 32-bit visual, scanout blocker disabled")
                self.use_blocker = False
                return
            colormap = s.root.create_colormap(visual.visual_id, X.AllocNone)
            window = s.root.create_window(self.output[0], self.output[1], 1, 1, 0, 32, X.InputOutput,
                                          visual.visual_id, background_pixel=0x01000000, border_pixel=0,
                                          colormap=colormap, override_redirect=True)
            window.shape_rectangles(shape.SO.Set, shape.SK.Input, X.Unsorted, 0, 0, [])  # clicks pass through
            window.set_wm_name("moonlight-local-cursor scanout blocker")
            # wlroots compositors (labwc, sway) hand keyboard focus to a newly mapped override-redirect window unless
            # its type says it never wants it; on labwc 0.20 the 1.0 blocker took focus from the game and mouse clicks
            # stopped reaching it. A notification is never focused, and the input hint says the same to the rest.
            window.change_property(self.d.intern_atom("_NET_WM_WINDOW_TYPE"), Xatom.ATOM, 32,
                                   [self.d.intern_atom("_NET_WM_WINDOW_TYPE_NOTIFICATION")])
            window.set_wm_hints(flags=Xutil.InputHint, input=0)
            window.map()
            self.blocker = (window, colormap)
        elif not on and self.blocker:
            window, colormap = self.blocker
            window.destroy()
            colormap.free()
            self.blocker = None

    def keep_blocker_on_top(self):
        if self.blocker:
            self.blocker[0].configure(x=self.output[0], y=self.output[1], stack_mode=X.Above)
            self.d.flush()

    def refresh(self):
        """Re-read the cursor. True if the current shape id changed."""
        r = self.d.xfixes_get_cursor_image(self.d.screen().root)
        pixels = list(r.cursor_image)
        shape_id, new = register_shape(self.shapes, r.width, r.height, r.xhot, r.yhot, pixels)
        if new:
            self.frames[shape_id] = shape_frame(shape_id, r.width, r.height, r.xhot, r.yhot, pixels)
        changed = shape_id != self.current
        self.current = shape_id
        return changed

    def events(self):
        """Drain X events. True if the cursor changed."""
        changed = False
        while self.d.pending_events():
            e = self.d.next_event()
            if (e.type, getattr(e, "sub_code", None)) == self.cursor_event:
                changed = True
        return changed and self.refresh()


# ---------- network side ----------
class Client:
    def __init__(self, sock, address):
        self.sock, self.address = sock, address
        self.buffer = b""
        self.last_seen = time.monotonic()
        self.sent_shapes = set()

    def send(self, data):
        self.sock.sendall(data)

    def send_state(self, xs):
        shape_id = xs.current
        if shape_id and shape_id not in self.sent_shapes:
            self.send(xs.frames[shape_id])
            self.sent_shapes.add(shape_id)
        self.send(state_frame(shape_id))


class Host:
    def __init__(self, args):
        self.args = args
        self.allow = parse_networks(args.allow)
        self.xs = XSession(args.display, args.output, not args.no_scanout_blocker)
        self.listener = None
        self.clients = {}
        self.retry_x = 0.0
        self.retry_listen = 0.0
        self.next_output_check = 0.0

    def name(self, client):
        return f"{client.address[0]}:{client.address[1]}"

    def listen(self):
        try:
            family = socket.AF_INET6 if ":" in self.args.listen else socket.AF_INET
            s = socket.socket(family, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            s.bind((self.args.listen, self.args.port))
            s.listen(4)
            self.listener = s
            log(f"listening on {self.args.listen} port {self.args.port}")
        except OSError as e:
            log(f"cannot listen on {self.args.listen} port {self.args.port}: {e}; retrying in 5 s")
            self.retry_listen = time.monotonic() + 5

    def accept(self):
        sock, address = self.listener.accept()
        if not is_allowed(address[0], self.allow):
            log(f"refused {address[0]}: not in --allow")
            sock.close()
            return
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(5)
        client = Client(sock, address)
        try:
            client.send(hello_frame(*self.xs.output[2:]))
            if self.xs.d:
                client.send_state(self.xs)
        except OSError:
            sock.close()
            return
        self.clients[sock] = client
        log(f"client {self.name(client)} connected; clients: {len(self.clients)}")
        self.xs.set_hidden(not self.args.keep_host_cursor)

    def drop(self, sock, reason):
        client = self.clients.pop(sock, None)
        sock.close()
        if client:
            log(f"client {self.name(client)} left ({reason}); clients: {len(self.clients)}")
        if not self.clients:
            self.xs.set_hidden(False)

    def read(self, sock):
        client = self.clients[sock]
        try:
            data = sock.recv(4096)
        except OSError as e:
            return self.drop(sock, str(e))
        if not data:
            return self.drop(sock, "closed")
        client.last_seen = time.monotonic()
        client.buffer += data
        while len(client.buffer) >= 5:
            length = struct.unpack(">I", client.buffer[1:5])[0]
            if length > MAX_CLIENT_FRAME:
                return self.drop(sock, "oversized frame")
            if len(client.buffer) < 5 + length:
                break
            kind, client.buffer = client.buffer[:1], client.buffer[5 + length:]
            if kind == b"P":
                try:
                    client.send(frame(b"P"))
                except OSError as e:
                    return self.drop(sock, str(e))

    def broadcast(self, data=None):
        for sock, client in list(self.clients.items()):
            try:
                client.send(data) if data else client.send_state(self.xs)
            except OSError as e:
                self.drop(sock, str(e))

    def lose_x(self, reason):
        log(f"X display {self.args.display}: {reason}")
        self.xs.close()
        self.retry_x = time.monotonic() + 2

    def step(self):
        now = time.monotonic()
        if self.listener is None and now >= self.retry_listen:
            self.listen()
        if self.xs.d is None and now >= self.retry_x:
            try:
                self.xs.connect()
                self.xs.set_hidden(bool(self.clients) and not self.args.keep_host_cursor)
                self.broadcast(hello_frame(*self.xs.output[2:]))
                self.broadcast()
            except Exception as e:  # XWayland not up yet, or gone: keep trying
                self.lose_x(f"unavailable: {e}")
        if self.xs.d and self.clients and now >= self.next_output_check:
            self.next_output_check = now + OUTPUT_CHECK
            try:
                if self.xs.read_output():
                    log(f"streamed output is now {self.xs.output[2]}x{self.xs.output[3]}")
                    self.broadcast(hello_frame(*self.xs.output[2:]))
                self.xs.keep_blocker_on_top()
            except Exception as e:
                self.lose_x(f"lost: {e}")
        for sock, client in list(self.clients.items()):
            if now - client.last_seen > CLIENT_TIMEOUT:
                self.drop(sock, f"silent for {CLIENT_TIMEOUT} s")

        waiting = [s for s in (self.listener, self.xs.d and self.xs) if s] + list(self.clients)
        try:
            queued = bool(self.xs.d and self.xs.d.pending_events())
        except Exception:
            queued = True                    # xs.events() below sorts out the broken connection
        if queued:
            ready = [self.xs]
        elif waiting:
            ready = select.select(waiting, [], [], 1.0)[0]
        else:
            time.sleep(1)
            ready = []
        for r in ready:
            if r is self.listener:
                self.accept()
            elif r is self.xs:
                try:
                    if self.xs.events():
                        self.broadcast()
                except Exception as e:
                    self.lose_x(f"lost: {e}")
            elif r in self.clients:
                self.read(r)

    def stop(self, *_):
        for sock in list(self.clients):
            sock.close()
        self.clients.clear()
        try:
            self.xs.set_hidden(False)
        except Exception:
            pass                             # the X server restores the cursor when our connection closes anyway
        sys.exit(0)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--listen", default="0.0.0.0", help="address to listen on (default 0.0.0.0; '::' for IPv4+IPv6)")
    p.add_argument("--port", type=int, default=48100)
    p.add_argument("--allow", nargs="*", default=DEFAULT_ALLOW, metavar="CIDR",
                   help="client networks allowed to connect (default: private and loopback ranges; 'any' for all)")
    p.add_argument("--display", default=os.environ.get("DISPLAY") or ":0", help="X display (default $DISPLAY or :0)")
    p.add_argument("--output", default="", help="RandR name of the output Sunshine streams, e.g. DP-1 "
                                                "(default: the primary output)")
    p.add_argument("--no-scanout-blocker", action="store_true", help="do not keep the 1x1 window that stops direct "
                                                                     "scanout of fullscreen games")
    p.add_argument("--keep-host-cursor", action="store_true", help="do not hide the cursor on the host (debugging)")
    host = Host(p.parse_args())
    signal.signal(signal.SIGTERM, host.stop)
    signal.signal(signal.SIGINT, host.stop)
    while True:
        host.step()


if __name__ == "__main__":
    main()
