# SPDX-License-Identifier: Apache-2.0
"""Apps installed (ADR-054 §3): one record per app, the set is the union.

A record names where the app came from, never a version or a binary:

* Flatpak (Depot, Flathub, any remote): app id, branch, the remote's name
  and URL, and whether it is held (masked). The receiving device installs
  with ``--user`` from a remote it already trusts at the same URL.
* ADR-052 store apps (.deb, .rpm by name): the store record's origin, which
  names the package, its userland and any repository the package added.
  The receiving device rebuilds the tree from those repositories with its
  own installer; nothing built elsewhere is copied.

Removing an app on one device does not remove it elsewhere. Records only
travel between devices of the same class (Phase 1: computers).
"""
from __future__ import annotations

import configparser
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ZONE = "apps"
TIMEOUT = 3600
# Runtimes, extensions and Luma's own system pieces are not apps a person chose.
SYSTEM_PREFIXES = ("org.projectluma.Platform", "org.freedesktop.Platform", "org.gnome.Platform", "org.kde.Platform")


BASELINE_ROLES = Path("/usr/share/luma/app-baseline/roles.json")


def image_apps() -> set[str]:
    """Apps the OS image itself ships (the offline app baseline): every device has them, so they don't sync."""
    try:
        return set(json.loads(BASELINE_ROLES.read_text()).get("roles") or {})
    except (OSError, ValueError):
        return set()


def _run(arguments: list[str], timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, capture_output=True, text=True, timeout=timeout, check=check,
                          env={**os.environ, "LC_ALL": "C.UTF-8"})


def flatpak_available() -> bool:
    return any((Path(folder) / "flatpak").exists() for folder in os.environ.get("PATH", "/usr/bin").split(":"))


def remotes() -> list[dict[str, str]]:
    found = []
    for installation in ("user", "system"):
        try:
            out = _run(["flatpak", "remotes", f"--{installation}", "--columns=name,url,options"]).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[1].strip():
                found.append({"name": parts[0].strip(), "url": parts[1].strip().rstrip("/"), "installation": installation,
                              "disabled": "disabled" in (parts[2] if len(parts) > 2 else "")})
    return found


def masked() -> set[str]:
    held: set[str] = set()
    for installation in ("user", "system"):
        try:
            out = _run(["flatpak", "mask", f"--{installation}"], check=False).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        held.update(line.strip() for line in out.splitlines() if line.strip() and not line.startswith("No "))
    return held


def installed_flatpaks() -> dict[str, dict[str, str]]:
    if not flatpak_available():
        return {}
    out = _run(["flatpak", "list", "--app", "--columns=application,origin,branch,installation,arch"]).stdout
    apps: dict[str, dict[str, str]] = {}
    for line in out.splitlines():
        parts = [part.strip() for part in line.split("\t")]
        if len(parts) < 5 or parts[0].startswith(SYSTEM_PREFIXES):
            continue
        app, origin, branch, installation, arch = parts[:5]
        # The user installation wins when an app is in both.
        if app not in apps or installation == "user":
            apps[app] = {"id": app, "remote": origin, "branch": branch, "installation": installation, "arch": arch}
    return apps


def flatpak_commit_date(app: str) -> int:
    """When the installed build was made, to order app versions (state is never applied to an older build)."""
    try:
        out = _run(["flatpak", "info", app], check=False).stdout
    except (subprocess.SubprocessError, OSError):
        return 0
    for line in out.splitlines():
        key, _, value = line.strip().partition(":")
        if key == "Date":
            try:
                return int(time.mktime(time.strptime(value.strip()[:19], "%Y-%m-%d %H:%M:%S")))
            except ValueError:
                return 0
    return 0


def store_records() -> list[dict[str, object]]:
    """ADR-052 store apps from Valet's records, with the tree record that says how to rebuild each."""
    try:
        from luma_installer.desktop import iter_records
        from luma_installer.app_store import Store
    except ImportError:
        return []
    store = Store()
    out = []
    for record in iter_records():
        meta = record.get("store")
        if not isinstance(meta, dict) or not meta.get("id") or not meta.get("commit"):
            continue
        try:
            tree = store.read_record(str(meta["commit"]))
        except Exception:  # noqa: BLE001 - a damaged tree is not offered
            continue
        if tree.get("format") not in {"deb", "rpm"}:
            continue  # AppImages and archives arrive as files; there is no repository to rebuild them from
        origin = dict(tree.get("origin") or {})
        # Only what names the app: never repository files, keys, extra packages or commands (review H5).
        hosts = sorted({row["uri"].split("/")[2] for row in record.get("vendor_repositories") or [] if "://" in str(row.get("uri", ""))})
        out.append({"id": str(meta["id"]), "name": str(record.get("name") or meta["id"]), "format": tree["format"],
                    "sha256": str(record.get("sha256") or ""), "icon": str(record.get("icon") or ""),
                    "package": str(origin.get("package") or ""), "userland": str(tree.get("userland") or ""),
                    "vendor_hosts": hosts, "needs_vendor_repository": bool(origin.get("added_repositories")),
                    "tree": {"userland": tree.get("userland"), "origin": {"added_repositories": [1] if origin.get("added_repositories") else []}},
                    "hold": bool(record.get("held") or record.get("hold"))})
    return out


def local_records(device: str, device_class: str = "computer") -> dict[str, dict[str, object]]:
    """What this device would publish: name -> record value."""
    out: dict[str, dict[str, object]] = {}
    known = {remote["name"] + "@" + remote["installation"]: remote for remote in remotes()}
    held = masked()
    shipped = image_apps()
    for app in installed_flatpaks().values():
        if app["id"] in shipped:
            continue
        remote = known.get(app["remote"] + "@" + app["installation"]) or known.get(app["remote"] + "@system") or known.get(app["remote"] + "@user")
        if not remote:
            continue
        out[f"flatpak:{app['id']}"] = {"kind": "flatpak", "id": app["id"], "branch": app["branch"], "remote": app["remote"],
                                       "url": remote["url"], "hold": app["id"] in held, "class": device_class, "from": device}
    for record in store_records():
        out[f"store:{record['id']}"] = {"kind": "store", "id": record["id"], "name": record["name"], "format": record["format"],
                                        "sha256": record["sha256"], "package": record["package"], "userland": record["userland"],
                                        "vendor_hosts": record["vendor_hosts"], "needs_vendor_repository": record["needs_vendor_repository"],
                                        "hold": record["hold"], "class": device_class, "from": device}
    return out


def installed_names() -> set[str]:
    names = {f"flatpak:{app}" for app in installed_flatpaks()}
    names.update(f"store:{record['id']}" for record in store_records())
    return names


# ---------------------------------------------------------------- installing on the receiving device

class Unavailable(Exception):
    """Not available for this device, with the reason in plain words."""


def _user_remote_for(url: str, name: str) -> str:
    known = remotes()
    for remote in known:
        if remote["installation"] == "user" and remote["url"] == url.rstrip("/") and not remote["disabled"]:
            return remote["name"]
    system = next((r for r in known if r["installation"] == "system" and r["url"] == url.rstrip("/")), None)
    if system is None:
        raise Unavailable("Its source isn't set up on this device.")
    # The receiving device's own copy of that remote's signing key, never one from the record.
    keyring = Path("/var/lib/flatpak/repo") / f"{system['name']}.trustedkeys.gpg"
    target = name if not any(r["installation"] == "user" and r["name"] == name for r in known) else f"{name}-sync"
    command = ["flatpak", "remote-add", "--user", "--if-not-exists"]
    if keyring.is_file():
        command.append(f"--gpg-import={keyring}")
    else:
        config = configparser.ConfigParser(strict=False, interpolation=None)
        config.read("/var/lib/flatpak/repo/config")
        if config.get(f'remote "{system["name"]}"', "gpg-verify", fallback="true").lower() not in {"false", "0"}:
            raise Unavailable("Its source has no signing key on this device.")
    _run(command + [target, system["url"]])
    return target


def install_flatpak(value: dict[str, object]) -> None:
    app, branch = str(value["id"]), str(value.get("branch") or "stable")
    remote = _user_remote_for(str(value["url"]), str(value["remote"]))
    arch = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine(), platform.machine())
    try:
        _run(["flatpak", "remote-info", "--user", f"--arch={arch}", remote, f"app/{app}/{arch}/{branch}"], timeout=300)
    except subprocess.CalledProcessError:
        raise Unavailable(f"Not available for this device · {'x86 only' if arch == 'aarch64' else 'no build for this device'}") from None
    _run(["flatpak", "install", "--user", "--noninteractive", "-y", remote, f"app/{app}/{arch}/{branch}"], timeout=TIMEOUT)
    if value.get("hold"):
        _run(["flatpak", "mask", "--user", app], check=False)


def install_store(value: dict[str, object]) -> None:
    """Rebuild a store app from this device's own repositories for its distribution, in a child process.

    A record that needed a vendor's repository on the other device is not installed here on its own: the person
    installs it on this device (review H5).
    """
    if value.get("needs_vendor_repository"):
        hosts = ", ".join(str(h) for h in value.get("vendor_hosts") or []) or "its maker's site"
        raise Unavailable(f"Installed from {hosts} on another device. Install it here yourself if you want it.")
    try:
        import luma_installer  # noqa: F401
    except ImportError:
        raise Unavailable("This device can't install apps from other systems.") from None
    result = subprocess.run([sys.executable, "-m", "luma_connect_sync.store_rebuild"], input=json.dumps(value), text=True,
                            capture_output=True, timeout=TIMEOUT, env={**os.environ, "LC_ALL": "C.UTF-8"})
    if result.returncode == 3:
        raise Unavailable(result.stdout.strip() or "Not available for this device")
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip().splitlines()[-1][:300] if (result.stderr or result.stdout).strip() else "The rebuild failed.")


def install(value: dict[str, object]) -> None:
    if value.get("kind") == "flatpak":
        install_flatpak(value)
    elif value.get("kind") == "store":
        install_store(value)
    else:
        raise Unavailable("This kind of app can't be installed here yet.")
