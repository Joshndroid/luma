#!/bin/bash
# as-user.sh CMD...: run inside the VM as luma-sync in its graphical session's environment.
u=luma-sync; uid=$(id -u $u)
exec runuser -u $u -- env HOME=/home/$u XDG_RUNTIME_DIR=/run/user/$uid DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$uid/bus \
  WAYLAND_DISPLAY=wayland-0 XDG_SESSION_TYPE=wayland PATH=/home/$u/.local/bin:/usr/local/bin:/usr/bin:/bin "$@"
