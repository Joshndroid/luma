# SPDX-License-Identifier: Apache-2.0
"""What one start of an application gets from its permissions.

Called by every Luma launcher (store trees, capsules, AppImages) right before
the sandbox is built: the folder view (``permission_views``), whether it has a
network, and the device nodes its granted device requests name. An
application installed before permissions existed is given what it had then
(the person's usual folders, network), so nothing works worse after the
change; the record says so and Settings can narrow it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, field

from . import app_identity, device_requests, permission_views

# What every capsule could use before step 3 (launcher._person_folders).
EARLIER_FOLDERS = ("Desktop", "Documents", "Downloads", "Music", "Pictures", "Public", "Templates", "Videos")
# Installed by xdg-desktop-portal 1.22.1-1.luma.5 and later (patch 0005): the
# portal names Luma's applications and hands them chosen files through the
# document portal. Until then a file picked in the file picker reaches the
# application as its real path, so the folders it could always use stay open.
PORTAL_IDENTITY = Path("/usr/share/xdg-desktop-portal/luma-names-applications")


def portal_names_applications() -> bool:
    if PORTAL_IDENTITY.exists():
        return True
    try:
        release = subprocess.run(["rpm", "-q", "--qf", "%{RELEASE}", "xdg-desktop-portal"], capture_output=True,
                                 text=True, timeout=10).stdout
        return int(release.split(".luma.", 1)[1].split(".", 1)[0]) >= 5
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return False


def effective_folders(permissions, key: str) -> set[str]:
    """The folders a start of ``key`` gets: its grants, and until the portal can name it, today's folders."""
    granted = permissions.granted_folders(key)
    if not portal_names_applications():
        granted |= {name for name in EARLIER_FOLDERS if name in permissions.visible_folders()}
    return granted


@dataclass
class Access:
    app: str
    network: bool = True
    binds: list[tuple[str, str]] | None = None
    devices: list[str] = field(default_factory=list)
    gate: list[str] = field(default_factory=list)
    nsenter: list[str] = field(default_factory=list)
    view: permission_views.View | None = None

    def close(self) -> None:
        if self.view is not None:
            self.view.close()
            self.view = None


def model():
    from . import app_permissions
    try:
        import gi
        gi.require_version("Gio", "2.0")
        store = app_permissions.PortalStore()
    except Exception:  # noqa: BLE001 - no session bus: the earlier behaviour, below
        return None
    try:
        # The permission service listens for this start's refusals and applies live changes.
        from gi.repository import GLib
        store.bus.call(app_permissions.FDO, "/org/freedesktop/DBus", app_permissions.FDO, "StartServiceByName",
                       GLib.Variant("(su)", (app_permissions.BUS_NAME, 0)), None, 0, 5000, None, None, None)
    except Exception:  # noqa: BLE001 - permissions are still read from the store
        pass
    return app_permissions.Permissions(store)


def seed_earlier(permissions, app: str) -> None:
    """An application from before permissions: the folders it could use then."""
    if permissions.store.lookup("luma", "files").get(app):
        return
    present = [name for name in EARLIER_FOLDERS if name in permissions.visible_folders()]
    permissions.install(app, {"files": {"level": "folders", "folders": present, "suggested_level": "folders"}},
                        by="earlier")


def for_start(application_id: str, record: dict[str, object], *, backend: str = "bwrap",
              instance: str = "") -> Access:
    key = app_identity.desktop_app_id(str(record.get("application_id") or application_id))
    permissions = model()
    if permissions is None:
        return Access(key)
    try:
        seed_earlier(permissions, key)
        granted = effective_folders(permissions, key)
        network = permissions.network(key)
        requests = [device_requests.parse_token(token) for token in permissions.devices(key)]
    except Exception as error:  # noqa: BLE001 - the permission store is down: start as before
        print(f"luma: permissions unavailable, starting with earlier folders: {error}", file=sys.stderr)
        return Access(key)
    access = Access(key, network=network, devices=device_requests.nodes(requests))
    view = permission_views.View(key, instance or str(os.getpid()), granted, backend=backend)
    if view.prepare():
        access.binds = view.binds()
        if backend == "bwrap":
            access.gate = view.gate_prefix()
            access.nsenter = view.nsenter()
        view.record()
        access.view = view
    else:
        access.binds = view.static_binds()
    return access
