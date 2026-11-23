# SPDX-License-Identifier: Apache-2.0
"""org.projectluma.ConnectSync1 on a private session bus, against the fake Hub:
turn on, join by code with the approval card's signals, the recovery key
behind polkit, and the state, device and app lists the UI reads."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

try:
    import gi
    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib
except (ImportError, ValueError):
    Gio = None

from fake_hub import FakeHub, serve  # noqa: E402
from harness import enrol_fake  # noqa: E402

CHILD = r"""
import os, sys
sys.path.insert(0, os.environ["LCS_SOURCE"])
from luma_connect_sync import service
# The test talks from outside any session scope; the caller rules have their own tests (test_review.M4Callers).
service.trusted_caller = lambda pid, proc=None: True
service.Service().run()
"""


def write_registration(home: Path, url: str, device: str, token: str, name: str) -> None:
    folder = home / ".local/share/luma/connect"
    folder.mkdir(parents=True, mode=0o700)
    os.chmod(home / ".local/share/luma", 0o700)
    os.chmod(folder, 0o700)
    path = folder / "device.json"
    path.write_text(json.dumps({"device_id": device, "token": token, "hub": url, "name": name}))
    os.chmod(path, 0o600)


@unittest.skipIf(Gio is None or not shutil.which("dbus-daemon"), "needs GLib and a D-Bus daemon")
class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="lcs-service-"))
        self.fake = FakeHub()
        self.server = serve(self.fake)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.buses: list[subprocess.Popen] = []
        self.children: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for process in self.children + self.buses:
            process.kill()
            process.wait()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def device(self, name: str):
        home = self.root / name
        home.mkdir()
        device, token = enrol_fake(self.url, "owner", name)
        write_registration(home, self.url, device, token, name)
        bus = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1", "--nopidfile"], stdout=subprocess.PIPE, text=True)
        address = bus.stdout.readline().strip()
        self.buses.append(bus)
        env = {**os.environ, "HOME": str(home), "XDG_DATA_HOME": str(home / ".local/share"), "XDG_STATE_HOME": str(home / ".local/state"),
               "XDG_RUNTIME_DIR": str(home / "run"), "DBUS_SESSION_BUS_ADDRESS": address, "LCS_SOURCE": str(HERE.parent),
               "LUMA_CONNECT_SYNC_KEYSTORE": f"file:{home / 'keys'}", "PATH": "/usr/bin:/bin"}
        (home / "run").mkdir(mode=0o700)
        child = subprocess.Popen([sys.executable, "-c", CHILD], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.children.append(child)
        connection = Gio.DBusConnection.new_for_address_sync(address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None)
        for _ in range(300):        # a loaded build machine can take a while to start Python and GLib
            try:
                connection.call_sync("org.projectluma.ConnectSync1", "/org/projectluma/ConnectSync1", "org.freedesktop.DBus.Peer", "Ping", None, None, Gio.DBusCallFlags.NONE, 1000, None)
                break
            except GLib.Error:
                time.sleep(0.1)
        return connection, device

    def approve_directly(self, name: str, join_id: str, code: str) -> None:
        """What the CLI does as the person, outside any sandbox."""
        from harness import make_device
        home = self.root / name
        registration = json.loads((home / ".local/share/luma/connect/device.json").read_text())
        device = make_device(name, self.url, registration["device_id"], registration["token"], self.root / f"{name}-cli")
        device.keystore.root = home / "keys"
        device.account.approve(join_id, code)

    @staticmethod
    def call(connection, method: str, args=None, reply: str | None = None):
        result = connection.call_sync("org.projectluma.ConnectSync1", "/org/projectluma/ConnectSync1", "org.projectluma.ConnectSync1", method,
                                      args, GLib.VariantType(reply) if reply else None, Gio.DBusCallFlags.NONE, 60000, None)
        return result.unpack() if result is not None else None

    def test_turn_on_join_and_lists(self) -> None:
        a, a_id = self.device("Desktop")
        b, b_id = self.device("Laptop")
        words, check = self.call(a, "BeginSetup", None, "(asai)")
        self.assertEqual(len(words), 24)
        self.assertEqual(len(check), 3)
        with self.assertRaises(GLib.Error) as wrong:
            self.call(a, "FinishSetup", GLib.Variant("(a{is})", ({i: "nope" for i in check},)))
        self.assertIn("CheckFailed", wrong.exception.message)
        self.call(a, "FinishSetup", GLib.Variant("(a{is})", ({i: words[i - 1] for i in check},)))
        with self.assertRaises(GLib.Error) as refused:
            self.call(a, "RevealRecoveryKey", None, "(as)")
        self.assertIn("NotAuthorized", refused.exception.message, "no polkit agent here: the key stays hidden")

        cards: list[tuple] = []
        a.signal_subscribe(None, "org.projectluma.ConnectSync1", "JoinRequested", "/org/projectluma/ConnectSync1", None,
                           Gio.DBusSignalFlags.NONE, lambda *args: cards.append(args[5].unpack()))
        view = json.loads(self.call(b, "RequestJoin", None, "(s)")[0])
        self.assertIn(view["state"], {"waiting", "code"})
        self.assertEqual([x["name"] for x in view["approvers"]], ["Desktop"])
        self.call(a, "SyncNow")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            view = json.loads(self.call(b, "GetJoin", None, "(s)")[0])
            if view["state"] == "code":
                break
            self.call(a, "SyncNow")
            time.sleep(0.3)
        self.assertEqual(view["state"], "code")
        requests = []
        while time.monotonic() < deadline and not (requests and requests[0]["code"]):
            requests = json.loads(self.call(a, "ListJoinRequests", None, "(s)")[0])
            time.sleep(0.2)
        self.assertEqual(requests[0]["code"], view["code"])
        # Approving needs the person's password (polkit); with no agent here the service refuses.
        with self.assertRaises(GLib.Error) as refused:
            self.call(a, "ApproveJoin", GLib.Variant("(ss)", (requests[0]["id"], view["code"])))
        self.assertIn("NotAuthorized", refused.exception.message)
        self.assertEqual(json.loads(self.call(b, "GetJoin", None, "(s)")[0])["state"], "code", "nothing was approved")
        self.approve_directly("Desktop", requests[0]["id"], view["code"])
        self.assertEqual(json.loads(self.call(b, "GetJoin", None, "(s)")[0])["state"], "code", "taken only after the person confirms here")
        self.assertEqual(json.loads(self.call(b, "ConfirmJoin", GLib.Variant("(b)", (True,)), "(s)")[0])["state"], "approved")
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

        state = json.loads(self.call(a, "GetState", None, "(s)")[0])
        self.assertIn(state["state"], {"up_to_date", "syncing"})
        self.assertEqual(set(state["categories"]), {"apps", "app_data", "settings", "appearance", "wallpaper", "keyboard", "wifi", "passwords", "browser"})
        self.assertFalse(state["categories"]["wallpaper"], "same wallpaper everywhere is off by default")
        devices = json.loads(self.call(b, "ListDevices", None, "(s)")[0])
        self.assertEqual(sorted((d["name"], d["this_device"]) for d in devices), [("Desktop", False), ("Laptop", True)])
        self.assertEqual({d["class"] for d in devices}, {"computer"})
        self.call(a, "SetCategory", GLib.Variant("(sb)", ("wifi", False)))
        self.assertFalse(json.loads(self.call(a, "GetState", None, "(s)")[0])["categories"]["wifi"])
        self.assertIsInstance(json.loads(self.call(a, "ListApps", None, "(s)")[0]), list)
        self.assertIsInstance(json.loads(self.call(a, "GetRestore", None, "(s)")[0]), list)
        # Removing a device, deleting synced data and turning Sync off need the password too.
        for method, args in (("RemoveDevice", GLib.Variant("(sb)", (b_id, False))), ("DeleteAppData", GLib.Variant("(s)", ("org.example.App",))),
                             ("SetEnabled", GLib.Variant("(b)", (False,)))):
            with self.assertRaises(GLib.Error) as refused:
                self.call(a, method, args)
            self.assertIn("NotAuthorized", refused.exception.message, method)
        self.assertEqual(self.fake.accounts["owner"]["epoch"], 1)
        self.call(a, "SetEnabled", GLib.Variant("(b)", (True,)))


if __name__ == "__main__":
    unittest.main()
