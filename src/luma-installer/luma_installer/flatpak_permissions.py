# SPDX-License-Identifier: Apache-2.0
"""A Flatpak's file access as a Luma file level (ADR-052, "What each format becomes").

A bundle's ``home`` or ``host`` request is the level "all files"; ``xdg-music``
and the like are "these folders"; anything else is the file picker. Luma
narrows a Flatpak only by excluding the protected paths (keys, keyrings,
browser profiles, other applications' data, sign-in scripts), never by taking
``home`` away: Flatpak puts an empty temporary folder where access was removed,
and the application's saves would vanish there. ``host`` becomes ``home`` plus
the removable-drive roots, because ``host`` also shows the whole system under
``/run/host/root``, where an exclusion cannot reach.

Widening (a folder, all files) and Luma's own exclusions are user overrides,
made with ``flatpak override --user`` after the person's previous override is
backed up once (``valet_permissions``). A change rebuilds the override from
that backup plus Luma's current choices, so removing a folder Luma added
removes exactly that line and nothing is ever hidden with an empty folder.
"""
from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
from pathlib import Path

from .errors import InstallerError
from .permission_views import PROTECTED, PROTECTED_FILES

_APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}\Z")
_XDG = {"Desktop": "xdg-desktop", "Documents": "xdg-documents", "Downloads": "xdg-download", "Music": "xdg-music",
        "Pictures": "xdg-pictures", "Public": "xdg-public-share", "Templates": "xdg-templates", "Videos": "xdg-videos"}
REMOVABLE = ("/media", "/run/media", "/mnt")
_RANK = {"chooser": 0, "folders": 1, "all": 2}
# Luma's own system apps (Depot is the installer and reads its records) keep
# what their signed manifests declare.
FIRST_PARTY = ("org.projectluma.", "io.luma.")


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def declared_filesystems(app_id: str, runner=subprocess.run) -> list[str]:
    """What the installed bundle itself asks for (its metadata, not overrides)."""
    try:
        done = runner(["flatpak", "info", "--show-metadata", app_id], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return filesystems_from_metadata(done.stdout if done.returncode == 0 else "")


def filesystems_from_metadata(text: str) -> list[str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error:
        return []
    value = parser.get("Context", "filesystems", fallback="")
    return [item.strip() for item in value.split(";") if item.strip() and not item.strip().startswith("!")]


def level(filesystems: list[str]) -> tuple[str, list[str]]:
    """The Luma file level and named folders a list of Flatpak filesystems amounts to."""
    folders: list[str] = []
    for item in filesystems:
        base = item.split(":")[0]
        if base in {"home", "host", "host-os"} or base in {"~", "~/"} or base.startswith(("/home", "/var/home")):
            return "all", []
        for name, xdg in _XDG.items():
            if base == xdg or base.startswith(xdg + "/"):
                folders.append(name)
        if base.startswith("~/") and "/" not in base[2:].strip("/") and not base[2:].startswith("."):
            folders.append(base[2:].strip("/"))
    folders = list(dict.fromkeys(folders))
    return ("folders" if folders else "chooser"), folders


def exclusions(app_id: str, declared: list[str]) -> list[str]:
    """``flatpak override`` arguments that keep protected paths out of a bundle with home access."""
    arguments: list[str] = []
    broad = level(declared)[0] == "all"
    if not broad or app_id.startswith(FIRST_PARTY):
        return arguments
    for path in PROTECTED:
        if app_id in path:
            continue  # its own data
        if path in PROTECTED_FILES:
            arguments.append(f"--filesystem=~/{path}:ro")
        else:
            arguments.append(f"--nofilesystem=~/{path}")
    if any(item.split(":")[0] == "host" for item in declared):
        arguments.extend(("--nofilesystem=host", "--filesystem=home"))
        arguments.extend(f"--filesystem={root}" for root in REMOVABLE)
    return arguments


def grants(level_name: str, folders: list[str]) -> list[str]:
    if level_name == "all":
        return ["--filesystem=home"]
    if level_name == "folders":
        return [f"--filesystem={_XDG.get(name, '~/' + name)}" for name in folders]
    return []


def _state_path(app_id: str) -> Path:
    return _data_home() / "luma" / "installer" / "permission-backups" / f"{app_id}.luma.json"


def apply(app_id: str, level_name: str, folders: list[str], *, runner=subprocess.run, declared: list[str] | None = None) -> list[str]:
    """Make the Flatpak's override say exactly: the person's own override, Luma's exclusions, Luma's grants."""
    if not _APP_ID.fullmatch(app_id):
        raise InstallerError("The Flatpak application ID is invalid.")
    from .valet_permissions import _backup_once
    declared = declared_filesystems(app_id, runner) if declared is None else declared
    declared_level, _ = level(declared)
    if _RANK.get(level_name, 0) < _RANK[declared_level]:
        # Narrower than the bundle needs: Flatpak would hide its folders behind
        # empty ones. Keep its declared level; exclusions still apply.
        level_name = declared_level
    root = _data_home()
    _backup_once(app_id, root)
    backup = json.loads((root / "luma/installer/permission-backups" / f"{app_id}.json").read_text(encoding="utf-8"))
    override = root / "flatpak" / "overrides" / app_id
    arguments = [*exclusions(app_id, declared), *grants(level_name, folders)]
    wanted = {"arguments": arguments}
    state = _state_path(app_id)
    try:
        if json.loads(state.read_text(encoding="utf-8")) == wanted and override.exists():
            return arguments
    except (OSError, ValueError):
        pass
    # Back to the person's own override, then Luma's lines on top.
    original = backup.get("original_override")
    if original is None:
        try:
            override.unlink()
        except FileNotFoundError:
            pass
    else:
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(original, encoding="utf-8")
    if arguments:
        try:
            done = runner(["flatpak", "override", "--user", *arguments, app_id], capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InstallerError("The app's file access could not be saved.") from error
        if done.returncode:
            raise InstallerError("The app's file access could not be saved: " + (done.stderr or "").strip()[-200:])
    state.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.write_text(json.dumps(wanted), encoding="utf-8")
    return arguments


def forget(app_id: str) -> None:
    try:
        _state_path(app_id).unlink()
    except FileNotFoundError:
        pass
