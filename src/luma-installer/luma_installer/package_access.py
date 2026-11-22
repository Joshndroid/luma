# SPDX-License-Identifier: Apache-2.0
"""What an install sets up for an application's permissions.

``plan_for_report`` is the permission part of the install transaction, the
same for every route (a file, Depot, Flathub, the command line): the file
level suggested from what the application is, the folders by name, what the
package asks to use, and its udev rules as one device request. It is read
from the package itself before anything is installed, so the sheet can show
it. ``record_install`` stores the chosen (or, without a sheet, the suggested)
values under the application's ID once it is installed, applies a Flatpak's
translation, and registers its native-messaging hosts.
"""
from __future__ import annotations

import configparser
import io
import json
import subprocess
import tarfile
from pathlib import Path

from . import app_identity, app_permissions, device_requests

LIMIT = 400 * 1024 * 1024


def _desktop_categories(text: str) -> tuple[list[str], str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return [], ""
    entry = "Desktop Entry"
    if not parser.has_section(entry) or parser.get(entry, "NoDisplay", fallback="false").lower() == "true":
        return [], ""
    return [item for item in parser.get(entry, "Categories", fallback="").split(";") if item], parser.get(entry, "Name", fallback="")


def _wanted(name: str) -> str:
    """'desktop' or 'rules' for a member of a package, '' otherwise."""
    name = name.lstrip("./")
    if name.startswith("usr/share/applications/") and name.endswith(".desktop"):
        return "desktop"
    if name.endswith(".rules") and any(name.startswith(folder + "/") for folder in device_requests.RULE_FOLDERS):
        return "rules"
    return ""


def _from_deb(path: Path) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {"desktop": {}, "rules": {}}
    with open(path, "rb") as stream:
        if stream.read(8) != b"!<arch>\n":
            return found
        while True:
            header = stream.read(60)
            if len(header) < 60:
                return found
            name, size = header[:16].decode().strip().rstrip("/"), int(header[48:58].decode().strip() or 0)
            if name.startswith("data.tar"):
                data = stream.read(size)
                break
            stream.seek(size + (size % 2), 1)
    if name.endswith(".zst"):
        try:
            from compression import zstd  # Python 3.14
            data = zstd.decompress(data)
        except ImportError:
            return found
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive:
            kind = _wanted(member.name)
            if kind and member.isfile() and member.size < 512 * 1024:
                handle = archive.extractfile(member)
                if handle is not None:
                    found[kind][member.name.lstrip("./")] = handle.read().decode("utf-8", "replace")
    return found


def _from_rpm(path: Path) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {"desktop": {}, "rules": {}}
    try:
        payload = subprocess.run(["rpm2cpio", str(path)], capture_output=True, timeout=120).stdout
    except (OSError, subprocess.TimeoutExpired):
        return found
    offset = 0
    while offset + 110 <= len(payload) and payload[offset:offset + 6] in (b"070701", b"070702"):
        fields = [int(payload[offset + 6 + index * 8:offset + 14 + index * 8], 16) for index in range(13)]
        size, name_size = fields[6], fields[11]
        name_start = offset + 110
        name = payload[name_start:name_start + name_size - 1].decode("utf-8", "replace")
        data_start = (name_start + name_size + 3) & ~3
        if name == "TRAILER!!!":
            break
        kind = _wanted(name)
        if kind and size < 512 * 1024:
            found[kind][name.lstrip("./")] = payload[data_start:data_start + size].decode("utf-8", "replace")
        offset = (data_start + size + 3) & ~3
    return found


def _from_tree(root: Path) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {"desktop": {}, "rules": {}}
    applications = root / "usr/share/applications"
    for path in sorted(applications.glob("*.desktop")) if applications.is_dir() else []:
        try:
            found["desktop"][str(path.relative_to(root))] = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return found


def plan_from_files(found: dict[str, dict[str, str]], *, name: str = "", declared: dict[str, object] | None = None,
                    uses: list[str] | None = None, devices: device_requests.Summary | None = None) -> dict[str, object]:
    categories: list[str] = []
    for text in found.get("desktop", {}).values():
        found_categories, found_name = _desktop_categories(text)
        if found_categories:
            categories, name = found_categories, name or found_name
            break
    summary = devices if devices is not None else device_requests.read_rules(found.get("rules", {}))
    return app_permissions.plan(categories=categories, declared=declared, name=name,
                                devices=summary.as_dict(), uses=uses or [])


def flatpak_uses(details: dict[str, str]) -> list[str]:
    shared = {item.strip() for item in details.get("Context · shared", "").split(";")}
    devices = {item.strip() for item in details.get("Context · devices", "").split(";")}
    talk = details.get("Session Bus Policy · org.freedesktop.Notifications", "")
    uses = ["network"] if "network" in shared else []
    if "all" in devices:
        uses.extend(("camera", "microphone"))
    if talk:
        uses.append("notifications")
    return uses


def plan_for_report(report) -> dict[str, object]:
    """The install transaction's permission part for a reviewed package."""
    kind, path = report.kind, Path(report.path)
    try:
        if kind == "flatpak":
            filesystems = [item for item in report.details.get("Context · filesystems", "").split(";") if item]
            return app_permissions.plan(declared={"filesystems": filesystems}, name=report.title,
                                        uses=flatpak_uses(report.details))
        if path.stat().st_size > LIMIT:
            return app_permissions.plan(name=report.title)
        if kind == "deb":
            return plan_from_files(_from_deb(path), name=report.title)
        if kind == "rpm":
            return plan_from_files(_from_rpm(path), name=report.title)
    except (OSError, tarfile.TarError, ValueError, EOFError):
        pass
    return app_permissions.plan(name=report.title)


def plan_for_tree(root: Path, *, name: str = "") -> dict[str, object]:
    """For a tree in the store (and an AppImage's payload): its launchers and rules as installed."""
    return plan_from_files(_from_tree(root), name=name, devices=device_requests.from_tree(root))


def record_install(app: str, chosen: dict[str, object], *, review: bool = False, tree: Path | None = None,
                   application_id: str = "", flatpak: bool = False, permissions=None) -> None:
    """Store the install's permissions and make them real."""
    permissions = permissions or app_permissions.Permissions(app_permissions.PortalStore())
    permissions.install(app, chosen, review=review)
    if permissions.devices(app):
        try:
            app_permissions.device_access(permissions)
        except OSError:
            pass  # the request stays granted in the store; Settings can apply it again
    if flatpak:
        from . import flatpak_permissions
        flatpak_permissions.apply(app, permissions.level(app), permissions.named_folders(app))
    if tree is not None and application_id:
        from . import native_messaging
        manifests = native_messaging.manifests_in_tree(tree)
        if manifests:
            native_messaging.register_hosts(app, application_id, manifests)
    if review:
        _notify_review(app)


def _notify_review(app: str) -> None:
    """A command-line install: ask the permission service to raise its review."""
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(app_permissions.BUS_NAME, app_permissions.OBJECT_PATH, app_permissions.BUS_NAME, "RequestReview",
                      GLib.Variant("(s)", (app,)), None, Gio.DBusCallFlags.NONE, 5000, None)
    except Exception:  # noqa: BLE001 - the review stays pending in the store and shows next time
        pass


def forget(app: str) -> None:
    try:
        app_permissions.Permissions(app_permissions.PortalStore()).forget(app)
    except Exception:  # noqa: BLE001
        pass
    from . import flatpak_permissions, native_messaging
    flatpak_permissions.forget(app)
    native_messaging.forget_app(app)


def key_for_record(application_id: str, record: dict[str, object]) -> str:
    if record.get("format") == "flatpak" and record.get("flatpak_id"):
        return str(record["flatpak_id"])
    return app_identity.desktop_app_id(application_id)


def as_json(plan: dict[str, object]) -> str:
    return json.dumps(plan, sort_keys=True)
