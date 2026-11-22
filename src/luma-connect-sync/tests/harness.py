# SPDX-License-Identifier: Apache-2.0
"""Simulated devices for tests: each has its own folder, keystore, local state
and Hub credential, and talks to a Hub over HTTP (the fake, or a real one)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from luma_connect_sync.account import Account  # noqa: E402
from luma_connect_sync.appdata import AppData, Target  # noqa: E402
from luma_connect_sync.engine import Engine, LocalState  # noqa: E402
from luma_connect_sync.hub import Hub, Registration  # noqa: E402
from luma_connect_sync.keystore import FileKeystore  # noqa: E402


OWNER = "flatpak:test-remote"     # every test device installs its apps from the same remote


@dataclass
class Device:
    name: str
    root: Path
    hub: Hub
    local: LocalState
    keystore: FileKeystore
    account: Account
    engine: Engine
    appdata: AppData

    def target(self, app: str) -> Target:
        return Target(app, "flatpak", self.root / "home/.var/app" / app, app, owner=OWNER)


def make_device(name: str, hub_url: str, device_id: str, token: str, root: Path) -> Device:
    root.mkdir(parents=True, exist_ok=True)
    hub = Hub(Registration(device_id, token, hub_url, name))
    local = LocalState(root / "state" / "state.sqlite")
    keystore = FileKeystore(root / "keys")
    account = Account(hub, keystore, local)
    engine = Engine(hub, account, local)
    data = AppData(engine, local, device_id, name)
    data.versions_root = root / "versions"
    return Device(name, root, hub, local, keystore, account, engine, data)


def enrol_fake(hub_url: str, account: str, name: str, device_id: str | None = None) -> tuple[str, str]:
    body = json.dumps({"account": account, "name": name, **({"device_id": device_id} if device_id else {})}).encode()
    request = urllib.request.Request(hub_url + "/__test/devices", data=body, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        value = json.loads(response.read())
    return value["device_id"], value["token"]


def temporary_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="luma-connect-sync-"))


def tree(folder: Path) -> dict[str, bytes]:
    out = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[str(path.relative_to(folder))] = path.read_bytes()
    return out


os.environ.setdefault("LUMA_CONNECT_SYNC_KEYSTORE", "file:/nonexistent")
