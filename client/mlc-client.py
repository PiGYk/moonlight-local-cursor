#!/usr/bin/env python3
"""moonlight-local-cursor client: show the host's game cursor as a native local cursor in Moonlight (X11).

Finds Moonlight's stream window, puts an invisible InputOnly window over it and gives that window a cursor with the
shape the host sends (shape, hotspot and "hidden" come from mlc-host.py). The local X server draws the cursor, so it
moves with no stream latency, and the host keeps it out of the video while we are connected. The InputOnly window
selects no events: clicks and motion go to Moonlight untouched.

No dependencies beyond libX11 and libXcursor (ctypes). Works under X11 and XWayland; on a Wayland session run
Moonlight under XWayland (QT_QPA_PLATFORM=xcb SDL_VIDEODRIVER=x11). Moonlight must be in absolute mouse mode
("Optimize mouse for remote desktop instead of games"), otherwise the local and the remote cursor drift apart.

The host address comes from Moonlight itself: the stream window is titled "<host name> - Moonlight", and Moonlight.conf
holds the addresses it knows for that host. --host overrides it.
"""
import argparse
import ctypes
import fcntl
import json
import os
import select
import socket
import struct
import sys
import time
import zlib
from ctypes import POINTER, Structure, byref, c_char_p, c_int, c_long, c_uint, c_ulong, c_void_p

PROTOCOL_VERSION = 1
MIN_SIDE = 24               # a cursor smaller than this gets lost on a laptop screen; never shrink below it
WINDOW_GRACE = 3            # seconds without a stream window before the host gets its cursor back
HOST_SILENCE = 20           # seconds without anything from the host before reconnecting
MAX_SIDE = 256
TITLE_SUFFIX = " - Moonlight"
MOONLIGHT_CONFS = [
    "~/.config/Moonlight Game Streaming Project/Moonlight.conf",
    "~/.var/app/com.moonlight_stream.Moonlight/config/Moonlight Game Streaming Project/Moonlight.conf",
]


# ---------- pure helpers (covered by tests) ----------
def video_rect(win_w, win_h, host_w, host_h):
    """Where the picture sits inside Moonlight's window (aspect kept, the rest is black bars): (x, y, w, h)."""
    k = min(win_w / host_w, win_h / host_h)
    w, h = round(host_w * k), round(host_h * k)
    return (win_w - w) // 2, (win_h - h) // 2, w, h


def cursor_scale(video_w, host_w, width, height, forced=None):
    """How much to scale the host cursor for this screen.

    As it looks in the stream (picture width / host output width), but not below MIN_SIDE: a 32 px cursor from a 4K
    host would be 16 px in a 1080p window. Never enlarged beyond its own size for that. --scale wins."""
    if forced:
        return forced
    k = video_w / host_w
    return max(k, min(1.0, MIN_SIDE / max(width, height, 1)))


def resample(pixels, width, height, k):
    """Premultiplied ARGB (list of uint32) → new size; each pixel averages 4x4 samples (fair to alpha)."""
    new_w, new_h = max(1, round(width * k)), max(1, round(height * k))
    if (new_w, new_h) == (width, height):
        return list(pixels), width, height
    out = []
    n = 4
    for y in range(new_h):
        for x in range(new_w):
            a = r = g = b = 0
            for sy in range(n):
                row = min(height - 1, int((y + (sy + 0.5) / n) * height / new_h)) * width
                for sx in range(n):
                    p = pixels[row + min(width - 1, int((x + (sx + 0.5) / n) * width / new_w))]
                    a += p >> 24
                    r += (p >> 16) & 255
                    g += (p >> 8) & 255
                    b += p & 255
            m = n * n
            out.append(((a // m) << 24) | ((r // m) << 16) | ((g // m) << 8) | (b // m))
    return out, new_w, new_h


def split_frames(buffer):
    """Returns ([(kind, payload), ...], rest of the buffer)."""
    frames = []
    while len(buffer) >= 5:
        length = struct.unpack(">I", buffer[1:5])[0]
        if len(buffer) < 5 + length:
            break
        frames.append((buffer[:1], buffer[5:5 + length]))
        buffer = buffer[5 + length:]
    return frames, buffer


def frame(kind, payload=b""):
    return kind + struct.pack(">I", len(payload)) + payload


def host_name_from_title(title):
    """"gaming-pc - Moonlight" → "gaming-pc"; None for other windows."""
    if title.endswith(TITLE_SUFFIX) and len(title) > len(TITLE_SUFFIX):
        return title[:-len(TITLE_SUFFIX)]
    return None


def moonlight_addresses(conf_text, host_name):
    """Addresses Moonlight.conf (QSettings INI) knows for `host_name`, best first."""
    section, hosts = None, {}
    for line in conf_text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "hosts" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        index, _, field = key.partition("\\")
        if field:
            hosts.setdefault(index, {})[field] = value.strip().strip('"')
    found = []
    for host in hosts.values():
        if host.get("hostname") != host_name:
            continue
        for field in ("manualaddress", "localaddress", "remoteaddress", "ipv6address"):
            address = host.get(field)
            if address and address not in found:
                found.append(address)
    return found


# ---------- X11 through ctypes ----------
x11 = ctypes.CDLL("libX11.so.6")
xcursor = ctypes.CDLL("libXcursor.so.1")
Window = Atom = Cursor = c_ulong


class XWindowAttributes(Structure):
    _fields_ = [("x", c_int), ("y", c_int), ("width", c_int), ("height", c_int), ("border_width", c_int),
                ("depth", c_int), ("visual", c_void_p), ("root", Window), ("c_class", c_int),
                ("bit_gravity", c_int), ("win_gravity", c_int), ("backing_store", c_int),
                ("backing_planes", c_ulong), ("backing_pixel", c_ulong), ("save_under", c_int),
                ("colormap", c_ulong), ("map_installed", c_int), ("map_state", c_int),
                ("all_event_masks", c_long), ("your_event_mask", c_long), ("do_not_propagate_mask", c_long),
                ("override_redirect", c_int), ("screen", c_void_p)]


class XcursorImage(Structure):
    _fields_ = [("version", c_uint), ("size", c_uint), ("width", c_uint), ("height", c_uint),
                ("xhot", c_uint), ("yhot", c_uint), ("delay", c_uint), ("pixels", POINTER(c_uint))]


ERROR_HANDLER = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)
for fname, restype, argtypes in [
    ("XOpenDisplay", c_void_p, [c_char_p]),
    ("XDefaultRootWindow", Window, [c_void_p]),
    ("XInternAtom", Atom, [c_void_p, c_char_p, c_int]),
    ("XGetWindowProperty", c_int, [c_void_p, Window, Atom, c_long, c_long, c_int, Atom, POINTER(Atom),
                                   POINTER(c_int), POINTER(c_ulong), POINTER(c_ulong), POINTER(c_void_p)]),
    ("XQueryTree", c_int, [c_void_p, Window, POINTER(Window), POINTER(Window), POINTER(POINTER(Window)),
                           POINTER(c_uint)]),
    ("XFree", c_int, [c_void_p]),
    ("XSelectInput", c_int, [c_void_p, Window, c_long]),
    ("XCreateWindow", Window, [c_void_p, Window, c_int, c_int, c_uint, c_uint, c_uint, c_int, c_uint, c_void_p,
                               c_ulong, c_void_p]),
    ("XMapWindow", c_int, [c_void_p, Window]),
    ("XDestroyWindow", c_int, [c_void_p, Window]),
    ("XResizeWindow", c_int, [c_void_p, Window, c_uint, c_uint]),
    ("XDefineCursor", c_int, [c_void_p, Window, Cursor]),
    ("XFreeCursor", c_int, [c_void_p, Cursor]),
    ("XGetWindowAttributes", c_int, [c_void_p, Window, POINTER(XWindowAttributes)]),
    ("XPending", c_int, [c_void_p]),
    ("XNextEvent", c_int, [c_void_p, c_void_p]),
    ("XConnectionNumber", c_int, [c_void_p]),
    ("XFlush", c_int, [c_void_p]),
    ("XSync", c_int, [c_void_p, c_int]),
    ("XSetErrorHandler", c_void_p, [ERROR_HANDLER]),
]:
    f = getattr(x11, fname)
    f.restype, f.argtypes = restype, argtypes
xcursor.XcursorImageCreate.restype = POINTER(XcursorImage)
xcursor.XcursorImageCreate.argtypes = [c_int, c_int]
xcursor.XcursorImageLoadCursor.restype = Cursor
xcursor.XcursorImageLoadCursor.argtypes = [c_void_p, POINTER(XcursorImage)]
xcursor.XcursorImageDestroy.argtypes = [POINTER(XcursorImage)]

STRUCTURE_NOTIFY_MASK, PROPERTY_CHANGE_MASK = 1 << 17, 1 << 22
DESTROY_NOTIFY, UNMAP_NOTIFY, MAP_NOTIFY, CONFIGURE_NOTIFY, PROPERTY_NOTIFY = 17, 18, 19, 22, 28
INPUT_ONLY, IS_VIEWABLE = 2, 2

X_ERRORS = []


@ERROR_HANDLER
def _on_x_error(_dpy, _event):
    # Windows vanish between requests (Moonlight closed one): Xlib's default handler would exit the process.
    X_ERRORS.append(time.monotonic())
    return 0


class Screen:
    def __init__(self):
        self.dpy = x11.XOpenDisplay(None)
        if not self.dpy:
            sys.exit("moonlight-local-cursor: cannot open the X display (is DISPLAY set?)")
        x11.XSetErrorHandler(_on_x_error)
        self.root = x11.XDefaultRootWindow(self.dpy)
        self.atoms = {}
        x11.XSelectInput(self.dpy, self.root, PROPERTY_CHANGE_MASK)

    def atom(self, name):
        if name not in self.atoms:
            self.atoms[name] = x11.XInternAtom(self.dpy, name.encode(), 0)
        return self.atoms[name]

    def prop(self, window, name, limit=4096):
        """Raw property: (format, count, bytes) or None."""
        kind, fmt, count, rest, data = Atom(), c_int(), c_ulong(), c_ulong(), c_void_p()
        if x11.XGetWindowProperty(self.dpy, window, self.atom(name), 0, limit, 0, 0, byref(kind), byref(fmt),
                                  byref(count), byref(rest), byref(data)) != 0 or not data.value:
            return None
        try:
            size = {8: 1, 16: ctypes.sizeof(ctypes.c_short), 32: ctypes.sizeof(c_long)}.get(fmt.value, 1)
            return fmt.value, count.value, ctypes.string_at(data.value, count.value * size)
        finally:
            x11.XFree(data)

    def numbers(self, window, name):
        p = self.prop(window, name)
        return list(struct.unpack(f"{p[1]}L", p[2])) if p and p[0] == 32 else []

    def text(self, window, name):
        p = self.prop(window, name)
        return p[2] if p and p[0] == 8 else b""

    def attributes(self, window):
        a = XWindowAttributes()
        return a if x11.XGetWindowAttributes(self.dpy, window, byref(a)) else None

    def children(self, window):
        root, parent, kids, n = Window(), Window(), POINTER(Window)(), c_uint()
        if not x11.XQueryTree(self.dpy, window, byref(root), byref(parent), byref(kids), byref(n)):
            return []
        try:
            return [kids[i] for i in range(n.value)]
        finally:
            if kids:
                x11.XFree(kids)

    def client_windows(self):
        listed = self.numbers(self.root, "_NET_CLIENT_LIST")
        if listed:
            return listed
        # No EWMH window manager: top-level windows and their children (frames).
        found = []
        for top in self.children(self.root):
            found.append(top)
            found.extend(self.children(top))
        return found

    def stream_window(self, pid=0):
        """Moonlight's stream window: (window, width, height, host name) or None.

        Moonlight 6.x: the stream window has WM_CLASS "moonlight", "com.moonlight_stream.Moonlight" and the title
        "<host> - Moonlight"; the Qt main window has class "Moonlight", an empty WM_NAME and _XEMBED_INFO, and is
        hidden while streaming."""
        for window in self.client_windows():
            if pid:
                pids = self.numbers(window, "_NET_WM_PID")
                if pids and pids[0] != pid:
                    continue
            wm_class = self.text(window, "WM_CLASS").lower()
            if b"moonlight" not in wm_class:
                continue
            title = (self.text(window, "_NET_WM_NAME") or self.text(window, "WM_NAME")).decode("utf-8", "replace")
            host_name = host_name_from_title(title)
            if host_name is None or self.prop(window, "_XEMBED_INFO") is not None:
                continue
            a = self.attributes(window)
            if a and a.map_state == IS_VIEWABLE:
                return window, a.width, a.height, host_name
        return None

    def make_cursor(self, pixels, width, height, xhot, yhot):
        img = xcursor.XcursorImageCreate(width, height)
        try:
            img.contents.xhot = min(max(0, xhot), width - 1)
            img.contents.yhot = min(max(0, yhot), height - 1)
            ctypes.memmove(img.contents.pixels, (c_uint * len(pixels))(*pixels), 4 * len(pixels))
            return xcursor.XcursorImageLoadCursor(self.dpy, img)
        finally:
            xcursor.XcursorImageDestroy(img)

    def events(self):
        ev = (c_long * 24)()
        while x11.XPending(self.dpy):
            x11.XNextEvent(self.dpy, ev)
            raw = bytes(ev)
            kind = struct.unpack_from("i", raw, 0)[0]
            if kind in (DESTROY_NOTIFY, UNMAP_NOTIFY, MAP_NOTIFY, CONFIGURE_NOTIFY):
                window = struct.unpack_from("L", raw, 40)[0]                  # .window
                size = struct.unpack_from("ii", raw, 56) if kind == CONFIGURE_NOTIFY else None
                yield kind, window, size
            elif kind == PROPERTY_NOTIFY:
                yield kind, struct.unpack_from("L", raw, 32)[0], struct.unpack_from("L", raw, 40)[0]


class Helper:
    def __init__(self, args):
        self.args = args
        self.x = Screen()
        self.window = None           # Moonlight's stream window
        self.size = (0, 0)
        self.host_name = None
        self.layer = None            # our InputOnly window inside it
        self.sock = None
        self.buffer = b""
        self.host = None             # {"width", "height"} from the host
        self.shapes = {}             # id → (pixels, width, height, xhot, yhot)
        self.cursors = {}            # (id, scale) → Cursor
        self.current = None
        self.empty = None
        self.next_search = 0.0
        self.next_connect = 0.0
        self.next_ping = 0.0
        self.last_heard = 0.0
        self.window_lost_at = None

    def log(self, *parts):
        if self.args.verbose:
            print(time.strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)

    # --- stream window ---
    def check_window(self):
        """Which window is the stream window now. Moonlight may recreate it (a second after start it can switch
        renderers, e.g. Vulkan → EGL), so a one-off search is not enough: check every second and move over."""
        found = self.x.stream_window(self.args.pid)
        if found is None:
            if self.window is not None:
                self.lose_window("gone")
            return
        window, width, height, host_name = found
        if window == self.window:
            if (width, height) != self.size:
                self.resize((width, height))
            return
        if self.window is not None:
            self.log(f"stream window recreated: {self.window:#x} → {window:#x}")
            self.remove_layer()
        if self.sock and host_name != self.host_name and not self.args.host:
            self.disconnect(f"now streaming from {host_name!r}")
        self.window, self.size, self.host_name, self.window_lost_at = window, (width, height), host_name, None
        x11.XSelectInput(self.x.dpy, window, STRUCTURE_NOTIFY_MASK)
        self.log(f"stream window {window:#x} {width}x{height}, host {host_name!r}")
        if self.host:
            self.add_layer()
            self.apply()

    def lose_window(self, reason):
        self.log(f"no stream window ({reason})")
        self.remove_layer()          # a closed window took our layer with it; a hidden one we clean up ourselves
        self.window = None
        self.window_lost_at = time.monotonic()
        self.next_search = 0.0

    def resize(self, size):
        self.size = size
        if self.layer:
            x11.XResizeWindow(self.x.dpy, self.layer, max(1, size[0]), max(1, size[1]))
        self.log(f"stream window {size[0]}x{size[1]}")
        self.apply()

    def add_layer(self):
        if not self.window or self.layer:
            return
        width, height = self.size
        errors = len(X_ERRORS)
        layer = x11.XCreateWindow(self.x.dpy, self.window, 0, 0, max(1, width), max(1, height), 0, 0,
                                  INPUT_ONLY, None, 0, None)
        x11.XMapWindow(self.x.dpy, layer)
        x11.XSync(self.x.dpy, 0)
        if len(X_ERRORS) != errors:
            self.lose_window("the window vanished while adding the cursor layer")
            return
        self.layer = layer
        self.log(f"cursor layer {layer:#x}")

    def remove_layer(self):
        if self.layer:
            x11.XDestroyWindow(self.x.dpy, self.layer)
            self.layer = None
        x11.XFlush(self.x.dpy)

    # --- cursor ---
    def free_cursors(self):
        for c in self.cursors.values():
            x11.XFreeCursor(self.x.dpy, c)
        self.cursors.clear()

    def scale(self, shape):
        _, _, video_w, _ = video_rect(*self.size, self.host["width"], self.host["height"])
        return round(cursor_scale(video_w, self.host["width"], shape[1], shape[2], self.args.scale), 3)

    def apply(self):
        if not (self.layer and self.host and self.current is not None):
            return
        if self.current == 0 or self.current not in self.shapes:
            if self.empty is None:
                self.empty = self.x.make_cursor([0], 1, 1, 0, 0)
            cursor = self.empty
        else:
            shape = self.shapes[self.current]
            k = self.scale(shape)
            cursor = self.cursors.get((self.current, k))
            if cursor is None:
                pixels, width, height = resample(shape[0], shape[1], shape[2], k)
                cursor = self.cursors[(self.current, k)] = self.x.make_cursor(
                    pixels, width, height, round(shape[3] * k), round(shape[4] * k))
        x11.XDefineCursor(self.x.dpy, self.layer, cursor)
        x11.XFlush(self.x.dpy)

    # --- network ---
    def candidates(self):
        if self.args.host:
            return [self.args.host]
        found = []
        confs = [self.args.moonlight_conf] if self.args.moonlight_conf else MOONLIGHT_CONFS
        for path in confs:
            try:
                with open(os.path.expanduser(path), encoding="utf-8", errors="replace") as f:
                    found += [a for a in moonlight_addresses(f.read(), self.host_name) if a not in found]
            except OSError:
                pass
        return found or [self.host_name]

    def connect(self):
        for address in self.candidates():
            try:
                self.sock = socket.create_connection((address, self.args.port), timeout=2)
            except OSError as e:
                self.log(f"host {address} port {self.args.port}: {e}")
                continue
            self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.sock.settimeout(5)
            self.last_heard = time.monotonic()
            self.log(f"connected to {address} port {self.args.port}")
            return
        self.sock = None
        self.next_connect = time.monotonic() + 3

    def disconnect(self, reason):
        if self.sock:
            self.log(f"disconnected: {reason}")
            self.sock.close()
        self.sock, self.buffer, self.host, self.current = None, b"", None, None
        self.shapes.clear()
        self.free_cursors()
        self.remove_layer()          # without the host the cursor is back in the video: do not draw ours
        self.next_connect = time.monotonic() + 1

    def receive(self):
        try:
            data = self.sock.recv(65536)
        except OSError as e:
            return self.disconnect(str(e))
        if not data:
            return self.disconnect("host closed the connection")
        self.last_heard = time.monotonic()
        frames, self.buffer = split_frames(self.buffer + data)
        try:
            for kind, payload in frames:
                self.handle(kind, payload)
        except (ValueError, KeyError, struct.error, zlib.error) as e:
            self.disconnect(f"bad data from the host: {e}")

    def handle(self, kind, payload):
        if kind == b"V":
            info = json.loads(payload)
            if info.get("version") != PROTOCOL_VERSION:
                self.log(f"host speaks protocol {info.get('version')}, we speak {PROTOCOL_VERSION}")
            self.host = {"width": int(info["width"]), "height": int(info["height"])}
            if self.host["width"] <= 0 or self.host["height"] <= 0:
                raise ValueError(f"output size {self.host}")
            self.free_cursors()          # scale depends on the output size
            self.add_layer()
            self.apply()
        elif kind == b"F":
            shape_id, width, height, xhot, yhot = struct.unpack_from(">IHHHH", payload)
            if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
                raise ValueError(f"cursor {width}x{height}")
            raw = zlib.decompressobj().decompress(payload[12:], width * height * 4)   # bounded: no zip bombs
            pixels = list(struct.unpack(f"<{width * height}I", raw))
            self.shapes[shape_id] = (pixels, width, height, xhot, yhot)
        elif kind == b"S":
            self.current = struct.unpack(">I", payload)[0]
            self.apply()

    # --- main loop ---
    def step(self):
        now = time.monotonic()
        if self.args.pid and not os.path.exists(f"/proc/{self.args.pid}"):
            self.disconnect("Moonlight exited")
            sys.exit(0)
        if now >= self.next_search:
            self.check_window()
            self.next_search = now + 1
        if self.sock and self.window is None and self.window_lost_at and now - self.window_lost_at > WINDOW_GRACE:
            self.disconnect("stream ended")
        if self.window and not self.sock and now >= self.next_connect:
            self.connect()
        if self.sock:
            if now >= self.next_ping:
                try:
                    self.sock.sendall(frame(b"P"))
                except OSError as e:
                    self.disconnect(str(e))
                self.next_ping = now + 5
            if self.sock and now - self.last_heard > HOST_SILENCE:
                self.disconnect(f"host silent for {HOST_SILENCE} s")

        x11.XFlush(self.x.dpy)
        x_fd = x11.XConnectionNumber(self.x.dpy)
        waiting = [x_fd] + ([self.sock] if self.sock else [])
        ready = select.select(waiting, [], [], 1.0)[0] if not x11.XPending(self.x.dpy) else [x_fd]
        if self.sock and self.sock in ready:
            self.receive()
        for kind, window, extra in self.x.events():
            if kind == PROPERTY_NOTIFY and window == self.x.root and extra == self.x.atom("_NET_CLIENT_LIST"):
                self.next_search = 0.0
            elif window == self.window and kind == DESTROY_NOTIFY:
                self.lose_window("closed")
            elif window == self.window and kind == CONFIGURE_NOTIFY and tuple(extra) != self.size:
                self.resize(tuple(extra))


def single_instance():
    """One helper per X display: autostart and the launcher may both start one."""
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    display_name = (os.environ.get("DISPLAY") or ":0").replace("/", "_")
    lock = open(os.path.join(runtime, f"moonlight-local-cursor-{os.getuid()}{display_name}.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return lock


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="", help="host address (default: what Moonlight.conf has for the streamed host)")
    p.add_argument("--port", type=int, default=48100)
    p.add_argument("--pid", type=int, default=0, help="only follow this Moonlight process and exit with it")
    p.add_argument("--scale", type=float, default=0, help="fixed cursor scale instead of matching the stream")
    p.add_argument("--moonlight-conf", default="", help="path to Moonlight.conf (default: native and Flatpak paths)")
    p.add_argument("--verbose", "-v", action="store_true", help="log what happens to stderr")
    args = p.parse_args()
    lock = single_instance()
    if lock is None:
        if args.verbose:
            print("moonlight-local-cursor: already running on this display", file=sys.stderr)
        return
    helper = Helper(args)
    while True:
        helper.step()


if __name__ == "__main__":
    main()
