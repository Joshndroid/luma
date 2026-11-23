# SPDX-License-Identifier: Apache-2.0
"""One device's Sync: the pieces put together, used by the service and the CLI.

A cycle brings the account key up to date, publishes this device's apps,
installs the ones other computers have, and syncs each closed app's data.
Every cycle starts from what is on the device and what the Hub has, so a
cycle cut short (offline, a crash, a full quota) is finished by the next.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import socket
import time
from contextlib import contextmanager
from pathlib import Path

from . import apps, appdata, portal_secrets
from . import quiet as quiet_points
from .account import Account, SyncStateError
from .engine import Engine, LocalState, state_directory
from .hub import Hub, HubError, Offline, load_registration
from .keystore import KeystoreError, open_keystore

log = logging.getLogger("luma-connect-sync")
CATEGORIES = ("apps", "app_data", "settings", "appearance", "wallpaper", "keyboard", "wifi", "passwords", "browser")
PHASE_ONE = {"apps", "app_data"}       # the others are kept as switches and sync from Phase 2
DEFAULT_OFF = {"wallpaper"}


def device_class() -> str:
    """Computer, Handheld or TV (ADR-054 §3), from the machine's chassis."""
    configured = os.environ.get("LUMA_DEVICE_CLASS", "")
    if configured in {"computer", "handheld", "tv"}:
        return configured
    try:
        chassis = Path("/sys/class/dmi/id/chassis_type").read_text().strip()
    except OSError:
        chassis = ""
    if chassis == "30":                       # a tablet; convertibles (31) and detachables (32) are computers
        return "handheld"
    try:
        hostnamectl = Path("/etc/machine-info").read_text()
        if "CHASSIS=handset" in hostnamectl or "CHASSIS=tablet" in hostnamectl:
            return "handheld"
        if "CHASSIS=tv" in hostnamectl:
            return "tv"
    except OSError:
        pass
    return "computer"


class Sync:
    def __init__(self) -> None:
        registration = load_registration()
        if registration is None:
            raise SyncStateError("signed_out", "Sign in to Luma Connect first.")
        self.registration = registration
        self.hub = Hub(registration)
        self.local = LocalState(state_directory(registration.device) / "state.sqlite")
        self.keystore = open_keystore()
        self.device_class = device_class()
        self.account = Account(self.hub, self.keystore, self.local, self.device_class)
        self.engine = Engine(self.hub, self.account, self.local)
        self.appdata = appdata.AppData(self.engine, self.local, registration.device, registration.name or socket.gethostname())
        # Checked again, live, right before a snapshot is published or an update swapped in (review M3).
        self.appdata.still_quiet = lambda app: app not in quiet_points.running()
        self.watcher = None
        self.events: list[tuple[str, tuple]] = []
        self.last: dict[str, object] = self.local.get("last", {})

    # ------------------------------------------------------------ settings on this device
    def settings(self) -> dict:
        values = self.local.get("settings", {})
        return {"enabled": values.get("enabled", True),
                "categories": {name: values.get("categories", {}).get(name, name not in DEFAULT_OFF) for name in CATEGORIES}}

    def set_setting(self, *, enabled: bool | None = None, category: str | None = None, on: bool | None = None) -> None:
        values = self.local.get("settings", {})
        if enabled is not None:
            values["enabled"] = enabled
        if category is not None:
            if category not in CATEGORIES:
                raise SyncStateError("unknown_category", f"There is no {category} category.")
            values.setdefault("categories", {})[category] = bool(on)
        self.local.put("settings", values)
        self.emit("StateChanged")

    @contextmanager
    def exclusive(self):
        """One cycle or key change at a time, between the service and the CLI."""
        path = self.local.path.parent / "sync.lock"
        with open(path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def emit(self, name: str, *args) -> None:
        self.events.append((name, args))

    # ------------------------------------------------------------ the cycle
    def local_fingerprint(self) -> str:
        """Cheap: what is installed, and the metadata of every closed app's folder. No Hub request."""
        parts = sorted(apps.installed_names())
        for app, target in sorted(appdata.targets(self.hub.device).items()):
            if self.watcher is None or self.watcher.quiet(app):
                parts.append(app + json.dumps(appdata.quick_state(target.folder, self.appdata.patterns), sort_keys=True))
                parts.append(json.dumps(portal_secrets._stamp(target.app)))   # sign-ins change without the synced folder changing
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    def idle(self, remote_seq: int | None) -> bool:
        """Nothing new on the Hub since the last full cycle, and nothing new here: skip the cycle entirely."""
        if remote_seq is None or remote_seq != self.local.get("cycle_seq"):
            return False
        return self.local_fingerprint() == self.local.get("cycle_local")

    def cycle(self, *, only_app: str | None = None, remote_seq: int | None = None) -> dict:
        settings = self.settings()
        started = time.time()
        if only_app is None and self.idle(remote_seq):
            return self.last
        with self.exclusive():
            try:
                standing = self.account.refresh()
                seq_at_start = int(self.account.state(fresh=False).get("seq", 0))
            except Offline as error:
                return self._record("offline", str(error))
            except HubError as error:
                if error.status == 401:
                    return self._record("revoked", "This device was removed from Luma Connect.")
                raise
            except SyncStateError as error:
                return self._record("attention", str(error), code=error.code)
            except KeystoreError as error:
                return self._record("attention", str(error), code="keys_unreadable")
            if standing == "revoked":
                self.account.forget()
                return self._record("revoked", "This device no longer syncs. Everything on it stays.")
            if standing != "member":
                return self._record(standing, "")
            if not settings["enabled"]:
                return self._record("paused", "Sync is off on this device.")
            try:
                self._joins()
                if settings["categories"]["apps"] and not only_app:
                    self.sync_apps()
                if settings["categories"]["app_data"]:
                    self.sync_appdata(only_app)
            except Offline as error:
                return self._record("offline", str(error))
            except HubError as error:
                if error.code == "quota":
                    return self._record("attention", "New changes stay on this device until there's room.", code="quota")
                if error.code == "rekey_pending":
                    return self._record("syncing", "Replacing keys after a device was removed.")
                if error.status == 429:
                    return self._record("syncing", "Waiting for the Hub.", code="rate_limited")
                raise
            if only_app is None:
                # Our own writes raised the sequence; the next cycle sees them once and is then idle.
                self.local.put("cycle_seq", seq_at_start)
                self.local.put("cycle_local", self.local_fingerprint())
            if time.time() - self.local.get("pruned", 0) > 86400:
                self.appdata.prune_versions()
                self.local.put("pruned", int(time.time()))
            return self._record("up_to_date", "", seconds=round(time.time() - started, 2))

    def _record(self, state: str, detail: str, **extra) -> dict:
        self.last = {"state": state, "detail": detail, "at": int(time.time()), **extra}
        if state == "up_to_date":
            self.last["last_sync"] = int(time.time())
        else:
            self.last["last_sync"] = self.local.get("last", {}).get("last_sync")
        self.local.put("last", self.last)
        self.emit("StateChanged")
        return self.last

    # ------------------------------------------------------------ apps installed
    @staticmethod
    def _same(a: dict, b: dict) -> bool:
        strip = lambda value: {k: v for k, v in value.items() if k not in {"from", "class"}}  # noqa: E731
        return strip(a) == strip(b)

    def sync_apps(self) -> None:
        self.engine.fetch(apps.ZONE)
        published = self.engine.all(apps.ZONE)
        for name, value in apps.local_records(self.hub.device, self.device_class).items():
            current = published.get(name)
            if current is None or not self._same(current[1], value):
                self.engine.save(apps.ZONE, name, value)
        installed = apps.installed_names()
        shipped = {f"flatpak:{app}" for app in apps.image_apps()}
        everything = sorted((name, entry) for name, entry in self.engine.all(apps.ZONE).items() if name not in shipped)
        # Valet's restore list shows every app at once: the ones not reached yet as Waiting.
        for name, (_version, value, _writer) in everything:
            if name not in installed and value.get("class", "computer") == self.device_class and not self.local.get(f"install:{name}"):
                self._install_state(name, "waiting", value)
        for name, (_version, value, _writer) in everything:
            if name in installed:
                self._install_state(name, "ready", value)
                continue
            state = self.local.get(f"install:{name}", {})
            if value.get("class", "computer") != self.device_class:
                self._install_state(name, "offered", value, reason="Installed on another kind of device")
                continue
            if state.get("state") in {"unavailable", "declined"} and state.get("record") == value:
                continue
            if state.get("state") == "failed" and time.time() - state.get("at", 0) < 3600 and state.get("record") == value:
                continue
            self._install_state(name, "installing", value)
            try:
                apps.install(value)
            except apps.Unavailable as error:
                self._install_state(name, "unavailable", value, reason=str(error))
                continue
            except Exception as error:  # noqa: BLE001 - one app's failure doesn't stop the rest
                log.warning("Installing %s failed: %s", name, str(error)[:200])
                self._install_state(name, "failed", value, reason=str(error)[:300])
                continue
            self._install_state(name, "ready", value)

    def _install_state(self, name: str, state: str, value: dict, reason: str = "") -> None:
        previous = self.local.get(f"install:{name}", {})
        entry = {"state": state, "reason": reason, "record": value, "at": int(time.time()), "id": value.get("id"),
                 "name": value.get("name") or value.get("id"), "kind": value.get("kind")}
        if previous.get("state") != state or previous.get("reason") != reason:
            self.local.put(f"install:{name}", entry)
            self.emit("RestoreChanged", str(value.get("id")))

    # ------------------------------------------------------------ app data
    def sync_appdata(self, only_app: str | None = None) -> None:
        targets = appdata.targets(self.hub.device)
        for app, target in sorted(targets.items()):
            if only_app and app != only_app:
                continue
            quiet = self.watcher.quiet(app) if self.watcher else app not in quiet_points.running()
            version = apps.flatpak_commit_date(app) if target.kind == "flatpak" else 0
            before = self.appdata.info(app).get("state")
            try:
                state = self.appdata.sync_app(target, quiet=quiet, version_date=version)
            except appdata.SnapshotAborted as error:
                log.info("%s: %s; trying again at the next quiet point", app, error)
                continue
            except HubError as error:
                if error.code != "stale_generation":
                    raise
                log.info("%s: waiting for its zone key to be replaced", app)
                continue
            if state != before:
                self.emit("RestoreChanged", app)

    # ------------------------------------------------------------ joins (existing devices)
    def _joins(self) -> None:
        seen = set(self.local.get("joins_seen", []))
        current = self.account.join_requests()
        ids = {row["id"] for row in current}
        for row in current:
            # A card for every request this device answered, before its code is ready (review B2): no silent answers.
            if row["id"] not in seen and not row.get("held"):
                # No code in the signal: every session-bus client hears signals; the card fetches it (review L8).
                self.emit("JoinRequested", row["id"], row["device"], row["name"], row["class"])
                seen.add(row["id"])
        for gone in seen - ids:
            self.emit("JoinCleared", gone, "answered")
        self.local.put("joins_seen", sorted(seen & ids))
        # A device that let itself in with the recovery key is shown on every member, with Remove. Known from the
        # statement the recovery key signed, not from what the Hub says (review M2); a device the Hub calls a member
        # without a signed statement is "suspicious" and gets no key.
        known = set(self.local.get("recovery_joins_seen", []))
        members = self.account.members()
        names = {d["device"]: d for d in self.account.state(fresh=False)["devices"]}
        for device, entry in members.items():
            key = f"{device}:{entry['sign']}"
            if entry.get("via") == "recovery" and device != self.hub.device and key not in known:
                row = names.get(device, {})
                self.emit("RecoveryJoined", device, row.get("name", ""), row.get("class", "computer"))
                known.add(key)
        self.local.put("recovery_joins_seen", sorted(known))
        self.local.put("suspicious", sorted(d for d, row in names.items() if row["state"] == "member" and d not in members))

    # ------------------------------------------------------------ what the UI reads
    def state(self) -> dict:
        last = dict(self.local.get("last", {}))
        settings = self.settings()
        try:
            hub = self.account.state(fresh=False) if self.account._state else self.account.state()
        except (Offline, HubError):
            hub = {}
        usage = hub.get("usage") or {}
        state = last.get("state", "syncing")
        attention = None
        replaced = self.local.get("recovery_replaced")
        if state == "revoked":
            attention = {"reason": "revoked", "detail": last.get("detail", "")}
        elif replaced:
            # Review S1: never silent. Shown until the person reads the new words (RevealRecoveryKey) and acknowledges.
            attention = {"reason": "new_recovery_key", "at": replaced.get("at"),
                         "detail": "Your recovery key was replaced when the device that kept it was removed. "
                                   "Write down the new one; the old words no longer work."}
        elif last.get("code") == "keys_unreadable":
            attention = {"reason": "keys_unreadable", "detail": last.get("detail", "")}
        elif last.get("code") == "quota":
            attention = {"reason": "quota", "detail": last.get("detail", ""), "used": usage.get("bytes"), "quota": usage.get("quota_bytes")}
        conflicts = [c for c in self.local.get("conflicts", []) if time.time() - c["at"] < 86400]
        if conflicts and not attention:
            attention = {"reason": "conflict", "detail": f"{conflicts[-1]['name']} changed on two devices",
                         "app": conflicts[-1]["app"], "from": conflicts[-1]["from"], "at": conflicts[-1]["at"]}
        shown = {"up_to_date": "up_to_date", "paused": "paused", "offline": "paused", "syncing": "syncing", "new": "off",
                 "pending": "joining", "revoked": "attention", "attention": "attention"}.get(state, "syncing")
        if attention and shown == "up_to_date":
            shown = "attention"
        return {"state": shown, "detail": last.get("detail", ""), "attention": attention, "last_sync": last.get("last_sync"),
                "enabled": settings["enabled"], "categories": settings["categories"], "phase_one": sorted(PHASE_ONE),
                "storage": {"used": usage.get("bytes", 0), "quota": usage.get("quota_bytes", 0)},
                "devices": len([d for d in hub.get("devices", []) if d["state"] == "member"]), "this_device": self.hub.device,
                "join_requests_held": self.local.get("join_requests_held", 0)}

    def devices(self) -> list[dict]:
        hub = self.account.state()
        return [{"device": d["device"], "name": d["name"], "class": d.get("class", "computer"), "state": d["state"],
                 "this_device": d["device"] == self.hub.device, "last_sync": (d.get("last_seen_at") or 0) // 1000 or None}
                for d in hub["devices"] if d["state"] != "revoked"]

    def app_list(self) -> list[dict]:
        out = []
        for app, target in sorted(appdata.targets(self.hub.device).items()):
            info = self.appdata.info(app)
            out.append({"app": app, "name": target.name or app, "kind": target.kind,
                        "size": appdata.folder_bytes(target.folder, self.appdata.patterns),
                        "enabled": self.appdata.enabled(app), "denied": appdata.denied(app), "uses_api": False,
                        "state": info.get("state", "waiting"), "last_synced": info.get("last_synced"),
                        "syncs_when_closed": True})
        return out

    def restore(self) -> list[dict]:
        rows = []
        installs = {key[len("install:"):]: value for key, value in self._kv_prefix("install:").items()}
        for name, entry in sorted(installs.items()):
            app = str(entry.get("id"))
            data = self.appdata.info(app)
            state = entry["state"]
            if state == "ready" and data.get("state") == "restoring":
                state, progress = "restoring", data.get("progress")
            elif state == "ready" and data.get("state") in {"waiting", "waiting_update"}:
                state, progress = "waiting", None
            else:
                progress = None
            rows.append({"app": app, "name": entry.get("name") or app, "kind": entry.get("kind"), "state": state,
                         "progress": progress, "reason": entry.get("reason", "")})
        return rows

    def _kv_prefix(self, prefix: str) -> dict:
        with self.local.lock:
            rows = self.local.db.execute("SELECT key,value FROM kv WHERE key LIKE ?", (prefix + "%",)).fetchall()
        return {key: json.loads(value) for key, value in rows}


__all__ = ["Sync", "SyncStateError", "KeystoreError", "CATEGORIES"]
