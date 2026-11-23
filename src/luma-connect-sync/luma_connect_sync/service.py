# SPDX-License-Identifier: Apache-2.0
"""luma-connect-syncd: the user service and org.projectluma.ConnectSync1.

The interface is what Settings, the shell's approval card and status
island, Valet and the launcher read (data/org.projectluma.ConnectSync1.xml).
Structured answers are JSON strings. The recovery key leaves the service
only in the reply to BeginSetup and, after a polkit check, RevealRecoveryKey;
it is never a property, a signal or a log line.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import appdata, crypto  # noqa: E402
from .account import SyncStateError  # noqa: E402
from .hub import HubError, Offline  # noqa: E402
from .quiet import Watcher  # noqa: E402
from .sync import Sync  # noqa: E402

BUS_NAME = "org.projectluma.ConnectSync1"
OBJECT_PATH = "/org/projectluma/ConnectSync1"
INTERFACE = "org.projectluma.ConnectSync1"
ERROR = "org.projectluma.ConnectSync1.Error"
REVEAL_ACTION = "org.projectluma.connect-sync.reveal-recovery-key"
MANAGE_ACTION = "org.projectluma.connect-sync.manage"      # approve a device, remove one, delete synced data, turn Sync off
PROC = Path("/proc")
IDLE_WAIT = 240           # long-poll: about 15 requests an hour while nothing happens
log = logging.getLogger("luma-connect-sync")


def sandboxed(pid: int, proc: Path = PROC) -> str:
    """Why a caller is an app in a sandbox (Flatpak, ADR-052 store app or capsule, snap), or "" for the person's own session.

    Sandboxed apps never reach this service: one with session-bus access could
    otherwise read a join code and approve a device.
    """
    base = proc / str(pid)
    try:
        if (base / "root/.flatpak-info").exists():
            return "flatpak"
    except OSError:
        return "unreadable"
    try:
        cgroup = (base / "cgroup").read_text()
    except OSError:
        return "unreadable"
    for marker, kind in (("/app-flatpak-", "flatpak"), ("/app-luma-", "luma-app"), ("/snap.", "snap")):
        if marker in cgroup:
            return kind
    try:
        context = (base / "attr/current").read_text().strip("\0\n ")
    except OSError:
        context = ""
    if "luma_application.process" in context or ":container_t:" in context:
        return "luma-app"
    return ""


# Methods that hand out keys or take a recovery key answer only the shell, Settings and a person's terminal (review M4).
TRUSTED_ONLY = {"ConfirmJoin", "AcknowledgeRecoveryKey", "ApproveJoin", "RemoveDevice", "JoinWithRecoveryKey", "RevealRecoveryKey", "BeginSetup", "FinishSetup", "ListJoinRequests", "GetJoin", "RequestJoin",
                "DeleteAllSyncedData"}
TRUSTED_UNITS = ("org.gnome.Shell@", "gnome-shell", "org.gnome.Settings", "gnome-control-center", "org.projectluma.Settings",
                 "app-gnome-org.gnome.Settings", "luma-settings")


def trusted_caller(pid: int, proc: Path = PROC) -> bool:
    """The caller's cgroup is the shell's or Settings' unit, or a login session (a terminal)."""
    try:
        cgroup = (proc / str(pid) / "cgroup").read_text().strip().splitlines()[-1].split("::", 1)[-1]
    except (OSError, IndexError):
        return False
    leaf = cgroup.rstrip("/").rsplit("/", 1)[-1]
    if leaf.startswith(("session-",)) and leaf.endswith(".scope"):
        return True
    return any(leaf.startswith(prefix) for prefix in TRUSTED_UNITS)


def introspection() -> str:
    for path in (Path("/usr/share/dbus-1/interfaces/org.projectluma.ConnectSync1.xml"),
                 Path(__file__).resolve().parent.parent / "data" / "org.projectluma.ConnectSync1.xml"):
        if path.is_file():
            return path.read_text()
    raise FileNotFoundError("org.projectluma.ConnectSync1.xml")


class Service:
    def __init__(self, sync: Sync | None = None) -> None:
        self.sync = sync or Sync()
        self.loop = GLib.MainLoop()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dbus")
        self.connection: Gio.DBusConnection | None = None
        self.setup = None
        self.seq = 0
        self.closed_at: dict[str, float] = {}
        self.sync.watcher = Watcher(on_closed=self._closed, on_installed=self.wake.set)

    # ------------------------------------------------------------ background work
    def _closed(self, apps: set[str]) -> None:
        from . import portal_secrets
        now = time.monotonic()
        # A sign-in helper this service ran under the app's id is not the person closing the app.
        apps = {app for app in apps if now - portal_secrets.HELPER_RUNS.get(app, -1e9) > 10}
        if not apps:
            return
        for app in apps:
            self.closed_at[app] = now
        threading.Timer(appdata.QUIET_SECONDS + 1, self.wake.set).start()

    def worker(self) -> None:
        while not self.stop.is_set():
            try:
                self.sync.cycle(remote_seq=self.seq if self.seq else None)
            except Exception:  # noqa: BLE001 - logged once; the next cycle starts clean
                log.exception("Sync cycle failed")
            self._flush_events()
            # Woken by the Hub (another device changed something), an app closing,
            # an install or removal here, or a D-Bus call; otherwise a local look every 5 minutes.
            self.wake.wait(300)
            self.wake.clear()

    def poller(self) -> None:
        attempt = 0
        while not self.stop.is_set():
            try:
                seq = self.sync.hub.wait(self.seq, IDLE_WAIT)
                attempt = 0
                if seq != self.seq:
                    self.seq = seq
                    self.wake.set()
            except Offline:
                attempt += 1
                self.stop.wait(min(300, 5 * 2 ** min(attempt, 6)))
            except HubError as error:
                attempt += 1
                self.stop.wait(error.retry_after or min(300, 5 * 2 ** min(attempt, 6)))

    def _flush_events(self) -> None:
        events, self.sync.events = self.sync.events, []
        for name, args in events:
            GLib.idle_add(self._signal, name, args)

    def _signal(self, name: str, args: tuple) -> bool:
        if self.connection is None:
            return False
        signature = {"StateChanged": "()", "AppsChanged": "()", "RestoreChanged": "(s)", "JoinChanged": "(s)",
                     "JoinRequested": "(ssss)", "JoinCleared": "(ss)", "RecoveryJoined": "(sss)"}[name]
        self.connection.emit_signal(None, OBJECT_PATH, INTERFACE, name, GLib.Variant(signature, tuple(args)))
        return False

    # ------------------------------------------------------------ D-Bus
    def run(self) -> None:
        node = Gio.DBusNodeInfo.new_for_xml(introspection())
        threading.Thread(target=self.sync.watcher.run, daemon=True, name="quiet").start()
        threading.Thread(target=self.worker, daemon=True, name="sync").start()
        threading.Thread(target=self.poller, daemon=True, name="hub-wait").start()

        def acquired(connection: Gio.DBusConnection, _name: str) -> None:
            self.connection = connection
            connection.register_object(OBJECT_PATH, node.interfaces[0], self._call, None, None)

        def lost(*_args) -> None:
            # Another process holds the name: never queue behind it or take it back silently (review M4).
            log.error("Lost or never got %s: another process owns it. Sync stops; clients must not trust that owner.", BUS_NAME)
            self.loop.quit()

        Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.DO_NOT_QUEUE, acquired, None, lost)
        try:
            self.loop.run()
        finally:
            self.stop.set()
            self.sync.watcher.stop()

    def _call(self, connection, sender, _path, _interface, method, parameters, invocation) -> None:
        handler = getattr(self, f"m_{method}", None)
        if handler is None:
            invocation.return_dbus_error(f"{ERROR}.UnknownMethod", method)
            return
        pid = self._pid(sender)
        why = sandboxed(pid) if pid else "unknown"
        if why:
            invocation.return_dbus_error(f"{ERROR}.NotAuthorized", "Apps in a sandbox can't use Luma Connect Sync.")
            return
        if method in TRUSTED_ONLY and not trusted_caller(pid):
            invocation.return_dbus_error(f"{ERROR}.NotAuthorized", "Only the shell, Settings or a terminal may do this.")
            return

        def run() -> None:
            try:
                result = handler(sender, *parameters.unpack())
            except SyncStateError as error:
                GLib.idle_add(invocation.return_dbus_error, f"{ERROR}.{_camel(error.code)}", str(error))
                return
            except Offline as error:
                GLib.idle_add(invocation.return_dbus_error, f"{ERROR}.Offline", str(error))
                return
            except HubError as error:
                GLib.idle_add(invocation.return_dbus_error, f"{ERROR}.{_camel(error.code or 'hub')}", str(error))
                return
            except Exception as error:  # noqa: BLE001
                log.exception("%s failed", method)
                GLib.idle_add(invocation.return_dbus_error, f"{ERROR}.Failed", type(error).__name__)
                return
            self._flush_events()
            GLib.idle_add(invocation.return_value, result)
        self.pool.submit(run)

    @staticmethod
    def _json(value) -> GLib.Variant:
        return GLib.Variant("(s)", (json.dumps(value),))

    # State
    def m_GetState(self, _sender):
        return self._json(self.sync.state())

    def m_SetEnabled(self, sender, on):
        if not on:
            self._require(sender, MANAGE_ACTION)
        self.sync.set_setting(enabled=on)
        self.wake.set()
        return None

    def m_SetCategory(self, _sender, category, on):
        self.sync.set_setting(category=category, on=on)
        self.wake.set()
        return None

    def m_SyncNow(self, _sender):
        self.wake.set()
        return None

    # Turning Sync on
    def m_BeginSetup(self, _sender):
        self.setup = self.sync.account.begin_setup()
        return GLib.Variant("(asai)", (self.setup.words, self.setup.check))

    def m_FinishSetup(self, _sender, answers):
        if self.setup is None:
            raise SyncStateError("no_setup", "Start again: the key wasn't shown on this device.")
        with self.sync.exclusive():
            self.sync.account.finish_setup(self.setup, {int(k): str(v) for k, v in answers.items()})
        self.setup = None
        self.wake.set()
        return None

    def m_CancelSetup(self, _sender):
        self.setup = None
        return None

    def m_RevealRecoveryKey(self, sender):
        self._require(sender, REVEAL_ACTION)
        return GLib.Variant("(as)", (self.sync.account.recovery_words(),))

    def m_AcknowledgeRecoveryKey(self, _sender):
        self.sync.local.delete("recovery_replaced")
        self.sync.emit("StateChanged")
        return None

    # Joining (this device)
    def _join_json(self, view) -> GLib.Variant:
        value = {"state": view.state, "code": view.code, "join": view.join, "approvers": view.approvers}
        GLib.idle_add(self._signal, "JoinChanged", (json.dumps(value),))
        return self._json(value)

    def m_RequestJoin(self, _sender):
        with self.sync.exclusive():
            return self._join_json(self.sync.account.request_join())

    def m_GetJoin(self, _sender):
        with self.sync.exclusive():
            return self._join_json(self.sync.account.join_view())

    def m_ConfirmJoin(self, _sender, matched):
        with self.sync.exclusive():
            view = self.sync.account.confirm_join(bool(matched))
        self.wake.set()
        return self._join_json(view)

    def m_CancelJoin(self, _sender):
        self.sync.account.cancel_join()
        return None

    def m_JoinWithRecoveryKey(self, _sender, words):
        with self.sync.exclusive():
            self.sync.account.join_with_recovery(crypto.normalise_words(words))
        self.wake.set()
        return None

    # Approving (other devices)
    def m_ListJoinRequests(self, _sender):
        return self._json(self.sync.account.join_requests())

    def m_ApproveJoin(self, sender, join_id, code):
        self._require(sender, MANAGE_ACTION)
        self.sync.account.approve(join_id, code)
        self.sync.emit("JoinCleared", join_id, "approved")
        return None

    def m_DenyJoin(self, _sender, join_id):
        self.sync.account.deny(join_id)
        self.sync.emit("JoinCleared", join_id, "denied")
        return None

    # App data
    def m_ListApps(self, _sender):
        return self._json(self.sync.app_list())

    def m_SetAppEnabled(self, _sender, app, on):
        self.sync.appdata.set_info(app, enabled=bool(on))
        self.sync.emit("AppsChanged")
        self.wake.set()
        return None

    def m_DeleteAppData(self, sender, app):
        self._require(sender, MANAGE_ACTION)

        target = appdata.targets(self.sync.hub.device).get(app)
        if target is None:
            raise SyncStateError("unknown_app", "That app isn't on this device.")
        with self.sync.exclusive():
            self.sync.appdata.delete_synced(target)
        self.sync.emit("AppsChanged")
        return None

    def m_ApplyPending(self, _sender, app):
        """Called by a launcher before it starts an app: bring the newest state in now if there is one."""
        if self.sync.watcher.is_running(app):
            return GLib.Variant("(b)", (False,))
        self.sync.watcher.requested.add(app)
        try:
            self.sync.cycle(only_app=app)
        finally:
            self.sync.watcher.requested.discard(app)
        return GLib.Variant("(b)", (self.sync.appdata.info(app).get("state") == "synced",))

    def m_Quiesce(self, _sender, app):
        """An app that never exits asks for a snapshot now, while it is idle (ADR-054 §4.3)."""
        self.sync.watcher.requested.add(app)
        self.wake.set()
        return None

    # Devices
    def m_ListDevices(self, _sender):
        return self._json(self.sync.devices())

    def m_RemoveDevice(self, sender, device, new_recovery_key):
        """Remove a device; with new_recovery_key, also replace the recovery key (returns the new words, else [])."""
        self._require(sender, MANAGE_ACTION)
        with self.sync.exclusive():
            _epoch, words = self.sync.account.revoke(device, new_recovery=bool(new_recovery_key))
        self.sync.emit("StateChanged")
        # Words come back whenever the recovery key was replaced, including automatically because the device held it.
        return GLib.Variant("(as)", (words or [],))

    def m_DeleteAllSyncedData(self, sender, words):
        """Everything synced for the account, from the Hub. The Hub asks for the recovery key (review C2.4)."""
        self._require(sender, MANAGE_ACTION)
        with self.sync.exclusive():
            self.sync.account.delete_everything(crypto.normalise_words(words))
        self.sync.emit("StateChanged")
        return None

    # Restore
    def m_GetRestore(self, _sender):
        return self._json(self.sync.restore())

    def m_GetAppState(self, _sender, app):
        row = next((r for r in self.sync.restore() if r["app"] == app), None)
        info = self.sync.appdata.info(app)
        return self._json(row or {"app": app, "state": "ready" if info.get("state") == "synced" else info.get("state", "none"),
                                  "progress": info.get("progress")})

    def m_ListConflicts(self, _sender):
        """{recent: conflicts merged on this device, versions: {app: [labelled siblings kept in the app's zone]}}"""
        versions = {}
        for app, target in appdata.targets(self.sync.hub.device).items():
            kept = self.sync.appdata.siblings(target)
            if kept:
                versions[app] = [{k: v for k, v in entry.items() if k != "file"} for entry in kept]
        return self._json({"recent": self.sync.local.get("conflicts", []), "versions": versions})

    # ------------------------------------------------------------ callers and polkit
    def _pid(self, sender: str) -> int:
        try:
            return int(self.connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "GetConnectionUnixProcessID",
                                                 GLib.Variant("(s)", (sender,)), GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 5000, None).unpack()[0])
        except Exception:  # noqa: BLE001
            return 0

    def _require(self, sender: str, action: str) -> None:
        if not self._authorized(sender, action):
            raise SyncStateError("not_authorized", "Your password is needed for this.")

    def _authorized(self, sender: str, action: str) -> bool:
        try:
            pid = self._pid(sender)
            if not pid:
                return False
            start = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
            system = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            subject = ("unix-process", {"pid": GLib.Variant("u", pid), "start-time": GLib.Variant("t", start), "uid": GLib.Variant("i", os.getuid())})
            result = system.call_sync("org.freedesktop.PolicyKit1", "/org/freedesktop/PolicyKit1/Authority", "org.freedesktop.PolicyKit1.Authority",
                                      "CheckAuthorization", GLib.Variant("((sa{sv})sa{ss}us)", (subject, action, {}, 1, "")),
                                      GLib.VariantType("((bba{ss}))"), Gio.DBusCallFlags.NONE, 120000, None).unpack()[0]
            return bool(result[0])
        except Exception:  # noqa: BLE001 - no polkit, no answer: refuse
            return False


def _camel(code: str) -> str:
    return "".join(part.capitalize() for part in code.replace("-", "_").split("_")) or "Failed"


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        Service().run()
    except SyncStateError as error:
        log.error("%s", error)
        return 1
    return 0
