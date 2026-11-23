# SPDX-License-Identifier: Apache-2.0
"""Sign-ins of Flatpak apps (ADR-054 §4.1), carried in the "keychain" zone.

A sandboxed app that stores passwords through libsecret keeps them in its
own file (``data/keyrings/default.keyring``), locked with a secret that the
secret portal keeps for that app alone; the host keyring does not let other
clients read it (gnome-keyring 50 hides portal items). So the file itself
can't be opened on another device, and this service can't read the portal's
secret either.

Instead the items are read and written *as the app*: a short helper runs in
the app's own sandbox (``flatpak run --command=python3 <app>``), where
libsecret opens the app's file with the app's own portal secret. On the
receiving device the same helper stores them, so they are encrypted under
that device's portal secret. ``data/keyrings/`` itself never syncs. The
helper runs only when the app is closed and its keyring file changed, or a
newer record arrived. Apps that talk to the host keyring directly are not
covered in Phase 1 (their items belong to the keychain category).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger("luma-connect-sync")
ZONE = "keychain"
MAX_ITEMS = 64
HELPER_RUNS: dict[str, float] = {}     # app -> when a helper last ran under its id (the watcher ignores those closes)

EXPORT = r"""
import base64, json, sys, gi
gi.require_version("Secret", "1")
from gi.repository import Secret
items = Secret.password_search_sync(None, {}, Secret.SearchFlags.ALL | Secret.SearchFlags.UNLOCK | Secret.SearchFlags.LOAD_SECRETS, None)
out = []
if len(items) > %d:
    print("dropped", len(items) - %d, file=sys.stderr)
for item in items[:%d]:
    value = item.retrieve_secret_sync(None)   # a SecretRetrievable in the file backend
    if value is None:
        continue
    out.append({"label": item.get_label(), "attributes": dict(item.get_attributes()), "content_type": value.get_content_type() or "text/plain",
                "secret": base64.b64encode(bytes(value.get())).decode()})
print(json.dumps(sorted(out, key=lambda e: sorted(e["attributes"].items()))))
""" % (MAX_ITEMS, MAX_ITEMS, MAX_ITEMS)

IMPORT = r"""
import base64, json, sys, gi
gi.require_version("Secret", "1")
from gi.repository import Secret
skipped = 0
for entry in json.load(sys.stdin):
    try:
        text = base64.b64decode(entry["secret"]).decode("utf-8")   # libsecret's Python binding takes text
    except UnicodeDecodeError:
        skipped += 1
        continue
    if not Secret.password_store_sync(None, entry["attributes"], Secret.COLLECTION_DEFAULT, entry["label"], text, None):
        sys.exit(1)
print("ok" if not skipped else "ok")
"""


def keyring_file(app: str) -> Path:
    return Path.home() / ".var/app" / app / "data/keyrings/default.keyring"


def uses_portal(app: str) -> bool:
    """Its own keyring file exists and it can't reach the host keyring directly."""
    if not keyring_file(app).is_file():
        return False
    try:
        permissions = subprocess.run(["flatpak", "info", "--show-permissions", app], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "org.freedesktop.secrets" not in permissions


MAX_OUTPUT = 1024 * 1024        # the helper's answer; more is refused (review L6)


def _helper(app: str, code: str, stdin: str = "") -> str:
    HELPER_RUNS[app] = time.monotonic()
    with tempfile.TemporaryFile() as out:
        result = subprocess.run(["flatpak", "run", "--command=python3", "--die-with-parent", app, "-c", code], input=stdin.encode(),
                                stdout=out, stderr=subprocess.DEVNULL, timeout=120, env={**os.environ, "LC_ALL": "C.UTF-8"})
        HELPER_RUNS[app] = time.monotonic()
        if result.returncode:
            raise RuntimeError(f"the sign-in helper for {app} failed")
        out.seek(0)
        text = out.read(MAX_OUTPUT + 1)
    if len(text) > MAX_OUTPUT:
        raise RuntimeError(f"the sign-in helper for {app} said too much")
    lines = text.decode("utf-8", "replace").strip().splitlines()
    return lines[-1] if lines else ""


def read(app: str) -> list[dict[str, object]]:
    try:
        return json.loads(_helper(app, EXPORT) or "[]")
    except (RuntimeError, ValueError, OSError, subprocess.SubprocessError):
        return []


def write(app: str, entries: list[dict[str, object]]) -> None:
    if _helper(app, IMPORT, json.dumps(entries)) != "ok":
        raise RuntimeError(f"the sign-ins of {app} could not be stored")


def fingerprint(entries: list[dict[str, object]]) -> str:
    return hashlib.sha256(json.dumps(entries, sort_keys=True).encode()).hexdigest()


def _stamp(app: str) -> list[int]:
    try:
        info = keyring_file(app).stat()
        return [info.st_size, info.st_mtime_ns]
    except OSError:
        return []


def changed(engine, local, target) -> bool:
    """Cheap: the app's keyring file moved since we last read it."""
    seen = local.get(f"portal:{target.zone}", {})
    return bool(_stamp(target.app)) and seen.get("stamp") != _stamp(target.app)


def publish(engine, local, target) -> None:
    if not changed(engine, local, target) or not uses_portal(target.app):
        return
    entries = read(target.app)
    state = local.get(f"portal:{target.zone}", {})
    _version, record = engine.get(ZONE, target.keychain_name)
    synced = ((record or {}).get("items") or []) if (record or {}).get("owner") == target.owner else []
    merged = {fingerprint([e]): e for e in synced + entries}
    items = sorted(merged.values(), key=lambda e: sorted(dict(e["attributes"]).items()))[:MAX_ITEMS]
    if items and (record or {}).get("items") != items:
        engine.save(ZONE, target.keychain_name, {"items": items, "owner": target.owner})
        log.info("%s: %d sign-ins published", target.app, len(items))
    # "applied" means in this device's keyring: synced items it doesn't have yet are stored now (the app is closed),
    # e.g. when the app made a fresh keyring file before the synced ones arrived.
    here = {fingerprint([e]) for e in entries}
    missing = [e for e in items if fingerprint([e]) not in here]
    if missing:
        write(target.app, missing)
        log.info("%s: %d sign-ins stored", target.app, len(missing))
    local.put(f"portal:{target.zone}", {**state, "stamp": _stamp(target.app), "applied": fingerprint(items)})


def restore(engine, local, target) -> None:
    """Store the synced sign-ins in this device's copy of the app's keyring, as the app."""
    if engine.zone(ZONE, create=False) is not None:
        engine.fetch(ZONE)
    _version, record = engine.get(ZONE, target.keychain_name)
    items = (record or {}).get("items") or []
    state = local.get(f"portal:{target.zone}", {})
    if not items or state.get("applied") == fingerprint(items):
        return
    if (record or {}).get("owner") != target.owner:
        log.info("%s: synced sign-ins belong to another publisher of this app; not stored", target.app)
        return
    write(target.app, items)
    log.info("%s: %d sign-ins stored", target.app, len(items))
    local.put(f"portal:{target.zone}", {**state, "stamp": _stamp(target.app), "applied": fingerprint(items)})
