# SPDX-License-Identifier: Apache-2.0
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from luma_installer import (app_identity, app_permissions, device_requests, flatpak_permissions, native_messaging,
                            package_access, permission_views, refusal_watch)
from luma_installer.app_permissions import MemoryStore, Permissions, suggest

APP = "org.projectluma.Installed.deb-player-0123456789ab"


class Home:
    def __init__(self, *folders):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        for name in folders:
            (self.path / name).mkdir()

    def __enter__(self):
        return self.path

    def __exit__(self, *_):
        self.directory.cleanup()


class SuggestedLevel(unittest.TestCase):
    def test_kinds(self):
        with mock.patch.object(app_permissions, "_xdg_folder", side_effect=lambda name: name):
            self.assertEqual(suggest(["AudioVideo", "Audio", "Player"])["folders"], ["Music"])
            self.assertEqual(suggest(["Graphics", "RasterGraphics", "Photography"])["folders"], ["Pictures"])
            self.assertEqual(suggest(["Office", "WordProcessor"])["folders"], ["Documents"])
            self.assertEqual(suggest(["AudioVideo", "Video", "Player"])["folders"], ["Videos"])
            self.assertEqual(suggest(["Network", "FileTransfer", "P2P"])["folders"], ["Downloads"])
            self.assertEqual(suggest(["System", "FileManager"])["level"], "all")
            self.assertEqual(suggest(["Utility", "Network", "FileTransfer"])["level"], "all")  # sync clients
            self.assertEqual(suggest(["Utility", "Backup"])["level"], "all")
            self.assertEqual(suggest(["Utility", "TextEditor"])["level"], "chooser")
            self.assertEqual(suggest([])["level"], "chooser")
            self.assertTrue(suggest([])["suggested"])

    def test_declared(self):
        self.assertEqual(suggest(declared={"filesystems": ["home"]})["level"], "all")
        self.assertEqual(suggest(declared={"filesystems": ["host:ro"]})["level"], "all")
        with mock.patch.object(app_permissions, "_xdg_folder", side_effect=lambda name: name):
            found = suggest(declared={"filesystems": ["xdg-music:ro", "xdg-download"]})
        self.assertEqual((found["level"], found["folders"]), ("folders", ["Music", "Downloads"]))


class Grants(unittest.TestCase):
    def model(self, home, applied=None):
        return Permissions(MemoryStore(), home=home, apply=(applied.append if applied is not None else None))

    def test_command_line_install_does_not_wait_and_asks_for_review(self):
        with Home("Music", "Documents") as home:
            model = self.model(home)
            model.install(APP, app_permissions.plan(categories=["Audio", "Player"]), review=True)
            self.assertEqual(model.level(APP), "folders")
            self.assertEqual(model.granted_folders(APP), {"Music"})
            self.assertTrue(model.review_pending(APP))
            self.assertEqual(model.review_apps(), [APP])
            model.finish_review(APP)
            self.assertFalse(model.review_pending(APP))

    def test_refusal_offered_once_and_not_now_remembered(self):
        with Home("Music", "Documents", "Pictures") as home:
            model = self.model(home)
            model.install(APP, {"files": {"level": "folders", "folders": ["Music"]}})
            self.assertTrue(model.refused(APP, "Documents"))
            self.assertFalse(model.refused(APP, "Documents"))
            self.assertFalse(model.refused(APP, "Music"))  # granted: no offer
            self.assertFalse(model.refused(APP, ".ssh"))  # never offered
            model.answer_offer(APP, "Pictures", False)
            self.assertFalse(model.refused(APP, "Pictures"))
            notes = {row["what"]: row["note"] for row in model.recent(APP)}
            self.assertEqual(notes["Documents"], "refused, offered once")
            self.assertEqual(notes["Pictures"], "refused, you said Not now")

    def test_allow_applies_to_the_running_app_and_moves_picker_to_folders(self):
        with Home("Music", "Documents") as home:
            applied: list[str] = []
            model = self.model(home, applied)
            model.install(APP, {"files": {"level": "chooser"}})
            applied.clear()
            model.answer_offer(APP, "Documents", True)
            self.assertEqual(model.level(APP), "folders")
            self.assertEqual(model.granted_folders(APP), {"Documents"})
            self.assertEqual(applied, [APP])  # the running app's view is changed, not restarted
            model.set_folder(APP, "Documents", False)
            self.assertEqual(model.granted_folders(APP), set())

    def test_all_files_list_and_turning_one_off(self):
        with Home("Music") as home:
            model = self.model(home)
            model.install("org.example.Files", {"files": {"level": "all"}})
            self.assertEqual(model.all_files_apps(), ["org.example.Files"])
            self.assertEqual(model.granted_folders("org.example.Files"), {"Music"})
            model.set_level("org.example.Files", "chooser")
            self.assertEqual(model.all_files_apps(), [])

    def test_device_request_on_and_off(self):
        with Home() as home:
            model = self.model(home)
            request = {"name": "USB lighting", "requests": ["lighting:1d50:6089"], "default": True}
            model.install(APP, {"files": {"level": "chooser"}, "devices": {**request, "granted": False}})
            self.assertEqual(model.devices(APP), [])
            self.assertEqual(model.device_requests(APP), ["lighting:1d50:6089"])
            model.set_device_requests(APP, True)
            self.assertEqual(model.devices(APP), ["lighting:1d50:6089"])
            self.assertEqual([row["kind"] for row in model.device_states(APP)], ["lighting"])
            model.set_device(APP, "lighting", False)
            self.assertEqual(model.devices(APP), [])

    def test_portal_devices_share_xdg_desktop_portal_tables(self):
        with Home() as home:
            model = self.model(home)
            model.set_device(APP, "camera", False)
            self.assertEqual(model.store.lookup("devices", "camera"), {APP: ["no"]})
            model.set_background(APP, True)
            self.assertEqual(model.store.lookup("background", "background"), {APP: ["yes"]})

    def test_stray_documents(self):
        with Home() as data:
            (data / ".config").mkdir()
            (data / ".config/settings.ini").write_text("x")
            (data / "Report.odt").write_text("x")
            (data / "Exports").mkdir()
            (data / "Exports/photo.png").write_text("x")
            self.assertEqual(sorted(app_permissions.stray_documents(data)), ["Exports/photo.png", "Report.odt"])
            with Home() as target:
                moved = app_permissions.move_stray_documents(data, "Kiln", target / "Kiln")
                self.assertEqual(sorted(Path(path).name for path in moved), ["Report.odt", "photo.png"])
                self.assertEqual(app_permissions.stray_documents(data), [])


class Identity(unittest.TestCase):
    """The same answers as Mutter 0041 (meta-wayland-client-sandbox.c)."""

    def test_level_names_the_installer_record_then_the_store(self):
        records = {"deb-player-0123": "s0:c1,c2", "rpm-x-1": "s0:c3,c4"}
        self.assertEqual(app_identity.resolve("s0:c3,c4", None, records=records, categories={}),
                         "org.projectluma.Installed.rpm-x-1")
        self.assertEqual(app_identity.resolve("s0:c9,c10", None, records=records, categories={"old": "s0:c9,c10"}),
                         "org.projectluma.Installed.old")
        self.assertEqual(app_identity.resolve("s0:c7,c8", None, records=records, categories={}), "")

    def test_scope_only_when_one_app_matches(self):
        records = {"deb-player-0123": "", "deb.player.0123": ""}
        self.assertEqual(app_identity.resolve(None, "app-luma-deb_player_0123-42.scope", records=records, categories={}), "")
        self.assertEqual(app_identity.resolve(None, "app-luma-rpm_x-7.scope", records={"rpm-x": ""}, categories={}),
                         "org.projectluma.Installed.rpm-x")
        self.assertEqual(app_identity.resolve(None, "app-gnome-foo-7.scope", records={"foo": ""}, categories={}), "")

    def test_label(self):
        self.assertEqual(app_identity.label_level("system_u:system_r:luma_application.process:s0:c1,c2\x00"), "s0:c1,c2")
        self.assertIsNone(app_identity.label_level("unconfined_u:unconfined_r:unconfined_t:s0"))


class Refusals(unittest.TestCase):
    def test_parse(self):
        avc = ('audit(1795314309.311:2375): avc:  denied  { read } for  pid=8768 comm="bash" name="Documents" dev="tmpfs" '
               'ino=337 scontext=system_u:system_r:luma_application.process:s0:c7,c8 '
               'tcontext=unconfined_u:object_r:luma_application.refused:s0 tclass=dir permissive=0')
        self.assertEqual(refusal_watch.parse(avc), {"pid": "8768", "folder": "Documents", "level": "s0:c7,c8",
                                                    "permissions": "read"})
        self.assertIsNone(refusal_watch.parse(avc.replace("luma_application.refused", "user_home_t")))
        self.assertIsNone(refusal_watch.parse(avc.replace('name="Documents"', 'name=".ssh"')))


class Views(unittest.TestCase):
    def test_every_visible_folder_and_no_hidden_one(self):
        with Home("Music", "Documents", ".ssh", ".gnupg", "Projects") as home:
            (home / "notes.txt").write_text("x")
            self.assertEqual(permission_views.folders(home), ["Documents", "Music", "Projects"])
            view = permission_views.View(APP, "1", {"Music"}, root=home)
            with mock.patch.object(permission_views, "stub", side_effect=lambda name: Path("/stub") / name):
                binds = dict((destination, source) for source, destination in view.static_binds())
            self.assertEqual(binds[str(home / "Music")], str(home / "Music"))
            self.assertEqual(binds[str(home / "Documents")], "/stub/Documents")
            self.assertNotIn(str(home / ".ssh"), binds)


class Flatpak(unittest.TestCase):
    def test_levels(self):
        self.assertEqual(flatpak_permissions.level(["home"]), ("all", []))
        self.assertEqual(flatpak_permissions.level(["xdg-music:ro", "xdg-documents/Work"]), ("folders", ["Music", "Documents"]))
        self.assertEqual(flatpak_permissions.level(["xdg-run/dconf"]), ("chooser", []))

    def test_home_is_narrowed_only_by_protected_paths(self):
        arguments = flatpak_permissions.exclusions("org.example.Editor", ["home"])
        self.assertIn("--nofilesystem=~/.ssh", arguments)
        self.assertIn("--filesystem=~/.bashrc:ro", arguments)
        self.assertNotIn("--nofilesystem=home", arguments)
        self.assertNotIn("--nofilesystem=xdg-documents", arguments)
        host = flatpak_permissions.exclusions("org.example.Files", ["host"])
        self.assertIn("--nofilesystem=host", host)
        self.assertIn("--filesystem=home", host)
        self.assertEqual(flatpak_permissions.exclusions("org.example.Player", ["xdg-music"]), [])
        self.assertEqual(flatpak_permissions.exclusions("org.projectluma.Depot", ["home"]), [])

    def test_never_narrower_than_the_bundle(self):
        calls = []
        with Home() as data, mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(data)}):
            run = lambda command, **_: (calls.append(command), mock.Mock(returncode=0, stdout="", stderr=""))[1]  # noqa: E731
            arguments = flatpak_permissions.apply("org.example.Editor", "chooser", [], runner=run, declared=["home"])
        self.assertIn("--filesystem=home", arguments)
        self.assertTrue(any("--nofilesystem=~/.ssh" in command for command in calls))


class Devices(unittest.TestCase):
    def test_rules_become_one_request(self):
        summary = device_requests.read_rules({
            "lib/udev/rules.d/60-openrgb.rules": 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="1532", TAG+="uaccess"\n'
                                                 'SUBSYSTEM=="i2c-dev", MODE="0666"\n',
            "lib/udev/rules.d/70-pad.rules": 'KERNEL=="js*", SUBSYSTEM=="input", TAG+="uaccess"\n'
                                             'KERNEL=="ttyACM[0-9]*", MODE="0666"\n# comment\n'
                                             'ACTION=="add", RUN+="/bin/true"\n'})
        self.assertEqual(summary.kinds, ["lighting", "controller", "serial"])
        self.assertEqual(summary.as_dict()["count"], 3)
        self.assertNotIn(":", summary.name())  # never vendor IDs

    def test_nodes_match_vendor(self):
        with Home() as root:
            sys_root, dev_root = root / "sys", root / "dev"
            device = sys_root / "devices/usb1/1-1"
            (device / "0003:1D50:6089.0001/hidraw/hidraw3").mkdir(parents=True)
            (device / "idVendor").write_text("1d50\n")
            (device / "idProduct").write_text("6089\n")
            (sys_root / "class/hidraw").mkdir(parents=True)
            (sys_root / "class/hidraw/hidraw3").symlink_to(device / "0003:1D50:6089.0001/hidraw/hidraw3")
            (device / "0003:1D50:6089.0001/hidraw/hidraw3/device").symlink_to(device / "0003:1D50:6089.0001")
            dev_root.mkdir()
            (dev_root / "hidraw3").write_text("")
            wanted = [device_requests.parse_token("lighting:1d50:6089")]
            self.assertEqual(device_requests.nodes(wanted, sys_root, dev_root), [str(dev_root / "hidraw3")])
            other = [device_requests.parse_token("hid:046d:c52b")]
            self.assertEqual(device_requests.nodes(other, sys_root, dev_root), [])


class Broker(unittest.TestCase):
    def test_host_starts_in_its_sandbox_with_the_connection_as_stdio(self):
        host = {"app": "org.projectluma.Installed.vault", "application_id": "deb-vault", "path": "/usr/bin/host",
                "allowed_origins": ["chrome-extension://abc/"]}
        started = []

        def launch(command, connection):
            started.append(command)
            reply = json.dumps({"ok": True}).encode()
            connection.sendall(struct.pack("=I", len(reply)) + reply)
            return mock.Mock(wait=lambda: 0)

        broker = native_messaging.Broker(launch=launch, identify=lambda pid: ("com.rhyme.viola", "flatpak"), peer=lambda _c: 1)
        with mock.patch.object(native_messaging, "load", return_value={"hosts": {"org.luma.vault": host}, "browsers": {}}):
            ours, theirs = socket.socketpair()
            ours.sendall(json.dumps({"host": "org.luma.vault", "origin": "chrome-extension://abc/"}).encode() + b"\n")
            thread = threading.Thread(target=broker.handle, args=("com.rhyme.viola", theirs))
            thread.start()
            thread.join(5)
            length = struct.unpack("=I", ours.recv(4))[0]
            self.assertEqual(json.loads(ours.recv(length)), {"ok": True})
            self.assertEqual(started[0][-3:], ["--", "/usr/bin/host", "chrome-extension://abc/"])
            # Another origin, or a caller that is not the browser, starts nothing.
            started.clear()
            for origin, caller in (("chrome-extension://evil/", "com.rhyme.viola"), ("chrome-extension://abc/", "org.example.Other")):
                broker.identify = lambda pid, caller=caller: (caller, "flatpak")
                ours, theirs = socket.socketpair()
                ours.sendall(json.dumps({"host": "org.luma.vault", "origin": origin}).encode() + b"\n")
                self.assertFalse(broker.handle("com.rhyme.viola", theirs))
            self.assertEqual(started, [])

    def test_manifests_in_tree(self):
        with Home() as root:
            folder = root / "etc/opt/chrome/native-messaging-hosts"
            folder.mkdir(parents=True)
            (folder / "x.json").write_text(json.dumps({"name": "org.x", "path": "/usr/bin/x", "type": "stdio",
                                                       "allowed_origins": ["chrome-extension://abc/"]}))
            (folder / "bad.json").write_text("{")
            self.assertEqual([found["name"] for found in native_messaging.manifests_in_tree(root)], ["org.x"])


class Scope(unittest.TestCase):
    def test_launcher_joins_the_application_scope_before_its_proxy_starts(self):
        from luma_installer import closure_launcher
        calls = []
        unit = f"app-luma-deb_x-{os.getpid()}.scope"
        with mock.patch.object(closure_launcher.shutil, "which", return_value="/usr/bin/busctl"), \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/1000"}), \
                mock.patch.object(closure_launcher.subprocess, "run",
                                  side_effect=lambda command, **_: (calls.append(command), mock.Mock(returncode=0))[1]), \
                mock.patch.object(closure_launcher.Path, "read_text", return_value=f"0::/app.slice/{unit}\n"):
            self.assertTrue(closure_launcher.enter_scope("deb-x"))
        self.assertIn(unit, calls[0])
        self.assertIn(str(os.getpid()), calls[0])


class InstallTransaction(unittest.TestCase):
    def test_plan_from_a_package_tree(self):
        with Home() as root:
            (root / "usr/share/applications").mkdir(parents=True)
            (root / "usr/share/applications/p.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Player\nCategories=AudioVideo;Audio;Player;\n")
            (root / "usr/lib/udev/rules.d").mkdir(parents=True)
            (root / "usr/lib/udev/rules.d/60-pad.rules").write_text('SUBSYSTEM=="input", KERNEL=="js*", TAG+="uaccess"\n')
            with mock.patch.object(app_permissions, "_xdg_folder", side_effect=lambda name: name):
                found = package_access.plan_for_tree(root)
            self.assertEqual(found["files"]["level"], "folders")
            self.assertEqual(found["files"]["folders"], ["Music"])
            self.assertEqual(found["devices"]["name"], "Game controllers")
            self.assertTrue(found["devices"]["default"])
            self.assertTrue(found["never"])


if __name__ == "__main__":
    unittest.main()
