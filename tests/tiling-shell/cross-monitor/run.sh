#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Cross-monitor drags with Tiling Shell: GNOME Shell on a virtual 3440x1440
# and a virtual 2560x1440 monitor side by side, each with its own layout.
# Real pointer drags (driver.js) cross between them; at every stop the zones
# drawn, the selection and the final drop must be exactly the layout of the
# monitor under the pointer. Needs a Luma OS tree, or Fedora 44 with the
# Luma gnome-shell, mutter and Tiling Shell installed, plus gtk4,
# python3-gobject, mesa-dri-drivers and dbus-daemon; no GPU, display server
# or logind session.
#
#   run.sh OUTDIR [MAIN_SCALE SIDE_SCALE SIDE_LAYOUT [WORKSPACE]]
#
# SIDE_LAYOUT is rows, quarters, thirds, uneven, none (no layout of its own:
# the side monitor follows the main one's) or reported (the layouts and
# selection of the setup the bug was reported on: four fixed workspaces with
# workspaces on every monitor, two of them listed, three monitors' entries,
# the other monitor numbered before the main one). WORKSPACE is the index of
# the workspace the drags run on. Without arguments the matrix below runs.
# LUMA_XM_THREE=1 adds a laptop panel that is then closed. Each case writes
# shell.log and result.json to OUTDIR/<case>; the exit status is the number
# of failing cases.
set -uo pipefail

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../../.." && pwd)
root=$1
shift

if [ $# -ge 3 ]; then
  cases=("$1 $2 $3 ${4:-0}")
else
  cases=("1 1 rows 0" "1 1 rows 2" "1 1 uneven 0" "1 1 none 1" "1 1.25 thirds 0"
         "1 2 quarters 1" "1.25 1 rows 0" "2 1 uneven 0" "1.25 1.25 uneven 3"
         "1 1 reported 0" "1 1 reported 1" "1 1 reported 2")
fi

# A container has no system bus or logind; GNOME Shell wants the first and
# must not wait for the second.
if [ "$(id -u)" = 0 ] && [ ! -S /run/dbus/system_bus_socket ]; then
  mkdir -p /run/dbus
  dbus-daemon --system --fork
fi
rm -rf /run/systemd/seats 2>/dev/null || :

failed=0
for c in "${cases[@]}"; do
  read -r main side layout ws <<<"$c"
  out=$root/main$main-side$side-$layout-ws$ws
  rm -rf "$out"
  mkdir -p "$out/runtime" "$out/config/glib-2.0/settings" "$out/data"
  chmod 0700 "$out/runtime"
  side_first=0
  # The OS defaults first (dconf luma.d uses the keyfile syntax), as a fresh
  # Luma account has them, then the test's own keys.
  keyfile=$out/config/glib-2.0/settings/keyfile
  cat /etc/dconf/db/luma.d/* >"$keyfile" 2>/dev/null || :
  cat >>"$keyfile" <<'KEYFILE'

[org/gnome/shell]
welcome-dialog-last-shown-version='999'
disable-user-extensions=false
enabled-extensions=['tilingshell@ferrarodomenico.com']
KEYFILE
  if [ "$layout" = reported ]; then
    side_first=1
    cat >>"$keyfile" <<'KEYFILE'

[org/gnome/mutter]
workspaces-only-on-primary=false
dynamic-workspaces=false

[org/gnome/desktop/wm/preferences]
num-workspaces=4
KEYFILE
  fi
  monitors="--virtual-monitor 3440x1440 --virtual-monitor 2560x1440"
  [ "${LUMA_XM_THREE:-0}" = 1 ] && monitors="$monitors --virtual-monitor 1920x1200"
  # shellcheck disable=SC2086
  env -u DISPLAY -u WAYLAND_DISPLAY \
    XDG_RUNTIME_DIR="$out/runtime" XDG_CONFIG_HOME="$out/config" \
    XDG_DATA_HOME="$out/data" GSETTINGS_BACKEND=keyfile LIBGL_ALWAYS_SOFTWARE=1 \
    GTK_A11Y=none NO_AT_BRIDGE=1 \
    LUMA_XM_OUT="$out" LUMA_XM_MAIN_SCALE="$main" LUMA_XM_SIDE_SCALE="$side" \
    LUMA_XM_SIDE_LAYOUT="$layout" LUMA_XM_WS="$ws" LUMA_XM_SIDE_FIRST="$side_first" \
    LUMA_XM_TRACE="${LUMA_XM_TRACE:-0}" LUMA_XM_FAST="${LUMA_XM_FAST:-0}" \
    LUMA_XM_RECONFIG="${LUMA_XM_RECONFIG:-0}" LUMA_XM_SPAN_KEY="${LUMA_XM_SPAN_KEY:-}" \
    LUMA_XM_APP="$repo/tests/gnome-shell/shelf-presentation/app.py" \
    dbus-run-session -- timeout 300 gnome-shell --headless --no-x11 $monitors \
    --automation-script="$here/driver.js" >"$out/shell.log" 2>&1
  python3 - "$out/result.json" "$c" <<'PY' || failed=$((failed + 1))
import json, sys
path, case = sys.argv[1], sys.argv[2]
try:
    result = json.load(open(path))
except FileNotFoundError:
    print(f"[{case}] no result: see shell.log")
    sys.exit(1)
bad = [r for r in result["results"] if not r["ok"]]
for r in bad:
    print(f"[{case}] FAIL {r['name']} -- {r['detail']}")
print(f"[{case}] {len(result['results']) - len(bad)} passed, {len(bad)} failed")
sys.exit(1 if bad or not result["results"] else 0)
PY
done
exit "$failed"
