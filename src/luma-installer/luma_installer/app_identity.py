# SPDX-License-Identifier: Apache-2.0
"""Which application a process is (ADR-052, Permissions 8).

One answer for every part of Luma that has to name an application from a
process: the permission service, the refusal watch, the native-messaging
broker and xdg-desktop-portal (through ``AppPermissions1.Identify``). The
rules are the compositor's (Mutter 0041, ``meta-wayland-client-sandbox.c``),
so a window, a portal request and a refusal from the same process carry the
same ID:

* a process in ``luma_application.process`` is named by its SELinux level,
  which is allocated to one application and kept in its record (the
  installer's ``APP.json`` ``mcs_level``; the store's ``state.json``
  ``categories`` for trees built before that);
* without a level, by the ``app-luma-NAME-PID.scope`` its launcher started it
  in, when exactly one installed application has that scope name;
* a Flatpak by its ``/.flatpak-info``.

Luma applications are named ``org.projectluma.Installed.ID``: the desktop-file
ID of the launcher Luma wrote for them. That is the key of every permission.
"""
from __future__ import annotations

import configparser
import json
import os
import re
from pathlib import Path

PREFIX = "org.projectluma.Installed."
DOMAIN = "luma_application.process"


def safe_id(value: str) -> str:
    """``desktop.safe_id`` (and Mutter 0041's ``luma_safe_id``)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.").lower()
    return cleaned[:80] or "application"


def desktop_app_id(application_id: str) -> str:
    return PREFIX + safe_id(application_id)


def installed_id(app: str) -> str:
    """The installer's application ID from a permission key; "" for other formats."""
    return app[len(PREFIX):] if app.startswith(PREFIX) else ""


def scope_name(application_id: str) -> str:
    """The application's part of the scope name its launcher uses."""
    return "".join(character if character.isalnum() else "_" for character in application_id)


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def installer_levels(root: Path | None = None) -> dict[str, str]:
    """Installer application ID -> its allocated level ("" if none)."""
    root = root or _data_home() / "luma/installer/applications"
    found: dict[str, str] = {}
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return found
    for path in entries:
        if path.suffix != ".json":
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        found[path.stem] = str(record.get("mcs_level") or "") if isinstance(record, dict) else ""
    return found


def store_levels(path: Path | None = None) -> dict[str, str]:
    path = path or _data_home() / "luma/apps/state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    categories = state.get("categories") if isinstance(state, dict) else None
    return {str(key): str(value) for key, value in (categories or {}).items()} if isinstance(categories, dict) else {}


def resolve(level: str | None, scope_leaf: str | None, *, records: dict[str, str] | None = None,
            categories: dict[str, str] | None = None) -> str:
    """Mutter 0041's ``luma_application_id``: the desktop ID, or ""."""
    records = installer_levels() if records is None else records
    categories = store_levels() if categories is None else categories
    if level:
        for application_id, recorded in records.items():
            if recorded == level:
                return desktop_app_id(application_id)
        for application_id, recorded in categories.items():
            if recorded == level:
                return desktop_app_id(application_id)
        return ""
    if not scope_leaf or not scope_leaf.startswith("app-luma-") or not scope_leaf.endswith(".scope"):
        return ""
    name = scope_leaf[len("app-luma-"):-len(".scope")]
    if "-" not in name:
        return ""
    name = name.rsplit("-", 1)[0]
    matches = [key for key in categories if scope_name(key) == name]
    matches += [key for key in records if scope_name(key) == name and key not in categories]
    return desktop_app_id(matches[0]) if len(matches) == 1 else ""


def label_level(label: str) -> str | None:
    """The level of a label in the Luma application domain; None for any other domain."""
    marker = f":{DOMAIN}:"
    if marker not in label:
        return None
    return label.split(marker, 1)[1].strip().strip("\x00")


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            return stream.read()
    except OSError:
        return ""


def scope_leaf(pid: int) -> str:
    for line in _read(f"/proc/{pid}/cgroup").splitlines():
        if line.startswith("0::"):
            return line[3:].rstrip("/").rsplit("/", 1)[-1]
    return ""


def flatpak_id(pid: int) -> str:
    text = _read(f"/proc/{pid}/root/.flatpak-info")
    if not text:
        return ""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return ""
    return parser.get("Application", "name", fallback="")


def identify(pid: int) -> tuple[str, str]:
    """(app ID, kind) for a running process; kind is "luma", "flatpak" or "host"."""
    level = label_level(_read(f"/proc/{pid}/attr/current"))
    if level is not None:
        return resolve(level, None), "luma"
    flatpak = flatpak_id(pid)
    if flatpak:
        return flatpak, "flatpak"
    found = resolve(None, scope_leaf(pid))
    return (found, "luma") if found else ("", "host")
