#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Headless test of windows hidden from capture (patches/mutter 0042,
# luma_window_capture_v1, LumaUI luma_window_set_hidden_from_capture()).
# GNOME Shell on two virtual 1920x1080 monitors with Xwayland, PipeWire and
# WirePlumber; windows painted one solid colour each, recorded through
# org.gnome.Mutter.ScreenCast (monitor, area and window streams, read with
# GStreamer's pipewiresrc in capture.c) and Shell screenshots, counted pixel
# by pixel. Needs Fedora 44 with gnome-shell and the Mutter under test,
# gtk4-devel, gtk3, wayland-devel, python3-gobject, libX11, mesa-dri-drivers,
# dbus-daemon, pipewire, wireplumber, pipewire-gstreamer,
# gstreamer1-plugins-base-devel, cairo-devel and Xwayland; no GPU, display
# server or logind session.
#
#   run.sh OUTDIR [luma|stock]          the checks (driver.js)
#   MODE=perf run.sh OUTDIR             what a monitor stream costs with no
#                                       hidden window, one on the other
#                                       monitor and one on its monitor
#
# "stock" expects a Mutter without the protocol: the kit reports it and the
# window stays in the stream. result.json has every check, every recording's
# pixel counts and the perf numbers; the exit status is the number of failures.
set -euo pipefail

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../../.." && pwd)
out=$1
expect=${2:-luma}
rm -rf "$out/runtime"
mkdir -p "$out/build" "$out/runtime" "$out/config/glib-2.0/settings" "$out/data"
chmod 0700 "$out/runtime"
rm -f "$out"/*.png "$out"/*.log "$out"/*.stop "$out/result.json" "$out/driver-trace.txt"

gcc -O2 -Wall -Wextra -Werror -Wno-unused-parameter "$here/capture.c" \
  $(pkg-config --cflags --libs gio-2.0 gstreamer-1.0 gstreamer-app-1.0 gstreamer-video-1.0 cairo) \
  -o "$out/build/capture"

protocol=$repo/src/luma-platform/protocols/luma-window-capture-v1.xml
wayland-scanner client-header "$protocol" "$out/build/luma-window-capture-v1-client-protocol.h"
wayland-scanner private-code "$protocol" "$out/build/luma-window-capture-v1-client-protocol.c"
# The client is built from the kit's own luma-window-capture.c.
gcc -O1 -Wall -Wextra -Werror -Wno-unused-parameter -DLUMA_HAVE_WAYLAND \
  -I"$out/build" -I"$repo/src/luma-platform/ui" \
  "$here/client.c" "$repo/src/luma-platform/ui/luma-window-capture.c" \
  "$out/build/luma-window-capture-v1-client-protocol.c" \
  $(pkg-config --cflags --libs gtk4 wayland-client) -o "$out/build/client"

cat >"$out/config/glib-2.0/settings/keyfile" <<'KEYFILE'
[org/gnome/shell]
welcome-dialog-last-shown-version='999'

[org/gnome/desktop/interface]
enable-animations=false
KEYFILE

rpm -q mutter gnome-shell gtk4 gtk3 pipewire >"$out/packages.txt" 2>&1 || :

status=0
env -u DISPLAY -u WAYLAND_DISPLAY \
  XDG_RUNTIME_DIR="$out/runtime" XDG_CONFIG_HOME="$out/config" \
  XDG_DATA_HOME="$out/data" GSETTINGS_BACKEND=keyfile LIBGL_ALWAYS_SOFTWARE=1 \
  GTK_A11Y=none NO_AT_BRIDGE=1 \
  LUMA_CAPTURE_OUT="$out" LUMA_CAPTURE_HERE="$here" \
  LUMA_CAPTURE_BIN="$out/build/capture" LUMA_CAPTURE_CLIENT="$out/build/client" \
  LUMA_CAPTURE_EXPECT="$expect" LUMA_CAPTURE_MODE="${MODE:-full}" \
  dbus-run-session -- sh -c '
    pipewire >"$LUMA_CAPTURE_OUT/pipewire.log" 2>&1 &
    sleep 1
    wireplumber >"$LUMA_CAPTURE_OUT/wireplumber.log" 2>&1 &
    sleep 1
    exec timeout 900 gnome-shell --headless \
      --virtual-monitor 1920x1080 --virtual-monitor 1920x1080 \
      --automation-script="$LUMA_CAPTURE_HERE/driver.js"' \
  >"$out/shell.log" 2>&1 || status=$?

python3 - "$out/result.json" "$status" <<'PY'
import json, sys
path, status = sys.argv[1], int(sys.argv[2])
try:
    result = json.load(open(path))
except FileNotFoundError:
    print(f"no result: gnome-shell exited with {status}")
    sys.exit(1)
failed = [r for r in result["results"] if not r["ok"]]
for r in result["results"]:
    print(("PASS " if r["ok"] else "FAIL ") + r["name"] + ("  -- " + r["detail"] if not r["ok"] else ""))
for p in result.get("perf", []):
    fps = " ".join(f"{v:.1f}" for v in p["fps"])
    cpu = " ".join(f"{v:.0f}" for v in p["shellCpuMsPerS"])
    print(f"  perf {p['label']}: paints {p['paints']}, query {p['queryUs']:.2f} us, "
          f"fps {fps}, shell CPU ms/s {cpu}")
print(f"{len(result['results']) - len(failed)} passed, {len(failed)} failed")
sys.exit(min(len(failed), 100))
PY
