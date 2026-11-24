#!/usr/bin/env bash
# moonlight-local-cursor installer (per user, no root needed).
#
#   ./install.sh host   [--listen ADDR] [--port N] [--allow CIDR ...] [--output NAME]
#   ./install.sh client [--host ADDR] [--port N] [--absolute-mouse] [--xwayland]
#   ./install.sh uninstall host|client
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/moonlight-local-cursor"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}/moonlight-local-cursor"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/moonlight-local-cursor.desktop"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
UNIT=moonlight-local-cursor-host.service
FLATPAK_ID=com.moonlight_stream.Moonlight

say() { printf '\033[1m%s\033[0m\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

need_python() {
    command -v python3 >/dev/null || die "python3 is required"
}

sunshine_unit() {
    systemctl --user list-units --all --no-legend --plain 2>/dev/null | awk '{print $1}' \
        | grep -iE '^(sunshine|app-dev\.lizardbyte\.app\.sunshine)[^ ]*\.service$' | head -n1 || true
}

install_host() {
    local listen=0.0.0.0 port=48100 output="" allow=()
    while [ $# -gt 0 ]; do
        case "$1" in
            --listen) listen="$2"; shift 2 ;;
            --port) port="$2"; shift 2 ;;
            --output) output="$2"; shift 2 ;;
            --allow) shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do allow+=("$1"); shift; done ;;
            -h|--help) usage ;;
            *) die "unknown host option: $1" ;;
        esac
    done
    need_python
    python3 -c 'import Xlib.ext.xfixes' 2>/dev/null || die "python-xlib is missing. Install it:
  Fedora: sudo dnf install python3-xlib      Debian/Ubuntu: sudo apt install python3-xlib
  Arch:   sudo pacman -S python-xlib          other: python3 -m pip install --user python-xlib"
    command -v systemctl >/dev/null || die "systemd (user units) is required for the host service"

    mkdir -p "$PREFIX" "$CONF" "$UNITS"
    install -m 755 "$HERE/host/mlc-host.py" "$PREFIX/mlc-host.py"

    local args="--listen $listen --port $port"
    [ -n "$output" ] && args+=" --output $output"
    [ ${#allow[@]} -gt 0 ] && args+=" --allow ${allow[*]}"
    cat > "$CONF/host.env" <<EOF
# moonlight-local-cursor host settings. After editing: systemctl --user restart $UNIT
MLC_HOST_ARGS=$args
DISPLAY=${DISPLAY:-:0}
EOF

    local sunshine; sunshine="$(sunshine_unit)"
    local after="graphical-session.target" partof="" wanted="default.target"
    if [ -n "$sunshine" ]; then
        after="$sunshine graphical-session.target"; partof="PartOf=$sunshine"; wanted="$sunshine"
    fi
    cat > "$UNITS/$UNIT" <<EOF
[Unit]
Description=moonlight-local-cursor host: game cursor drawn by Moonlight clients, not in the video
After=$after
$partof

[Service]
EnvironmentFile=$CONF/host.env
ExecStart=/usr/bin/env python3 $PREFIX/mlc-host.py \$MLC_HOST_ARGS
Restart=always
RestartSec=3

[Install]
WantedBy=$wanted
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$UNIT" >/dev/null
    systemctl --user restart "$UNIT"
    sleep 1
    systemctl --user is-active --quiet "$UNIT" || die "the service did not start: journalctl --user -u $UNIT"

    say "Host installed: $UNIT is running (${sunshine:+tied to $sunshine; }settings in $CONF/host.env)."
    echo "Streamed output: ${output:-the primary one}. Outputs:"
    if command -v xrandr >/dev/null; then xrandr --listmonitors 2>/dev/null | tail -n +2 | sed 's/^/  /'; fi
    echo "Open TCP port $port for your clients only, for example:"
    echo "  firewalld: sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=CLIENT_IP port port=$port protocol=tcp accept' && sudo firewall-cmd --reload"
    echo "  ufw:       sudo ufw allow from CLIENT_IP to any port $port proto tcp"
    echo "Logs: journalctl --user -u $UNIT -f"
}

set_absolute_mouse() {
    if pgrep -x moonlight >/dev/null 2>&1; then
        echo "Moonlight is running, so its settings were not touched. Close it and run again with --absolute-mouse,"
        echo "or enable Settings → 'Optimize mouse for remote desktop instead of games' yourself."
        return
    fi
    local done_any=""
    for conf in "$HOME/.config/Moonlight Game Streaming Project/Moonlight.conf" \
                "$HOME/.var/app/$FLATPAK_ID/config/Moonlight Game Streaming Project/Moonlight.conf"; do
        [ -f "$conf" ] || continue
        python3 - "$conf" <<'PY'
import sys
path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines()
out, section, done = [], None, False
for line in lines:
    s = line.strip()
    if s.startswith("[") and s.endswith("]"):
        if section == "General" and not done:
            out.append("mouseacceleration=true"); done = True
        section = s[1:-1]
    elif section == "General" and s.startswith("mouseacceleration="):
        line, done = "mouseacceleration=true", True
    out.append(line)
if not done:
    if section != "General":
        out += ["", "[General]"]
    out.append("mouseacceleration=true")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
        echo "Absolute mouse mode enabled in: $conf"; done_any=1
    done
    [ -n "$done_any" ] || echo "No Moonlight.conf yet: start Moonlight once, or enable 'Optimize mouse for remote desktop instead of games'."
}

setup_xwayland() {
    if command -v flatpak >/dev/null && flatpak info "$FLATPAK_ID" >/dev/null 2>&1; then
        flatpak override --user --env=QT_QPA_PLATFORM=xcb --env=SDL_VIDEODRIVER=x11 "$FLATPAK_ID"
        echo "Flatpak Moonlight will now run under XWayland."
    elif command -v moonlight >/dev/null; then
        mkdir -p "$APPS"
        cat > "$APPS/moonlight-local-cursor.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Moonlight (local cursor)
Comment=Moonlight under XWayland, so moonlight-local-cursor can draw the game cursor locally
Exec=env QT_QPA_PLATFORM=xcb SDL_VIDEODRIVER=x11 moonlight %U
Icon=moonlight
Categories=Game;Network;
EOF
        echo "Added the menu entry 'Moonlight (local cursor)': start Moonlight from it."
    else
        echo "Start Moonlight with: QT_QPA_PLATFORM=xcb SDL_VIDEODRIVER=x11 <your Moonlight command>"
    fi
}

install_client() {
    local host="" port=48100 absolute="" xwayland=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --host) host="$2"; shift 2 ;;
            --port) port="$2"; shift 2 ;;
            --absolute-mouse) absolute=1; shift ;;
            --xwayland) xwayland=1; shift ;;
            -h|--help) usage ;;
            *) die "unknown client option: $1" ;;
        esac
    done
    need_python
    python3 -c 'import ctypes; ctypes.CDLL("libX11.so.6"); ctypes.CDLL("libXi.so.6"); ctypes.CDLL("libXcursor.so.1")' \
        2>/dev/null || die "libX11, libXi and libXcursor are required (packages libx11 / libxi / libxcursor)"

    mkdir -p "$PREFIX" "$(dirname "$AUTOSTART")"
    install -m 755 "$HERE/client/mlc-client.py" "$PREFIX/mlc-client.py"
    local cmd="/usr/bin/env python3 $PREFIX/mlc-client.py --port $port"
    [ -n "$host" ] && cmd+=" --host $host"
    cat > "$AUTOSTART" <<EOF
[Desktop Entry]
Type=Application
Name=Moonlight local cursor
Comment=Draws the streamed game's cursor locally in Moonlight windows
Exec=$cmd
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
    pkill -f "$PREFIX/mlc-client.py" 2>/dev/null || true
    if [ -n "${DISPLAY:-}" ]; then
        nohup $cmd >/dev/null 2>&1 &
        disown || true
    fi

    [ -n "$absolute" ] && set_absolute_mouse
    if [ -n "$xwayland" ]; then
        setup_xwayland
    elif [ "${XDG_SESSION_TYPE:-}" = wayland ]; then
        echo "Note: this is a Wayland session. The helper speaks X11, so Moonlight has to run under XWayland:"
        echo "  run ./install.sh client --xwayland, or start Moonlight with QT_QPA_PLATFORM=xcb SDL_VIDEODRIVER=x11"
    fi
    say "Client installed: the helper starts with your session and is running now."
    echo "It finds Moonlight's stream window by itself and connects to ${host:-the host Moonlight streams from} port $port."
    [ -n "$absolute" ] || echo "Moonlight needs absolute mouse mode: Settings → 'Optimize mouse for remote desktop instead of games' (or --absolute-mouse)."
    echo "Troubleshooting: python3 $PREFIX/mlc-client.py --verbose   (stop the running one first: pkill -f mlc-client.py)"
}

uninstall() {
    case "${1:-}" in
        host)
            systemctl --user disable --now "$UNIT" 2>/dev/null || true
            rm -f "$UNITS/$UNIT" "$PREFIX/mlc-host.py" "$CONF/host.env"
            systemctl --user daemon-reload
            say "Host removed." ;;
        client)
            pkill -f "$PREFIX/mlc-client.py" 2>/dev/null || true
            rm -f "$AUTOSTART" "$PREFIX/mlc-client.py" "$APPS/moonlight-local-cursor.desktop"
            if command -v flatpak >/dev/null && flatpak info "$FLATPAK_ID" >/dev/null 2>&1; then
                flatpak override --user --unset-env=QT_QPA_PLATFORM --unset-env=SDL_VIDEODRIVER "$FLATPAK_ID" 2>/dev/null || true
            fi
            say "Client removed." ;;
        *) usage 1 ;;
    esac
    rmdir "$PREFIX" "$CONF" 2>/dev/null || true
}

case "${1:-}" in
    host) shift; install_host "$@" ;;
    client) shift; install_client "$@" ;;
    uninstall) shift; uninstall "$@" ;;
    -h|--help|"") usage ;;
    *) usage 1 ;;
esac
