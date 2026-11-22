# SPDX-License-Identifier: Apache-2.0
"""Quiet points (ADR-054 §4.3): an app's folder is touched only while the app
is closed and 20 seconds have passed with no new launch.

Flatpak instances are read from ``$XDG_RUNTIME_DIR/.flatpak/<instance>``
(what ``flatpak ps`` reads); ADR-052 store apps hold a shared lock on
``$XDG_RUNTIME_DIR/luma-apps/<id>/.running`` while any launcher runs them.
"""
from __future__ import annotations

import configparser
import fcntl
import json
import os
import threading
import time
from pathlib import Path

from .appdata import QUIET_SECONDS


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def running_flatpaks() -> set[str]:
    root = runtime_dir() / ".flatpak"
    out: set[str] = set()
    try:
        instances = list(root.iterdir())
    except OSError:
        return out
    for instance in instances:
        try:
            info = configparser.ConfigParser(strict=False, interpolation=None)
            info.read(instance / "info")
            app = info.get("Application", "name", fallback="")
            pid = int(json.loads((instance / "bwrapinfo.json").read_text()).get("child-pid") or 0)
        except (OSError, ValueError, configparser.Error):
            continue
        if app and pid and Path(f"/proc/{pid}").exists():
            out.add(app)
    return out


def running_store_apps() -> set[str]:
    root = runtime_dir() / "luma-apps"
    out: set[str] = set()
    try:
        folders = list(root.iterdir())
    except OSError:
        return out
    for folder in folders:
        try:
            with open(folder / ".running", "a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except FileNotFoundError:
            continue
        except OSError:
            out.add(folder.name)
    return out


def installed_stamp() -> tuple:
    """Changes when an app is installed or removed (Flatpak user and system, ADR-052 records)."""
    folders = [Path.home() / ".local/share/flatpak/app", Path("/var/lib/flatpak/app"),
               Path.home() / ".local/share/luma/apps", Path.home() / ".local/share/luma/installer/applications"]
    out = []
    for folder in folders:
        try:
            out.append(folder.stat().st_mtime_ns)
        except OSError:
            out.append(0)
    return tuple(out)


def running() -> set[str]:
    return running_flatpaks() | running_store_apps()


class Watcher:
    """Remembers when each app was last seen running; calls back when one closes."""

    def __init__(self, on_closed=None, interval: float = 3.0, on_installed=None) -> None:
        self.last_running: dict[str, float] = {}
        self.current: set[str] = set()
        self.on_closed = on_closed
        self.interval = interval
        self.requested: set[str] = set()
        self.on_installed = on_installed
        self._installed = installed_stamp()
        self._stop = threading.Event()

    def poll(self) -> set[str]:
        now = time.monotonic()
        current = running()
        for app in current:
            self.last_running[app] = now
        closed = self.current - current
        self.current = current
        stamp = installed_stamp()
        if stamp != self._installed:
            self._installed = stamp
            if self.on_installed:
                self.on_installed()
        if closed and self.on_closed:
            self.on_closed(closed)
        return closed

    def is_running(self, app: str) -> bool:
        return app in self.current

    def quiet(self, app: str) -> bool:
        if app in self.current:
            return False
        if app in self.requested:
            return True
        seen = self.last_running.get(app)
        return seen is None or time.monotonic() - seen >= QUIET_SECONDS

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - a bad instance folder is skipped next time too
                pass

    def stop(self) -> None:
        self._stop.set()
