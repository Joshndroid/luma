# SPDX-License-Identifier: Apache-2.0
"""One permission store for every application format (ADR-052, Permissions).

Everything an application may reach is kept in the desktop's permission store
(``org.freedesktop.impl.portal.PermissionStore``, the one xdg-desktop-portal
and the Shell's layer-shell grants already use), keyed by the application's
ID: ``org.projectluma.Installed.ID`` for store trees, capsules and AppImages
(the ID ``app_identity`` and Mutter 0041 give their windows), the Flatpak ID
for Flatpaks. Portal tables keep their own meaning:

=========================  ================================================
table / id                 value per application
=========================  ================================================
luma / files               ["chooser"] | ["folders"] | ["all"]
luma / folders             folder names it may use ("Music", ...)
luma / folder-history      "+Music@T@install", "-Pictures@T@settings",
                           "?Documents@T@refused" (offered), "!Documents@T@notnow"
luma / network             ["on"] | ["off"]
luma / autostart           ["on"] | ["off"]
luma / devices             granted device kinds ("controller", "hid:1532:0084")
luma / device-requests     what its package's udev rules ask for
luma / uses                what the package asks for (shown on the sheet)
luma / used                "Music@T", "camera@T": last use of each
luma / suggested           ["folders", "Music"]: what Luma chose from its kind
luma / review              ["pending"] after a command-line install
luma / display-over-apps   the Shell's layer-shell rows (Shell 0380, #525)
background / background    ["yes"] | ["no"] (xdg-desktop-portal)
devices / camera, ...      ["yes"] | ["no"] (xdg-desktop-portal)
location / location        xdg-desktop-portal's own row
=========================  ================================================

``org.projectluma.AppPermissions1`` (``AppPermissionsService``) is the one
interface the install sheet, Settings and the Shell's folder offer use. A
change reaches a running application at once: folders through
``permission_views``, Flatpak overrides through ``flatpak_permissions``.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Iterable

from . import app_identity

TABLE = "luma"
LEVELS = ("chooser", "folders", "all")
XDG_FOLDERS = {"DESKTOP": "Desktop", "DOCUMENTS": "Documents", "DOWNLOAD": "Downloads", "MUSIC": "Music",
               "PICTURES": "Pictures", "PUBLICSHARE": "Public", "TEMPLATES": "Templates", "VIDEOS": "Videos"}
#: device kind -> (table, id) in the portal's own tables; the rest live in luma/devices.
PORTAL_DEVICES = {"camera": ("devices", "camera"), "microphone": ("devices", "microphone"),
                  "speakers": ("devices", "speakers"), "location": ("location", "location"),
                  "screen": ("screenshot", "screenshot"), "notifications": ("notifications", "notification")}
DEVICE_KINDS = ("camera", "microphone", "location", "screen", "usb", "controller", "serial", "hid", "lighting")
USES = ("network", "camera", "microphone", "location", "screen", "notifications", "background", "autostart")
RECENT = 5


# -- the store ---------------------------------------------------------------

class MemoryStore:
    """The permission store's interface, in memory (tests and no session bus)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.tables: dict[str, dict[str, dict[str, list[str]]]] = {}
        if path and path.is_file():
            self.tables = json.loads(path.read_text())
        self.listeners: list = []

    def lookup(self, table: str, entry: str) -> dict[str, list[str]]:
        return {app: list(values) for app, values in self.tables.get(table, {}).get(entry, {}).items()}

    def set(self, table: str, entry: str, app: str, values: list[str]) -> None:
        self.tables.setdefault(table, {}).setdefault(entry, {})[app] = list(values)
        self._save()

    def delete(self, table: str, entry: str, app: str) -> None:
        self.tables.get(table, {}).get(entry, {}).pop(app, None)
        self._save()

    def entries(self, table: str) -> list[str]:
        return list(self.tables.get(table, {}))

    def _save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.tables))


class PortalStore:
    """``org.freedesktop.impl.portal.PermissionStore`` on the session bus."""

    NAME = "org.freedesktop.impl.portal.PermissionStore"
    PATH = "/org/freedesktop/impl/portal/PermissionStore"

    def __init__(self, bus=None) -> None:
        from gi.repository import Gio
        self.Gio = Gio
        self.bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def _call(self, method: str, arguments, reply: str | None):
        from gi.repository import GLib
        return self.bus.call_sync(self.NAME, self.PATH, self.NAME, method, arguments,
                                  GLib.VariantType.new(reply) if reply else None,
                                  self.Gio.DBusCallFlags.NONE, 10000, None)

    def lookup(self, table: str, entry: str) -> dict[str, list[str]]:
        from gi.repository import GLib
        try:
            result = self._call("Lookup", GLib.Variant("(ss)", (table, entry)), "(a{sas}v)")
        except GLib.Error:
            return {}
        return {app: list(values) for app, values in result.unpack()[0].items()}

    def set(self, table: str, entry: str, app: str, values: list[str]) -> None:
        from gi.repository import GLib
        self._call("SetPermission", GLib.Variant("(sbssas)", (table, True, entry, app, list(values))), None)

    def delete(self, table: str, entry: str, app: str) -> None:
        from gi.repository import GLib
        try:
            self._call("DeletePermission", GLib.Variant("(sss)", (table, entry, app)), None)
        except GLib.Error:
            pass

    def entries(self, table: str) -> list[str]:
        from gi.repository import GLib
        try:
            return list(self._call("List", GLib.Variant("(s)", (table,)), "(as)").unpack()[0])
        except GLib.Error:
            return []


# -- what an application is ------------------------------------------------------

def _xdg_folder(name: str) -> str:
    """The folder name in the home folder for an XDG folder, as this person has it."""
    try:
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib
        key = {value: key for key, value in XDG_FOLDERS.items()}[name]
        found = GLib.get_user_special_dir(getattr(GLib.UserDirectory, "DIRECTORY_" + key))
        if found:
            path = Path(found).resolve()
            if path.parent == Path.home().resolve():
                return path.name
    except (ImportError, ValueError, AttributeError, KeyError):
        pass
    return name


def suggest(categories: Iterable[str] = (), *, declared: dict[str, object] | None = None,
            name: str = "") -> dict[str, object]:
    """The file level Luma suggests for an application, from what it is.

    ``categories``: its desktop file's Categories. ``declared``: what its
    package declares (Flatpak ``filesystems``). A music player gets Music, a
    photo editor Pictures, an office suite Documents; file managers and sync
    or backup clients get all files; anything else the file picker.
    """
    categories = {category.strip() for category in categories if category.strip()}
    declared = declared or {}
    filesystems = [str(item) for item in declared.get("filesystems") or []]  # type: ignore[union-attr]
    reason = ""
    folders: list[str] = []
    if any(item.split(":")[0] in {"home", "host", "host-os", "~", "/"} or item.split(":")[0].startswith(("/home", "/var/home"))
           for item in filesystems):
        return {"level": "all", "folders": [], "reason": "declared", "suggested": True}
    for item in filesystems:
        base = item.split(":")[0].split("/")[0]
        if base.startswith("xdg-") and base[4:].upper().replace("-", "") in {"DOCUMENTS", "MUSIC", "PICTURES", "VIDEOS",
                                                                             "DOWNLOAD", "DESKTOP", "PUBLICSHARE", "TEMPLATES"}:
            folders.append(XDG_FOLDERS[base[4:].upper().replace("-", "")])
    words = name.lower()
    if "FileManager" in categories or "Backup" in categories or "FileTransfer" in categories and "P2P" not in categories \
            or "Archiving" in categories and "backup" in words:
        return {"level": "all", "folders": [], "reason": "file-manager" if "FileManager" in categories else "sync",
                "suggested": True}
    if "Audio" in categories and ("Player" in categories or "Music" in categories) or "Music" in categories:
        folders.append("Music"); reason = reason or "music"
    if "Video" in categories and ("Player" in categories or "TV" in categories) or "AudioVideoEditing" in categories and "Video" in categories:
        folders.append("Videos"); reason = reason or "video"
    if {"Photography", "RasterGraphics", "VectorGraphics", "2DGraphics", "3DGraphics", "Scanning"} & categories or (
            "Graphics" in categories and "Viewer" in categories):
        folders.append("Pictures"); reason = reason or "pictures"
    if {"Office", "WordProcessor", "Spreadsheet", "Presentation", "Publishing"} & categories:
        folders.append("Documents"); reason = reason or "office"
    if "P2P" in categories or "WebBrowser" in categories:
        folders.append("Downloads"); reason = reason or ("downloads" if "P2P" in categories else "browser")
    folders = list(dict.fromkeys(_xdg_folder(folder) for folder in folders))
    if folders:
        return {"level": "folders", "folders": folders, "reason": reason or "declared", "suggested": True}
    return {"level": "chooser", "folders": [], "reason": "default", "suggested": True}


def plan(*, categories: Iterable[str] = (), declared: dict[str, object] | None = None, name: str = "",
         devices: dict[str, object] | None = None, uses: Iterable[str] = ()) -> dict[str, object]:
    """The install transaction's permission part: what the sheet shows before Install."""
    files = {**suggest(categories, declared=declared, name=name), "chosen": False}
    return {"files": files, "uses": [use for use in uses if use in USES],
            "devices": devices or {"name": "", "icon": "", "kinds": [], "count": 0, "requests": [], "default": False},
            "never": True}


# -- the model ---------------------------------------------------------------------

def _now() -> int:
    return int(time.time())


def _token_time(token: str) -> int:
    parts = token.split("@")
    try:
        return int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return 0


class Permissions:
    """Reads and changes grants. ``apply`` makes a change real for a running application."""

    def __init__(self, store=None, *, home: Path | None = None, apply=None) -> None:
        self.store = store if store is not None else MemoryStore()
        self.home = home or Path.home().resolve()
        self.apply = apply  # callable(app) -> None, after a folder or file-level change
        self.lock = threading.RLock()

    # one value
    def _get(self, entry: str, app: str, table: str = TABLE) -> list[str]:
        return self.store.lookup(table, entry).get(app, [])

    def _set(self, entry: str, app: str, values: list[str], table: str = TABLE) -> None:
        self.store.set(table, entry, app, values)

    def _history(self, app: str, mark: str, folder: str, by: str) -> None:
        history = self._get("folder-history", app)
        history.append(f"{mark}{folder}@{_now()}@{by}")
        self._set("folder-history", app, history[-64:])

    # reading
    def known(self) -> list[str]:
        apps: set[str] = set()
        for entry in ("files", "folders", "network", "suggested", "devices", "device-requests", "used", "review"):
            apps |= set(self.store.lookup(TABLE, entry))
        return sorted(apps)

    def level(self, app: str) -> str:
        value = self._get("files", app)
        return value[0] if value and value[0] in LEVELS else "chooser"

    def named_folders(self, app: str) -> list[str]:
        return [folder for folder in self._get("folders", app) if _plain(folder)]

    def visible_folders(self) -> list[str]:
        from .permission_views import folders
        return folders(self.home)

    def granted_folders(self, app: str) -> set[str]:
        level = self.level(app)
        if level == "all":
            return set(self.visible_folders())
        if level == "folders":
            return set(self.named_folders(app))
        return set()

    def network(self, app: str) -> bool:
        return self._get("network", app) != ["off"]

    def background(self, app: str) -> bool:
        return self._get("background", app, table="background") == ["yes"]

    def autostart(self, app: str) -> bool:
        return self._get("autostart", app) == ["on"]

    def devices(self, app: str) -> list[str]:
        return self._get("devices", app)

    def device_requests(self, app: str) -> list[str]:
        return self._get("device-requests", app)

    def review_pending(self, app: str) -> bool:
        return self._get("review", app) == ["pending"]

    def used(self, app: str) -> dict[str, int]:
        return {token.split("@")[0]: _token_time(token) for token in self._get("used", app)}

    def recent(self, app: str, limit: int = RECENT) -> list[dict[str, object]]:
        """Used recently, refusals included, newest first."""
        rows: list[dict[str, object]] = []
        for what, when in self.used(app).items():
            folder = what in self.visible_folders() or what in XDG_FOLDERS.values()
            rows.append({"what": what, "kind": "folder" if folder else "device", "note": "", "when": when})
        for token in self._get("folder-history", app):
            mark, rest = token[:1], token[1:]
            if mark not in "?!":
                continue
            folder = rest.split("@")[0]
            rows.append({"what": folder, "kind": "folder", "when": _token_time(rest),
                         "note": "refused, offered once" if mark == "?" else "refused, you said Not now"})
        for kind, (table, entry) in PORTAL_DEVICES.items():
            if self._get(entry, app, table=table)[:1] == ["no"]:
                rows.append({"what": kind, "kind": "device", "note": "refused, you said Not now",
                             "when": self.used(app).get(f"asked-{kind}", 0)})
        rows.sort(key=lambda row: int(row["when"]), reverse=True)
        seen: set[tuple[str, str]] = set()
        unique = []
        for row in rows:
            key = (str(row["what"]), str(row["note"]))
            if key not in seen:
                seen.add(key)
                unique.append(row)
        return unique[:limit]

    def device_states(self, app: str) -> list[dict[str, object]]:
        """Devices it asked for or used, with their state."""
        used = self.used(app)
        requested = {token.split(":")[0] for token in self.device_requests(app)}
        granted = {token.split(":")[0] for token in self.devices(app)}
        rows = []
        for kind in DEVICE_KINDS:
            if kind in PORTAL_DEVICES:
                table, entry = PORTAL_DEVICES[kind]
                value = self._get(entry, app, table=table)[:1]
                asked, allowed = bool(value) or f"asked-{kind}" in used, value == ["yes"]
            else:
                asked, allowed = kind in requested, kind in granted
            if not (asked or kind in used):
                continue
            state = "used" if kind in used else ("asked" if asked else "never")
            rows.append({"kind": kind, "granted": allowed, "state": state,
                         "when": used.get(kind) or used.get(f"asked-{kind}", 0)})
        return rows

    def other_devices(self, app: str) -> list[str]:
        shown = {row["kind"] for row in self.device_states(app)}
        return [kind for kind in DEVICE_KINDS if kind not in shown]

    def all_files_apps(self) -> list[str]:
        return sorted(app for app, value in self.store.lookup(TABLE, "files").items() if value[:1] == ["all"])

    def review_apps(self) -> list[str]:
        return sorted(app for app, value in self.store.lookup(TABLE, "review").items() if value == ["pending"])

    def summary(self, app: str) -> dict[str, object]:
        suggested = self._get("suggested", app)
        return {"level": self.level(app), "folders": self.named_folders(app),
                "suggested_level": suggested[0] if suggested else "", "network": self.network(app),
                "background": self.background(app), "autostart": self.autostart(app),
                "review_pending": self.review_pending(app), "uses": self._get("uses", app)}

    def detail(self, app: str) -> dict[str, object]:
        suggested = self._get("suggested", app)
        return {**self.summary(app), "suggested_folders": suggested[1:],
                "devices": self.device_states(app), "other_devices": self.other_devices(app),
                "device_requests": self.device_requests(app), "granted_devices": self.devices(app),
                "recent": self.recent(app), "history": self._get("folder-history", app)}

    # changing
    def install(self, app: str, chosen: dict[str, object], *, by: str = "install", review: bool = False) -> None:
        """Record what an install chose (or the suggestion, for a command-line install)."""
        with self.lock:
            files = dict(chosen.get("files") or {})  # type: ignore[arg-type]
            level = str(files.get("level") or "chooser")
            folders = [str(folder) for folder in files.get("folders") or [] if _plain(str(folder))]  # type: ignore[union-attr]
            existing = self._get("files", app)
            self._set("suggested", app, [str(files.get("suggested_level") or level), *folders])
            if not existing:
                self._set("files", app, [level if level in LEVELS else "chooser"])
                self._set("folders", app, folders)
                for folder in folders:
                    self._history(app, "+", folder, by)
            self._set("uses", app, [use for use in chosen.get("uses") or [] if use in USES])  # type: ignore[union-attr]
            devices = dict(chosen.get("devices") or {})  # type: ignore[arg-type]
            requests = list(dict.fromkeys(str(token) for token in devices.get("requests") or []))  # type: ignore[union-attr]
            if requests:
                self._set("device-requests", app, requests)
                if devices.get("granted", devices.get("default", True)):
                    self._set("devices", app, requests)
            if not self._get("network", app):
                self._set("network", app, ["on"])
            if review:
                self._set("review", app, ["pending"])
        self._applied(app)

    def set_level(self, app: str, level: str, *, by: str = "settings") -> None:
        if level not in LEVELS:
            raise ValueError(f"unknown file level {level!r}")
        with self.lock:
            before = self.level(app)
            self._set("files", app, [level])
            if before != level:
                self._history(app, "=", level, by)
        self._applied(app)

    def set_folder(self, app: str, folder: str, allowed: bool, *, by: str = "settings") -> None:
        if not _plain(folder):
            raise ValueError(f"{folder!r} cannot be given to an application")
        with self.lock:
            level = self.level(app)
            named = self.named_folders(app)
            if allowed:
                if level == "all":
                    return
                if folder not in named:
                    named.append(folder)
                self._set("folders", app, named)
                if level == "chooser":
                    self._set("files", app, ["folders"])
                self._history(app, "+", folder, by)
            else:
                if level == "all":
                    named = [name for name in self.visible_folders() if name != folder]
                    self._set("files", app, ["folders"])
                named = [name for name in named if name != folder]
                self._set("folders", app, named)
                self._history(app, "-", folder, by)
        self._applied(app)

    def refused(self, app: str, folder: str) -> bool:
        """A refusal was seen. True exactly once per application and folder: offer it then."""
        if not app or not _plain(folder):
            return False
        with self.lock:
            if folder in self.granted_folders(app):
                return False
            for token in self._get("folder-history", app):
                if token[:1] in "?!" and token[1:].split("@")[0] == folder:
                    return False
            self._history(app, "?", folder, "refused")
        return True

    def answer_offer(self, app: str, folder: str, allow: bool) -> None:
        if allow:
            self.set_folder(app, folder, True, by="offer")
        else:
            with self.lock:
                self._history(app, "!", folder, "notnow")

    def record_use(self, app: str, what: str) -> None:
        with self.lock:
            used = [token for token in self._get("used", app) if token.split("@")[0] != what]
            used.append(f"{what}@{_now()}")
            self._set("used", app, used[-48:])

    def set_network(self, app: str, allowed: bool) -> None:
        self._set("network", app, ["on" if allowed else "off"])

    def set_background(self, app: str, allowed: bool) -> None:
        self._set("background", app, ["yes" if allowed else "no"], table="background")

    def set_autostart(self, app: str, allowed: bool) -> None:
        self._set("autostart", app, ["on" if allowed else "off"])

    def set_device(self, app: str, kind: str, allowed: bool) -> None:
        if kind in PORTAL_DEVICES:
            table, entry = PORTAL_DEVICES[kind]
            self._set(entry, app, ["yes" if allowed else "no"], table=table)
            return
        with self.lock:
            requested = [token for token in self.device_requests(app) if token.split(":")[0] == kind] or [kind]
            granted = [token for token in self.devices(app) if token.split(":")[0] != kind]
            if allowed:
                granted.extend(requested)
            self._set("devices", app, granted)

    def set_device_requests(self, app: str, allowed: bool) -> None:
        self._set("devices", app, self.device_requests(app) if allowed else [])

    def finish_review(self, app: str) -> None:
        self.store.delete(TABLE, "review", app)

    def forget(self, app: str) -> None:
        """An application was removed: its rows go with it."""
        for entry in ("files", "folders", "folder-history", "network", "autostart", "devices", "device-requests",
                      "uses", "used", "suggested", "review"):
            self.store.delete(TABLE, entry, app)
        self.store.delete("background", "background", app)
        for table, entry in PORTAL_DEVICES.values():
            self.store.delete(table, entry, app)

    def _applied(self, app: str) -> None:
        if self.apply is not None:
            try:
                self.apply(app)
            except Exception as error:  # noqa: BLE001 - a running app keeps its view; the grant is stored
                print(f"luma-app-permissions: {app}: {error}", file=sys.stderr)


def _plain(folder: str) -> bool:
    """A top-level folder name that may be given to an application."""
    return bool(folder) and "/" not in folder and not folder.startswith(".") and folder not in {"..", "."}


# -- applications on this device ------------------------------------------------------

def installed_records() -> dict[str, dict[str, object]]:
    """Permission key -> installer record, for every application the installer knows."""
    from .desktop import records_root
    found: dict[str, dict[str, object]] = {}
    root = records_root()
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(record, dict):
            continue
        key = str(record.get("flatpak_id")) if record.get("format") == "flatpak" and record.get("flatpak_id") \
            else app_identity.desktop_app_id(path.stem)
        found[key] = {**record, "_id": path.stem}
    return found


_FLATPAKS: dict[str, object] = {"at": 0.0, "apps": {}}


def flatpak_apps() -> dict[str, dict[str, object]]:
    """Every installed Flatpak application, however it was installed (Depot, Flathub, the CLI)."""
    import subprocess
    if time.monotonic() - float(_FLATPAKS["at"]) < 30:  # type: ignore[arg-type]
        return dict(_FLATPAKS["apps"])  # type: ignore[arg-type]
    found: dict[str, dict[str, object]] = {}
    try:
        listed = subprocess.run(["flatpak", "list", "--app", "--columns=application,name,origin,version"],
                                capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        listed = ""
    for line in listed.splitlines():
        parts = (line.split("\t") + ["", "", "", ""])[:4]
        if parts[0]:
            found[parts[0]] = {"format": "flatpak", "flatpak_id": parts[0], "name": parts[1] or parts[0],
                               "origin": parts[2], "version": parts[3]}
    _FLATPAKS.update({"at": time.monotonic(), "apps": found})
    return found


def origin(record: dict[str, object]) -> tuple[str, str]:
    """(format, where from) for Settings' origin line."""
    kind = str(record.get("format") or "")
    store = dict(record.get("store") or {})  # type: ignore[arg-type]
    userland = str(store.get("userland") or "")
    pretty = userland.replace("-", " ").title().replace("Lts", "LTS") if userland else ""
    if kind == "flatpak":
        remote = str(record.get("origin") or record.get("remote") or "")
        return "flatpak", ("Depot" if remote in {"luma", "depot", "luma-depot"} else
                           "Flathub" if remote == "flathub" else remote or "Flatpak")
    if kind == "deb":
        return "deb", pretty or "Debian"
    if kind == "rpm":
        return "rpm", pretty or "Fedora"
    if kind == "appimage":
        return "appimage", "the developer"
    if kind == "portable":
        return "archive", "the developer"
    return kind or "unknown", ""


def data_folder(app: str, record: dict[str, object] | None) -> Path | None:
    if record and record.get("data_home"):
        return Path(str(record["data_home"]))
    if record and record.get("format") == "flatpak":
        return Path.home() / ".var/app" / str(record.get("flatpak_id") or app)
    if record and record.get("sha256"):
        return Path.home() / ".local/share/luma/installer/data" / str(record["sha256"])
    if not app.startswith(app_identity.PREFIX):
        return Path.home() / ".var/app" / app
    return None


def stray_documents(data: Path | None, limit: int = 50) -> list[str]:
    """Visible files an application left in its own data folder (ADR-052, Permissions 4)."""
    found: list[str] = []
    if data is None or not data.is_dir():
        return found
    for directory, folders, files in os.walk(data):
        folders[:] = sorted(name for name in folders if not name.startswith("."))
        for name in sorted(files):
            if not name.startswith(".") and not name.endswith((".lock", ".pid", ".sock")):
                found.append(str(Path(directory, name).relative_to(data)))
                if len(found) >= limit:
                    return found
    return found


def move_stray_documents(data: Path | None, name: str, destination: Path | None = None) -> list[str]:
    moved: list[str] = []
    destination = destination or Path.home() / _xdg_folder("Documents") / name
    for relative in stray_documents(data, limit=500):
        assert data is not None
        source = data / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        stem, suffix, number = target.stem, target.suffix, 2
        while target.exists():
            target = target.with_name(f"{stem} ({number}){suffix}")
            number += 1
        shutil.move(str(source), str(target))
        moved.append(str(target))
    return moved


def folder_bytes(path: Path | None) -> int:
    total = 0
    if path is None or not path.exists():
        return total
    for directory, _folders, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_blocks * 512
            except OSError:
                continue
    return total


# -- applying a change to what runs -------------------------------------------------

def applier(model: "Permissions"):
    """What a stored change does to a running application, by format."""
    def apply(app: str) -> None:
        from . import flatpak_permissions, permission_views
        records = installed_records()
        record = records.get(app)
        if record is not None and record.get("format") == "flatpak" or (record is None and not app.startswith(app_identity.PREFIX)):
            flatpak_permissions.apply(app, model.level(app), model.named_folders(app))
            return
        from . import sandbox_permissions
        permission_views.apply_running(app, sandbox_permissions.effective_folders(model, app))
    return apply


# -- D-Bus ------------------------------------------------------------------------------

BUS_NAME = "org.projectluma.AppPermissions1"
FDO = "org.freedesktop.DBus"
OBJECT_PATH = "/org/projectluma/AppPermissions"
INTERFACE_XML = """
<node>
  <interface name="org.projectluma.AppPermissions1">
    <property name="Apps" type="a{sa{sv}}" access="read"/>
    <property name="AllFilesApps" type="as" access="read"/>
    <property name="ReviewPending" type="as" access="read"/>
    <method name="GetApp"><arg type="s" name="app" direction="in"/><arg type="a{sv}" name="detail" direction="out"/></method>
    <method name="Plan"><arg type="a{sv}" name="package" direction="in"/><arg type="a{sv}" name="plan" direction="out"/></method>
    <method name="SetFileLevel"><arg type="s" name="app" direction="in"/><arg type="s" name="level" direction="in"/></method>
    <method name="SetFolder"><arg type="s" name="app" direction="in"/><arg type="s" name="folder" direction="in"/><arg type="b" name="allowed" direction="in"/></method>
    <method name="AnswerOffer"><arg type="s" name="app" direction="in"/><arg type="s" name="folder" direction="in"/><arg type="b" name="allow" direction="in"/></method>
    <method name="SetNetwork"><arg type="s" name="app" direction="in"/><arg type="b" name="allowed" direction="in"/></method>
    <method name="SetBackground"><arg type="s" name="app" direction="in"/><arg type="b" name="allowed" direction="in"/></method>
    <method name="SetAutostart"><arg type="s" name="app" direction="in"/><arg type="b" name="allowed" direction="in"/></method>
    <method name="SetDevice"><arg type="s" name="app" direction="in"/><arg type="s" name="kind" direction="in"/><arg type="b" name="allowed" direction="in"/></method>
    <method name="SetDeviceRequests"><arg type="s" name="app" direction="in"/><arg type="b" name="allowed" direction="in"/></method>
    <method name="FinishReview"><arg type="s" name="app" direction="in"/></method>
    <method name="RequestReview"><arg type="s" name="app" direction="in"/></method>
    <method name="StrayDocuments"><arg type="s" name="app" direction="in"/><arg type="as" name="files" direction="out"/></method>
    <method name="MoveStrayDocuments"><arg type="s" name="app" direction="in"/><arg type="as" name="moved" direction="out"/></method>
    <method name="RecordUse"><arg type="s" name="app" direction="in"/><arg type="s" name="what" direction="in"/></method>
    <method name="Identify"><arg type="u" name="pid" direction="in"/><arg type="s" name="app" direction="out"/><arg type="s" name="kind" direction="out"/></method>
    <signal name="Changed"><arg type="s" name="app"/></signal>
    <signal name="Refused"><arg type="s" name="app"/><arg type="s" name="folder"/></signal>
    <signal name="Used"><arg type="s" name="app"/><arg type="s" name="what"/></signal>
    <signal name="ReviewRequested"><arg type="s" name="app"/><arg type="s" name="name"/></signal>
  </interface>
</node>
"""

_CHANGES = {"SetFileLevel", "SetFolder", "AnswerOffer", "SetNetwork", "SetBackground", "SetAutostart", "SetDevice",
            "SetDeviceRequests", "FinishReview", "RequestReview", "MoveStrayDocuments", "RecordUse"}


def _variant_dict(values: dict[str, object]):
    from gi.repository import GLib

    def wrap(value):
        if isinstance(value, bool):
            return GLib.Variant("b", value)
        if isinstance(value, int):
            return GLib.Variant("x", value)
        if isinstance(value, str):
            return GLib.Variant("s", value)
        if isinstance(value, dict):
            return GLib.Variant("a{sv}", {str(key): wrap(item) for key, item in value.items()})
        if isinstance(value, (list, tuple)):
            if all(isinstance(item, str) for item in value):
                return GLib.Variant("as", list(value))
            return GLib.Variant("av", [wrap(item) for item in value])
        return GLib.Variant("s", str(value))
    return {str(key): wrap(value) for key, value in values.items()}


class AppPermissionsService:
    """``org.projectluma.AppPermissions1`` on the session bus."""

    def __init__(self, model: Permissions, connection) -> None:
        from gi.repository import Gio
        self.model, self.connection = model, connection
        node = Gio.DBusNodeInfo.new_for_xml(INTERFACE_XML)
        self.interface = node.interfaces[0]
        self.registration = connection.register_object(OBJECT_PATH, self.interface, self._call, self._property, None)

    # the UI's view of every application
    def apps(self) -> dict[str, dict[str, object]]:
        records = {**flatpak_apps(), **installed_records()}
        listed: dict[str, dict[str, object]] = {}
        for app in sorted(set(records) | set(self.model.known())):
            record = records.get(app, {})
            kind, source = origin(record) if record else ("flatpak" if not app.startswith(app_identity.PREFIX) else "unknown", "")
            listed[app] = {"name": str(record.get("name") or app), "format": kind, "origin": source,
                           "icon": str(record.get("icon") or ""), **self.model.summary(app)}
        return listed

    def detail(self, app: str) -> dict[str, object]:
        record = {**flatpak_apps(), **installed_records()}.get(app, {})
        data = data_folder(app, record)
        kind, source = origin(record) if record else ("", "")
        declared = ""
        if kind == "flatpak":
            from . import flatpak_permissions
            declared = flatpak_permissions.level(flatpak_permissions.declared_filesystems(app))[0]
        return {**self.apps().get(app, {"name": app}), **self.model.detail(app), "format": kind, "origin": source,
                # A Flatpak cannot be narrowed below what its package declares (flatpak_permissions).
                "declared_level": declared,
                "version": str(record.get("version") or ""), "data_folder": str(data or ""),
                "data_bytes": folder_bytes(data), "stray_documents": stray_documents(data)}

    def _property(self, _connection, _sender, _path, _interface, name):
        from gi.repository import GLib
        if name == "Apps":
            return GLib.Variant("a{sa{sv}}", {app: _variant_dict(values) for app, values in self.apps().items()})
        if name == "AllFilesApps":
            return GLib.Variant("as", self.model.all_files_apps())
        if name == "ReviewPending":
            return GLib.Variant("as", self.model.review_apps())
        return None

    def _caller_trusted(self, sender: str) -> bool:
        """Changes come from the desktop, never from an application's sandbox."""
        from gi.repository import GLib
        try:
            pid = self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                                            "GetConnectionUnixProcessID", GLib.Variant("(s)", (sender,)),
                                            GLib.VariantType.new("(u)"), 0, 5000, None).unpack()[0]
        except GLib.Error:
            return False
        return app_identity.identify(int(pid))[1] == "host"

    def _call(self, _connection, sender, _path, _interface, method, parameters, invocation):
        from gi.repository import GLib
        arguments = parameters.unpack()
        try:
            if method in _CHANGES and not self._caller_trusted(sender):
                invocation.return_dbus_error("org.projectluma.AppPermissions1.Error.NotAllowed",
                                             "Applications cannot change permissions.")
                return
            app = str(arguments[0]) if arguments and isinstance(arguments[0], str) else ""
            result = None
            if method == "GetApp":
                result = GLib.Variant("(a{sv})", (_variant_dict(self.detail(app)),))
            elif method == "Plan":
                package = {key: value for key, value in arguments[0].items()}
                result = GLib.Variant("(a{sv})", (_variant_dict(plan(
                    categories=package.get("categories") or [], declared={"filesystems": package.get("filesystems") or []},
                    name=str(package.get("name") or ""), uses=package.get("uses") or [])),))
            elif method == "SetFileLevel":
                self._narrowable(app, arguments[1])
                self.model.set_level(app, arguments[1])
            elif method == "SetFolder":
                self.model.set_folder(app, arguments[1], bool(arguments[2]))
            elif method == "AnswerOffer":
                self.model.answer_offer(app, arguments[1], bool(arguments[2]))
            elif method == "SetNetwork":
                self.model.set_network(app, bool(arguments[1]))
                self._flatpak_network(app, bool(arguments[1]))
            elif method == "SetBackground":
                self.model.set_background(app, bool(arguments[1]))
            elif method == "SetAutostart":
                self.model.set_autostart(app, bool(arguments[1]))
                autostart(app, bool(arguments[1]))
            elif method == "SetDevice":
                self.model.set_device(app, arguments[1], bool(arguments[2]))
                self._device_access()
            elif method == "SetDeviceRequests":
                self.model.set_device_requests(app, bool(arguments[1]))
                self._device_access()
            elif method == "FinishReview":
                self.model.finish_review(app)
            elif method == "RequestReview":
                self.model.store.set(TABLE, "review", app, ["pending"])
                self.review(app, str(self.apps().get(app, {}).get("name") or app))
            elif method == "StrayDocuments":
                result = GLib.Variant("(as)", (stray_documents(data_folder(app, installed_records().get(app))),))
            elif method == "MoveStrayDocuments":
                record = installed_records().get(app) or {}
                moved = move_stray_documents(data_folder(app, record), str(record.get("name") or app))
                result = GLib.Variant("(as)", (moved,))
            elif method == "RecordUse":
                self.model.record_use(app, arguments[1])
                self.emit("Used", GLib.Variant("(ss)", (app, arguments[1])))
            elif method == "Identify":
                found, kind = app_identity.identify(int(arguments[0]))
                result = GLib.Variant("(ss)", (found, kind))
            if method in _CHANGES:
                self.changed(app)
            invocation.return_value(result)
        except (ValueError, OSError) as error:
            invocation.return_dbus_error("org.projectluma.AppPermissions1.Error.Failed", str(error))

    def _narrowable(self, app: str, level: str) -> None:
        if app.startswith(app_identity.PREFIX):
            return
        from . import flatpak_permissions
        declared = flatpak_permissions.level(flatpak_permissions.declared_filesystems(app))[0]
        if LEVELS.index(level if level in LEVELS else "chooser") < LEVELS.index(declared):
            raise ValueError(f"{app} needs {declared} to keep its files; it is narrowed by excluding keys and passwords")

    def _device_access(self) -> None:
        device_access(self.model)

    def _flatpak_network(self, app: str, allowed: bool) -> None:
        record = installed_records().get(app)
        if record and record.get("format") == "flatpak":
            from .valet_permissions import apply_flatpak_choices
            apply_flatpak_choices(record, {"network": allowed})

    def emit(self, signal: str, parameters) -> None:
        self.connection.emit_signal(None, OBJECT_PATH, BUS_NAME, signal, parameters)

    def changed(self, app: str) -> None:
        from gi.repository import GLib
        self.emit("Changed", GLib.Variant("(s)", (app,)))
        self.connection.emit_signal(None, OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
                                    GLib.Variant("(sa{sv}as)", (BUS_NAME, {}, ["Apps", "AllFilesApps", "ReviewPending"])))

    def refused(self, app: str, folder: str) -> None:
        from gi.repository import GLib
        if self.model.refused(app, folder):
            self.emit("Refused", GLib.Variant("(ss)", (app, folder)))
            self.changed(app)

    def review(self, app: str, name: str) -> None:
        from gi.repository import GLib
        self.emit("ReviewRequested", GLib.Variant("(ss)", (app, name)))


def device_access(model: "Permissions", wait: bool = False) -> None:
    """The system's udev rules for every granted device request, through polkit."""
    import subprocess
    tokens = sorted({token for values in model.store.lookup(TABLE, "devices").values() for token in values
                     if token.split(":")[0] not in PORTAL_DEVICES})
    process = subprocess.Popen(["pkexec", "/usr/libexec/luma-installer-system", "device-access", *tokens],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if wait:
        process.wait(timeout=120)


def autostart(app: str, enabled: bool) -> None:
    """Start at sign-in: the application's launcher in ~/.config/autostart, or not."""
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart"
    target = config / f"{app}.desktop"
    if not enabled:
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return
    from .desktop import applications_root
    for root in (applications_root(), Path.home() / ".local/share/flatpak/exports/share/applications",
                 Path("/var/lib/flatpak/exports/share/applications")):
        source = root / f"{app}.desktop"
        if source.is_file():
            config.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8") + "\nX-Luma-Autostart=true\n", encoding="utf-8")
            return


# -- refusals from the system watch -----------------------------------------------------

def refusal_socket() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "luma-permissions" / "refusals.sock"


def listen_for_refusals(service: AppPermissionsService) -> threading.Thread:
    """Refusals arrive from ``luma-refusal-watch`` (root) on a socket only it may write to."""
    from gi.repository import GLib
    path = refusal_socket()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(16)

    def serve() -> None:
        while True:
            connection, _ = server.accept()
            with connection:
                try:
                    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                    _pid, uid, _gid = __import__("struct").unpack("3i", credentials)
                    if uid != 0:
                        continue
                    data = b""
                    connection.settimeout(2)
                    while chunk := connection.recv(4096):
                        data += chunk
                        if len(data) > 65536:
                            break
                except OSError:
                    continue
            for line in data.decode("utf-8", "replace").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                app = app_identity.resolve(str(event.get("level") or ""), None) if event.get("level") else ""
                folder = str(event.get("folder") or "")
                if app and folder:
                    GLib.idle_add(lambda a=app, f=folder: service.refused(a, f) and False)

    thread = threading.Thread(target=serve, name="refusals", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
    loop = GLib.MainLoop()
    state: dict[str, object] = {}

    def acquired(connection, _name):
        model = Permissions(PortalStore(connection))
        model.apply = applier(model)
        service = AppPermissionsService(model, connection)
        state["service"] = service
        try:
            listen_for_refusals(service)
        except OSError as error:
            print(f"luma-app-permissions: refusals are not reported: {error}", file=sys.stderr)
        try:
            from . import native_messaging
            state["broker"] = native_messaging.Broker(model)
            state["broker"].start()
        except Exception as error:  # noqa: BLE001 - the broker is optional to the store
            print(f"luma-app-permissions: native messaging broker: {error}", file=sys.stderr)

    Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.NONE, acquired, None,
                     lambda *_: loop.quit())
    loop.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
