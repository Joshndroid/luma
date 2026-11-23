#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Headless test of session save and restore (ADR-055; patches/mutter 0047,
# patches/gnome-shell 0392 lumaSession.js). Three GNOME Shell runs over one
# home: "save" opens GTK 4 and X11 windows in every state on two virtual
# 1920x1080 monitors and takes the log-out snapshot; "restore" is the next
# login, which restores it, and every window's frame and floating
# rectangles, state, workspace, monitor, stacking and focus are compared
# with what was saved; "remap" continues the snapshot on one 2560x1600
# monitor. Needs Fedora 44 with gnome-shell and the Mutter under test,
# gtk4, python3-gobject, python3-xlib, Xwayland, mesa-dri-drivers and
# dbus-daemon; no GPU, display server or logind session.
#
#   run.sh OUTDIR [SHELL_SOURCE]
#
# SHELL_SOURCE is a GNOME Shell tree with Luma's patches applied (default:
# the module from patches/gnome-shell/0392 is extracted). result.json has
# every check; the exit status is the number of failures.
set -euo pipefail

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../../.." && pwd)
out=$1
source_tree=${2:-}
rm -rf "$out"
mkdir -p "$out/runtime" "$out/config/glib-2.0/settings" "$out/data/applications" "$out/state" "$out/module"
chmod 0700 "$out/runtime"

# The module, with its imports pointed at the running Shell's resources.
if [ -n "$source_tree" ]; then
  cp "$source_tree/js/ui/lumaSession.js" "$out/module/lumaSession.js"
else
  python3 - "$repo/patches/gnome-shell/0392-session-snapshots-and-restore.patch" "$out/module/lumaSession.js" <<'PY'
import sys
patch, dest = sys.argv[1], sys.argv[2]
lines, take = [], False
for line in open(patch):
    if line.startswith("+++ b/"):
        take = line.strip() == "+++ b/js/ui/lumaSession.js"
        continue
    if take and line.startswith("diff --git"):
        break
    if take and line.startswith("+"):
        lines.append(line[1:])
open(dest, "w").write("".join(lines))
PY
fi
sed -i -e "s#from '\.\./misc/#from 'resource:///org/gnome/shell/misc/#" \
  -e "s#from '\./#from 'resource:///org/gnome/shell/ui/#" "$out/module/lumaSession.js"

for app in docs counter x11 keyed; do
  install -m 0755 "$here/$app.py" "$out/$app.py"
done
cat >"$out/data/applications/org.projectluma.ContinueDocs.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Continue Docs
Exec=python3 $out/docs.py
DBusActivatable=false
DESKTOP
cat >"$out/data/applications/org.projectluma.ContinueCounter.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Continue Counter
Exec=python3 $out/counter.py
DESKTOP
cat >"$out/data/applications/org.projectluma.ContinueX11.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Continue X11
Exec=env GDK_BACKEND=x11 python3 $out/x11.py
StartupWMClass=ContinueX11
DESKTOP

# The LumaUI app runs on the kit under test: LUMA_PLATFORM_SOURCE is
# src/luma-platform, LUMA_PLATFORM_BUILD its meson build directory.
platform_src=${LUMA_PLATFORM_SOURCE:-$repo/src/luma-platform}
platform_build=${LUMA_PLATFORM_BUILD:-}
typelibs=$(find "${platform_build:-/nonexistent}" -name '*.typelib' -printf '%h:' 2>/dev/null || :)
libs=$(find "${platform_build:-/nonexistent}" -name '*.so*' -printf '%h:' 2>/dev/null || :)
cat >"$out/data/applications/org.projectluma.ContinueKeyed.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Continue Keyed
Exec=env PYTHONPATH=$platform_src/appkit GI_TYPELIB_PATH=$typelibs LD_LIBRARY_PATH=$libs python3 $out/keyed.py
DESKTOP

cat >"$out/config/glib-2.0/settings/keyfile" <<'KEYFILE'
[org/gnome/shell]
welcome-dialog-last-shown-version='999'

[org/gnome/desktop/interface]
enable-animations=false

[org/gnome/mutter]
dynamic-workspaces=false

[org/gnome/desktop/wm/preferences]
num-workspaces=4
KEYFILE

rpm -q mutter gnome-shell gtk4 >"$out/packages.txt" 2>&1 || :

phase() {
  local name=$1; shift
  env -u DISPLAY -u WAYLAND_DISPLAY \
    XDG_RUNTIME_DIR="$out/runtime" XDG_CONFIG_HOME="$out/config" \
    XDG_DATA_HOME="$out/data" XDG_STATE_HOME="$out/state" \
    GSETTINGS_BACKEND=keyfile LIBGL_ALWAYS_SOFTWARE=1 GTK_A11Y=none NO_AT_BRIDGE=1 \
    LUMA_SESSION_OUT="$out" LUMA_SESSION_PHASE="$name" \
    LUMA_SESSION_MODULE="$out/module/lumaSession.js" \
    dbus-run-session -- timeout 300 gnome-shell --headless "$@" \
      --automation-script="$here/driver.js" >"$out/shell-$name.log" 2>&1 || :
  # Clients of the last session end with it, as at log-out.
  pkill -f "$out/(docs|counter|x11|keyed).py" 2>/dev/null || :
  sleep 1
}

phase save --virtual-monitor 1920x1080 --virtual-monitor 1920x1080
phase restore --virtual-monitor 1920x1080 --virtual-monitor 1920x1080
phase remap --virtual-monitor 2560x1600

python3 - "$out/result.json" <<'PY'
import json, sys
try:
    result = json.load(open(sys.argv[1]))
except FileNotFoundError:
    print("no result")
    sys.exit(1)
failed = [r for r in result["results"] if not r["ok"]]
for r in result["results"]:
    print(("PASS " if r["ok"] else "FAIL ") + r["name"] + ("  -- " + r["detail"] if not r["ok"] else ""))
print(f"{len(result['results']) - len(failed)} passed, {len(failed)} failed")
sys.exit(min(len(failed), 100))
PY
