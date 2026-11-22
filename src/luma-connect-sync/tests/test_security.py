# SPDX-License-Identifier: Apache-2.0
"""The five fixes from the architecture review (ARCHITECTURE.md §10), one test each or more."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from harness import OWNER, tree
from test_sync import Base

from luma_connect_sync import appdata, crypto
from luma_connect_sync.account import SyncStateError


def target(app: str) -> appdata.Target:
    return appdata.Target(app, "flatpak", Path("/nonexistent") / app, app, owner=OWNER)


def mine(target: appdata.Target, manifest: dict) -> dict:
    return {"v": 2, "app": target.app, "owner": target.owner, **manifest}


class SandboxedCallers(unittest.TestCase):
    """1. Sandboxed apps never reach the service (the polkit half is in test_service)."""

    def proc(self, pid: int, *, cgroup: str = "0::/user.slice/user-1000.slice/session-2.scope\n", flatpak: bool = False,
             context: str = "unconfined_u:unconfined_r:unconfined_t:s0-s0:c0.c1023") -> Path:
        root = Path(tempfile.mkdtemp())
        base = root / str(pid)
        (base / "root").mkdir(parents=True)
        (base / "attr").mkdir()
        (base / "cgroup").write_text(cgroup)
        (base / "attr/current").write_text(context + "\0")
        if flatpak:
            (base / "root/.flatpak-info").write_text("[Application]\nname=org.example.Evil\n")
        return root

    def test_classifies_callers(self) -> None:
        from luma_connect_sync.service import sandboxed
        self.assertEqual(sandboxed(7, self.proc(7)), "", "the person's own session")
        self.assertEqual(sandboxed(7, self.proc(7, flatpak=True)), "flatpak")
        self.assertEqual(sandboxed(7, self.proc(7, cgroup="0::/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.example.Evil-123.scope\n")), "flatpak")
        self.assertEqual(sandboxed(7, self.proc(7, cgroup="0::/user.slice/user@1000.service/app.slice/app-luma-deb_x_1-99.scope\n")), "luma-app")
        self.assertEqual(sandboxed(7, self.proc(7, context="system_u:system_r:luma_application.process:s0:c12,c34")), "luma-app")
        self.assertEqual(sandboxed(8, self.proc(7)), "unreadable", "a caller that can't be identified is refused")


class DconfFence(unittest.TestCase):
    """2. An app's state carries only its own dconf path."""

    def test_reserved_prefixes(self) -> None:
        self.assertEqual(appdata.allowed_dconf("org.gnome.TextEditor"), {"/org/gnome/TextEditor/", "/org/gnome/texteditor/"})
        for app in ("org.gnome.desktop", "org.gnome.desktop.screensaver", "org.gnome", "org.freedesktop.Foo", "org.projectluma.Notes",
                    "ca.desrt.dconf", "org.gnome.Shell", "org", "bad..id", "org.example.x/../y"):
            self.assertEqual(appdata.allowed_dconf(app), set(), app)

    def test_incoming_manifest_paths_are_filtered(self) -> None:
        editor, desktop = target("org.gnome.TextEditor"), target("org.gnome.desktop")
        manifest = {"files": {}, "dconf": {"/org/gnome/TextEditor/": "[/]\nx=1\n", "/org/gnome/desktop/screensaver/": "[/]\nlock-enabled=false\n",
                                           "/": "[/]\n"}}
        self.assertEqual(appdata.check_manifest(editor, mine(editor, manifest))["dconf"], {"/org/gnome/TextEditor/": "[/]\nx=1\n"})
        self.assertEqual(appdata.check_manifest(desktop, mine(desktop, {**manifest, "dconf": {"/org/gnome/desktop/": "x"}}))["dconf"], {})


class PathRules(unittest.TestCase):
    """4. Manifest paths stay inside the app's folder; symlinks are never followed out of it."""

    def test_rules(self) -> None:
        for bad in ("", "/etc/passwd", "../x", "a/../../x", "a//b", "./a", "a/.", "a\0b"):
            self.assertFalse(appdata.safe_relative(bad), repr(bad))
        self.assertTrue(appdata.safe_relative("data/keyrings/default.keyring"))
        self.assertTrue(appdata.is_excluded("data/keyrings/default.keyring", appdata.excludes()), "sign-ins travel as items, never as the file")
        self.assertFalse(appdata.safe_link("data/l", "/etc/shadow"))
        self.assertFalse(appdata.safe_link("data/l", "../../x"))
        self.assertFalse(appdata.safe_link("data/l", "../config/x"), "no '..' in a link at all (review L1)")
        self.assertTrue(appdata.safe_link("data/l", "config/x"))
        app = target("org.example.App")
        with self.assertRaises(appdata.SnapshotAborted):
            appdata.check_manifest(app, mine(app, {"files": {"../escape": {"t": "f"}}}))
        with self.assertRaises(appdata.SnapshotAborted):
            appdata.check_manifest(app, mine(app, {"files": {"l": {"t": "l", "to": "data"}, "l/x": {"t": "f"}}}))
        with self.assertRaises(appdata.SnapshotAborted):
            appdata.check_manifest(app, mine(app, {"files": {"dev": {"t": "c"}}}))
        kept = appdata.check_manifest(app, mine(app, {"files": {"out": {"t": "l", "to": "/home/x/.ssh"}, "in": {"t": "l", "to": "data/a"}}}))
        self.assertEqual(set(kept["files"]), {"in"}, "a link out of the folder is dropped")
        # Review L1: a chain of links that each look harmless.
        chain = appdata.check_manifest(app, mine(app, {"files": {"d1/d2/s": {"t": "l", "to": "../.."}, "d1/d2/l": {"t": "l", "to": "s/../../../x"}}}))
        self.assertEqual(chain["files"], {})


class Applied(Base):
    def setUp(self) -> None:
        super().setUp()
        self.a, self.b = self.device("Desktop"), self.device("Laptop")
        self.a.account.setup(self.a.account.begin_setup().entropy)
        self.joined(self.a, self.b)

    def test_apply_refuses_a_hostile_manifest_and_snapshot_never_follows_links(self) -> None:
        ta, tb = self.a.target("org.example.Kiln"), self.b.target("org.example.Kiln")
        (ta.folder / "data").mkdir(parents=True)
        (ta.folder / "data/ok.txt").write_text("ok")
        outside = self.root / "outside-secret"
        outside.write_text("never synced")
        os.symlink(outside, ta.folder / "data/escape")
        self.a.appdata.sync_app(ta, quiet=True)
        self.b.appdata.sync_app(tb, quiet=True)
        self.assertEqual(tree(tb.folder), {"data/ok.txt": b"ok"}, "the link out was neither read nor recreated")
        zone = self.a.engine.zone(ta.zone, create=False)["zone"]
        hostile = {"v": 2, "app": ta.app, "owner": ta.owner, "files": {"../../evil": {"t": "f", "s": 1, "mt": 1, "m": 0o644, "h": "0" * 64, "p": []}}, "dconf": {}}
        stored = self.a.appdata.store_manifest(zone, hostile, None)
        with self.assertRaises(appdata.SnapshotAborted):
            self.b.appdata.apply(tb, stored)
        self.assertFalse((self.root / "evil").exists())


class Revocation(Base):
    """5. A removed device can't come back on its own, with its old keys or the recovery words."""

    def test_removed_device_needs_approval_and_a_new_recovery_key_locks_old_words_out(self) -> None:
        a, b = self.device("Desktop"), self.device("Laptop")
        pending = a.account.begin_setup()
        a.account.setup(pending.entropy)
        self.joined(a, b)
        old_keys = b.account.keys().sign_pub
        epoch, words = a.account.revoke(b.hub.device)
        self.assertEqual((epoch, words), (2, None))
        # The laptop signs in again under the same device id and still has the 24 words.
        device, token = __import__("harness").enrol_fake(self.url, self.account, "Laptop", b.hub.device)
        again = __import__("harness").make_device("Laptop", self.url, device, token, self.root / "Laptop")
        with self.assertRaises(SyncStateError) as refused:
            again.account.join_with_recovery(pending.words)
        self.assertEqual(refused.exception.code, "approval_required")
        self.assertNotEqual(again.account.keys().sign_pub, old_keys, "its old keys are never used again")
        self.joined(a, again)
        self.assertEqual(again.account.refresh(), "member")
        # Removing it again, with a new recovery key: the old words open nothing.
        epoch, words = a.account.revoke(again.hub.device, new_recovery=True)
        self.assertEqual(epoch, 3)
        self.assertEqual(len(words), 24)
        fresh = self.device("ThinkPad")
        with self.assertRaises(SyncStateError) as stale:
            fresh.account.join_with_recovery(pending.words)
        self.assertEqual(stale.exception.code, "wrong_key")
        fresh.account.join_with_recovery(words)
        mine = fresh.account.me(fresh.account.state())
        self.assertEqual(mine["joined_via"], "recovery", "members see it joined with the recovery key")
        self.assertEqual(a.account.recovery_words(), words)


if __name__ == "__main__":
    unittest.main()
