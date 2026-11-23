# moonlight-local-cursor

Steam Remote Play-style cursor for **Sunshine → Moonlight** game streaming on Linux: the client draws the game's
cursor itself, as a native cursor, so it moves instantly. The host keeps it out of the video, so there is exactly one
cursor on screen.

[Українською](README.uk.md)

## Why

Moonlight shows the host's cursor as part of the video. Every movement has to travel to the host and come back as an
encoded frame, so the cursor lags by the full stream latency. On a 100–200 ms link, point-and-click games (RPGs,
strategy, anything with a mouse cursor) become miserable. Moonlight's local-cursor toggle gives you a second, generic
arrow on top of the lagging one.

Steam Remote Play solves this by sending the cursor *shape* to the client and letting the client draw it. This project
does the same for Sunshine and Moonlight when the host runs Linux.

- The client draws the cursor as a native cursor, with no stream latency.
- You get the game's real cursor: the sword, the hand, the dialog bubble, with the right hotspot.
- The cursor is scaled the way the stream is scaled, and never below 24 px. A 32 px cursor from a 4K host stays
  usable in a 1080p window.
- The host cursor is taken out of the video, so you see one cursor, not two.
- No configuration on the client: it finds Moonlight's stream window by itself and takes the host address from
  Moonlight's own settings.
- Also fixes the "one frame forward, one frame back" jitter that Sunshine's KMS capture can show during camera
  movement (see [Scanout blocker](#scanout-blocker)).

## How it works

```
host (Linux, Sunshine, X11 or Wayland + XWayland)        client (Linux, Moonlight on X11/XWayland)
┌───────────────────────────────────────────┐            ┌───────────────────────────────────────────┐
│ game (Proton/Wine/X11) sets an X cursor   │            │ mlc-client.py                             │
│ mlc-host.py                               │  TCP 48100 │  · finds Moonlight's stream window        │
│  · XFixes: cursor shape + hotspot  ───────┼───────────►│  · puts an InputOnly window over it       │
│  · XFixesHideCursor: no cursor on the     │            │  · gives it the host's cursor shape       │
│    host screen or in Sunshine's capture   │            │    (scaled like the stream, ≥ 24 px)      │
│  · 1x1 scanout blocker while streaming    │            │  Moonlight sends absolute mouse positions │
└───────────────────────────────────────────┘            └───────────────────────────────────────────┘
```

- **Host** (`host/mlc-host.py`): reads the X cursor through XFixes and sends each new shape once, then only its id.
  While a client is connected it hides the cursor over X windows (`XFixesHideCursor` on the root window). XWayland
  then hands the compositor an empty cursor, so Sunshine never captures it, whether the compositor uses a hardware
  cursor plane or draws the cursor into the frame. When the last client leaves, or the service stops, the cursor
  comes back on its own.
- **Client** (`client/mlc-client.py`): plain Python with libX11 and libXcursor through ctypes. The local X server
  draws the cursor. The InputOnly window selects no events, so clicks and motion reach Moonlight untouched. When
  Moonlight recreates its window (renderer switch, resize), the helper follows.

The wire protocol is in [PROTOCOL.md](PROTOCOL.md). It is small enough for a Windows, macOS or Android client.

## Requirements

**Host:** Linux with Sunshine; games that use an X cursor (anything under XWayland: Proton, Wine, most native games
on X11); Python 3 with `python-xlib`; a systemd user session.

**Client:** Linux with Moonlight (native, Flatpak or AppImage) running on X11 or XWayland; Python 3, `libX11`,
`libXcursor`. Moonlight must be in **absolute mouse mode**: *Settings → Optimize mouse for remote desktop instead of
games*.

## Install

```bash
git clone https://github.com/PiGYk/moonlight-local-cursor.git
cd moonlight-local-cursor
```

**On the host** (where Sunshine runs):

```bash
./install.sh host                    # listens on 0.0.0.0:48100, accepts private and loopback addresses
./install.sh host --output DP-1      # if Sunshine streams a monitor other than the primary one
./install.sh host --listen 10.0.0.2 --allow 10.0.0.7/32    # only one client, only over a VPN address
```

This installs a systemd user service `moonlight-local-cursor-host`. If Sunshine runs as a user service, the host
service starts and stops together with it. Open TCP port 48100 to your clients in the firewall; the installer prints
the command for firewalld and ufw.

**On the client** (where Moonlight runs):

```bash
./install.sh client --absolute-mouse         # also turns on absolute mouse mode (close Moonlight first)
./install.sh client --xwayland               # on a Wayland session: run Moonlight under XWayland
./install.sh client --host 192.168.1.50      # only if the host is not reachable at the address Moonlight uses
```

The helper starts with your desktop session and waits for a Moonlight stream. Then stream as usual.

Uninstall: `./install.sh uninstall host` or `./install.sh uninstall client`.

## Options

Host (`~/.config/moonlight-local-cursor/host.env`, then `systemctl --user restart moonlight-local-cursor-host`):

| Option | Default | Meaning |
|---|---|---|
| `--listen` | `0.0.0.0` | address to listen on, `::` for IPv4 and IPv6 |
| `--port` | `48100` | TCP port |
| `--allow` | private and loopback ranges | client networks allowed to connect, `any` for everyone |
| `--output` | primary output | RandR name of the streamed output (`xrandr --listmonitors`) |
| `--display` | `$DISPLAY` or `:0` | X display |
| `--no-scanout-blocker` | | do not keep the 1x1 window (see below) |
| `--keep-host-cursor` | | do not hide the host cursor (debugging) |

Client (`mlc-client.py`): `--host`, `--port`, `--scale` (fixed cursor scale), `--moonlight-conf` (path to
`Moonlight.conf`), `--pid` (follow one Moonlight process and exit with it), `--verbose`.

## Scanout blocker

Once the cursor is hidden, a fullscreen game can be the only thing on the output. The compositor may then hand the
game's buffers straight to the display (direct scanout) instead of composing a frame. Sunshine's KMS capture reads
the buffer on the screen plane, but the game is already rendering the next frames into it, so the stream jumps a
frame back and forth whenever the camera moves.

We hit this with labwc (wlroots 0.20) on NVIDIA. A visible software cursor had been blocking direct scanout, and hiding
it uncovered the problem. While clients are connected, the host keeps a 1x1 override-redirect window with alpha 1/255
in the corner of the streamed output. It is invisible, clicks pass through it, it never takes focus, and it makes the
compositor compose again. To check on your machine, look at the primary plane of the streamed output in
`/sys/kernel/debug/dri/*/state`. Without the blocker it cycles through the game's buffers (5 in our case). With the
blocker it uses the compositor's own buffers (2).

The same jitter can show up without this tool on compositors that keep the cursor on a hardware plane, because there
the cursor never blocks direct scanout. The blocker helps there too. Disable it with `--no-scanout-blocker`.

## Limitations

- X cursors only. Native Wayland apps on the host get the compositor's own cursor, and that one stays in the video.
- The client speaks X11. On a Wayland session, Moonlight has to run under XWayland (`--xwayland` sets that up).
- Absolute mouse mode only. Games that grab the mouse for camera look need relative mode. Moonlight switches modes with
  Ctrl+Alt+Shift+M, but then the local cursor no longer matches the game. Steam switches automatically; Moonlight
  cannot do that without changes of its own.
- One hide flag for all clients: while any client is connected, the cursor is hidden for everyone, including other
  Moonlight clients that do not run the helper.
- No Windows, macOS, Android or iOS client yet. Steam Deck Game Mode is untested.

## Security

The side channel carries only cursor images and pings, with no input and no screen content. It has no encryption or
authentication, so keep it on a LAN or VPN. By default the host accepts only private and loopback addresses; narrow it
with `--listen`, `--allow` and your firewall.

## Prior art

"Cursor protocol v2" by jtsorlinis carries the cursor inside Moonlight's own stream and can switch mouse modes
automatically. It needs a Windows host: [Vibeshine PR #367](https://github.com/Nonary/vibeshine/pull/367) (host),
[qiin2333/moonlight-qt PR #283](https://github.com/qiin2333/moonlight-qt/pull/283) (client). This project is a side
channel for Linux hosts that needs no rebuild of Sunshine or Moonlight.

## Tested with

Sunshine 2026.914 on Nobara Linux 44 (labwc with wlroots 0.20, NVIDIA RTX 4070, KMS capture), Moonlight-qt 6.2.0
AppImage on MX Linux (Debian 13, Xfce, X11), Divinity: Original Sin 2 under Proton, over WireGuard.
Reports from other setups are welcome.

## License

MIT
