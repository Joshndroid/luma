# SPDX-License-Identifier: Apache-2.0
"""Unit and fault tests against the fake Hub (ADR-054 Phase 1, test 1).

Set LUMA_SYNC_TEST_HUB=http://127.0.0.1:PORT and LUMA_SYNC_TEST_ENROL=<url>
to run the same tests against a real local Hub (tests/integration).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
import unittest
import uuid
from pathlib import Path

from harness import enrol_fake, make_device, temporary_root, tree

from luma_connect_sync import appdata, crypto
from luma_connect_sync.account import SyncStateError
from luma_connect_sync.hub import HubError
from fake_hub import FakeHub, serve

CANARY = os.environ.get("LUMA_SYNC_CANARY") or "luma-canary-" + uuid.uuid4().hex


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temporary_root()
        self.account = "acct-" + uuid.uuid4().hex[:12]
        external = os.environ.get("LUMA_SYNC_TEST_HUB")
        if external:
            self.fake, self.server, self.url = None, None, external.rstrip("/")
        else:
            self.fake = FakeHub()
            self.server = serve(self.fake)
            self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def need_fake(self) -> None:
        if self.fake is None:
            self.skipTest("inspects the fake Hub's storage")

    def device(self, name: str, device_id: str | None = None):
        device, token = enrol_fake(self.url, self.account, name, device_id)
        return make_device(name, self.url, device, token, self.root / name)

    def joined(self, approver, newcomer):
        view = newcomer.account.request_join()
        self.assertEqual(view.state, "waiting")
        requests = approver.account.join_requests()       # posts the approvers' half
        self.assertEqual(len(requests), 1)
        view = newcomer.account.join_view()               # reveals the device half
        self.assertEqual(view.state, "code")
        card = approver.account.join_requests()[0]
        self.assertEqual(card["code"], view.code, "both devices show the same code")
        approver.account.approve(card["id"], view.code)
        self.assertEqual(newcomer.account.join_view().state, "code", "nothing is taken before the person confirms here")
        self.assertEqual(newcomer.account.confirm_join(True).state, "approved")
        return view.code


class CryptoTests(unittest.TestCase):
    def test_recovery_words(self) -> None:
        entropy = crypto.new_recovery_entropy()
        words = crypto.recovery_words(entropy)
        self.assertEqual(len(words), 24)
        self.assertEqual(crypto.recovery_entropy(crypto.normalise_words(" ".join(w.upper() for w in words))), entropy)
        swapped = words[:]
        swapped[0], swapped[1] = swapped[1], swapped[0]
        if swapped != words:
            with self.assertRaises(crypto.CryptoError):
                crypto.recovery_entropy(swapped)
        with self.assertRaises(crypto.CryptoError):
            crypto.recovery_entropy(words[:23])

    def test_seal_binds_associated_data(self) -> None:
        key = os.urandom(32)
        sealed = crypto.seal(key, b"hello", b"zone-a")
        self.assertEqual(crypto.open_sealed(key, sealed, b"zone-a"), b"hello")
        with self.assertRaises(crypto.CryptoError):
            crypto.open_sealed(key, sealed, b"zone-b")
        tampered = bytearray(sealed)
        tampered[-1] ^= 1
        with self.assertRaises(crypto.CryptoError):
            crypto.open_sealed(key, bytes(tampered), b"zone-a")

    def test_bundle_chain_and_envelope(self) -> None:
        keys = crypto.DeviceKeys.generate()
        recovery = crypto.recovery_keys(crypto.new_recovery_entropy(), "acct")
        bundle = crypto.Bundle.first("acct", recovery, "dev", keys)
        sealed = crypto.wrap(bundle, "dev", keys.box_pub)
        self.assertEqual(crypto.unwrap(sealed, keys.box, "acct", "dev", 1).ask, bundle.ask)
        with self.assertRaises(crypto.CryptoError):
            crypto.unwrap(sealed, keys.box, "acct", "other", 1)
        nxt = bundle.successor({"dev": keys.entry()}, [], "dev", keys, ["dev"])
        self.assertTrue(nxt.follows(bundle))
        forged = crypto.Bundle.from_bytes(nxt.to_bytes())
        forged.ask = os.urandom(32)
        self.assertFalse(forged.follows(bundle), "a new key not signed as made is refused")
        self.assertEqual(bundle.zone_id("apps"), nxt.zone_id("apps"), "zone ids survive a new account key")


class KeyTests(Base):
    def test_setup_join_deny_recover_revoke(self) -> None:
        a, b, c, d = (self.device(n) for n in ("Desktop", "Laptop", "Phone", "ThinkPad"))
        self.assertEqual(a.account.refresh(), "new")
        pending = a.account.begin_setup()
        with self.assertRaises(SyncStateError):
            a.account.finish_setup(pending, {i: "wrong" for i in pending.check})
        a.account.finish_setup(pending, {i: pending.words[i - 1] for i in pending.check})
        self.assertEqual(a.account.refresh(), "member")
        self.assertEqual(a.account.recovery_words(), pending.words)
        # Approval by code.
        b.account.request_join()
        a.account.join_requests()
        code = b.account.join_view().code
        card = a.account.join_requests()[0]
        with self.assertRaises(SyncStateError):
            a.account.approve(card["id"], "000000" if code != "000000" else "111111")
        a.account.approve(card["id"], code)
        self.assertEqual(b.account.confirm_join(True).state, "approved")
        self.assertEqual(b.account.bundle().ask, a.account.bundle().ask)
        self.assertEqual(a.account.join_requests(), [], "the card clears once answered")
        # Not me.
        c.account.request_join()
        b.account.join_requests()
        c.account.join_view()
        b.account.deny(b.account.join_requests()[0]["id"])
        self.assertEqual(c.account.join_view().state, "denied")
        self.assertIsNone(c.account.bundle())
        # Recovery key.
        wrong = crypto.recovery_words(crypto.new_recovery_entropy())
        with self.assertRaises(SyncStateError) as raised:
            d.account.join_with_recovery(wrong)
        self.assertEqual(str(raised.exception), "That key doesn't match this account")
        d.account.join_with_recovery(pending.words)
        self.assertEqual(d.account.refresh(), "member")
        # Zones written before the revocation stay readable by those who remain.
        a.engine.save("apps", "flatpak:org.example.One", {"kind": "flatpak", "id": "org.example.One"})
        old_ask = a.account.bundle().ask
        epoch, _words = a.account.revoke(b.hub.device)
        self.assertEqual(epoch, 2)
        self.assertNotEqual(a.account.bundle().ask, old_ask)
        self.assertEqual(d.account.refresh(), "member")
        self.assertEqual(d.account.bundle().epoch, 2, "the remaining device took the chained key")
        d.engine.fetch("apps")
        self.assertEqual(d.engine.get("apps", "flatpak:org.example.One")[1]["id"], "org.example.One")
        with self.assertRaises(HubError) as revoked:
            b.account.state()
        self.assertEqual(revoked.exception.status, 401)
        # New writes use the new zone key generation.
        if self.fake is None:
            return
        a.engine.refresh_zones()
        a.engine.save("apps", "flatpak:org.example.Two", {"kind": "flatpak", "id": "org.example.Two"})
        zone = a.engine.zone_id("apps")
        row = next(r for r in self.fake.accounts[self.account]["zones"][zone]["records"].values() if r["version"] == 1 and r["seq"] > 0 and r["generation"] == 2)
        self.assertEqual(row["generation"], 2)

    def test_forged_approver_half_is_refused(self) -> None:
        self.need_fake()
        a, b = self.device("Desktop"), self.device("Laptop")
        pending = a.account.begin_setup()
        a.account.setup(pending.entropy)
        b.account.request_join()
        # The Hub (here: the test) posts its own half before any member does.
        join = next(iter(self.fake.accounts[self.account]["joins"].values()))
        join["approver_nonce"] = crypto.b64(os.urandom(32))
        b.account.join_view()
        card = a.account.join_requests()[0]
        self.assertTrue(card["tampered"])
        with self.assertRaises(SyncStateError):
            a.account.approve(card["id"], b.account.join_view().code)


class DataTests(Base):
    def setUp(self) -> None:
        super().setUp()
        self.a, self.b = self.device("Desktop"), self.device("Laptop")
        pending = self.a.account.begin_setup()
        self.a.account.setup(pending.entropy)
        self.joined(self.a, self.b)

    def fill(self, folder: Path) -> None:
        (folder / "config/glib-2.0/settings").mkdir(parents=True)
        (folder / "config/glib-2.0/settings/keyfile").write_text(f"[org/example/app]\ntheme='dark'\nnote='{CANARY}'\n")
        (folder / "data/documents").mkdir(parents=True)
        (folder / "data/documents/recipe.txt").write_text(f"Bread\n{CANARY}\n")
        (folder / "data/big.bin").write_bytes(os.urandom(6 * 1024 * 1024) + CANARY.encode())
        db = sqlite3.connect(folder / "data/state.db")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE t(v TEXT)")
        db.execute("INSERT INTO t VALUES(?)", (CANARY,))
        db.commit()
        db.close()
        (folder / "cache").mkdir()
        (folder / "cache/thumbs.bin").write_bytes(b"cache")
        (folder / "data/app.lock").write_text("1")
        os.symlink("documents/recipe.txt", folder / "data/latest")

    def test_closed_app_state_round_trip_and_canary(self) -> None:
        ta, tb = self.a.target("org.example.Kiln"), self.b.target("org.example.Kiln")
        self.fill(ta.folder)
        self.assertEqual(self.a.appdata.sync_app(ta, quiet=True), "synced")
        self.assertEqual(self.b.appdata.sync_app(tb, quiet=True), "synced")
        expected = {k: v for k, v in tree(ta.folder).items() if not k.startswith("cache/") and not k.endswith(".lock")}
        self.assertEqual(tree(tb.folder), expected)
        self.assertEqual(os.readlink(tb.folder / "data/latest"), "documents/recipe.txt")
        self.assertFalse((tb.folder / "cache").exists(), "caches never sync")
        if self.fake is not None:
            self.assertNotIn(CANARY.encode(), self.fake.everything(), "the Hub holds no plaintext")
        # A change on A while B is open waits, then arrives at B's next quiet point.
        time.sleep(0.01)
        (ta.folder / "data/documents/recipe.txt").write_text("Bread v2\n")
        self.assertEqual(self.a.appdata.sync_app(ta, quiet=True), "synced")
        self.assertEqual(self.b.appdata.sync_app(tb, quiet=False), "waiting")
        self.assertEqual((tb.folder / "data/documents/recipe.txt").read_text(), f"Bread\n{CANARY}\n")
        self.assertEqual(self.b.appdata.sync_app(tb, quiet=True), "synced")
        self.assertEqual((tb.folder / "data/documents/recipe.txt").read_text(), "Bread v2\n")
        # Nothing changed: nothing is written.
        writes = self.b.hub.requests
        self.assertEqual(self.b.appdata.sync_app(tb, quiet=True), "synced")
        self.assertLess(self.b.hub.requests - writes, 4, "an idle check costs a request or two")

    def test_both_changed_keeps_both(self) -> None:
        ta, tb = self.a.target("org.example.Kiln"), self.b.target("org.example.Kiln")
        self.fill(ta.folder)
        self.a.appdata.sync_app(ta, quiet=True)
        self.b.appdata.sync_app(tb, quiet=True)
        (ta.folder / "data/documents/recipe.txt").write_text("from A\n")
        (ta.folder / "data/only-a.txt").write_text("a\n")
        self.a.appdata.sync_app(ta, quiet=True)
        time.sleep(0.02)
        (tb.folder / "data/documents/recipe.txt").write_text("from B, newer\n")
        (tb.folder / "data/only-b.txt").write_text("b\n")
        self.assertEqual(self.b.appdata.sync_app(tb, quiet=True), "synced")
        self.assertEqual((tb.folder / "data/documents/recipe.txt").read_text(), "from B, newer\n")
        self.assertTrue((tb.folder / "data/only-a.txt").exists() and (tb.folder / "data/only-b.txt").exists())
        kept = list(self.b.appdata.versions_root.rglob("recipe.txt"))
        self.assertEqual([p.read_text() for p in kept], ["from A\n"], "the other side's copy is kept")
        self.a.appdata.sync_app(ta, quiet=True)
        self.assertEqual(tree(ta.folder).get("data/only-b.txt"), b"b\n")
        self.assertEqual((ta.folder / "data/documents/recipe.txt").read_text(), "from B, newer\n")
        self.assertTrue(self.b.local.get("conflicts"))
        siblings = self.a.appdata.siblings(ta)
        self.assertEqual([(v["path"], v["label"].startswith("Version from Desktop")) for v in siblings], [("data/documents/recipe.txt", True)],
                         "the losing copy is a labelled sibling on every device, never silently overwritten")

    def test_opt_out_and_delete(self) -> None:
        ta = self.a.target("org.example.Kiln")
        self.fill(ta.folder)
        self.a.appdata.set_info(ta.app, enabled=False)
        self.assertEqual(self.a.appdata.sync_app(ta, quiet=True), "off")
        self.assertIsNone(self.a.engine.zone(ta.zone, create=False))
        self.a.appdata.set_info(ta.app, enabled=True)
        self.a.appdata.sync_app(ta, quiet=True)
        self.assertIsNotNone(self.a.engine.zone(ta.zone, create=False))
        self.a.appdata.delete_synced(ta)
        self.assertIsNone(self.a.engine.refresh_zones().get(self.a.engine.zone_id(ta.zone)))
        self.assertTrue((ta.folder / "data/documents/recipe.txt").exists(), "local data stays")

    def test_deny_list_and_exclusions(self) -> None:
        self.assertTrue(appdata.denied("org.mozilla.firefox"))
        self.assertFalse(appdata.denied("org.gnome.TextEditor"))
        patterns = appdata.excludes()
        for rel in ("cache/x", ".cache/y", "tmp/z", "data/a.lock", "data/SingletonLock", "config/GPUCache/data_0"):
            self.assertTrue(appdata.is_excluded(rel, patterns), rel)
        self.assertFalse(appdata.is_excluded("data/documents/recipe.txt", patterns))

    def test_apps_zone_union(self) -> None:
        self.a.engine.save("apps", "flatpak:org.example.One", {"kind": "flatpak", "id": "org.example.One", "from": "a"})
        self.b.engine.fetch("apps")
        ok, _ = self.b.engine.save("apps", "flatpak:org.example.Two", {"kind": "flatpak", "id": "org.example.Two", "from": "b"})
        self.assertTrue(ok)
        stale_ok, current = self.a.engine.save("apps", "flatpak:org.example.One", {"kind": "flatpak", "id": "org.example.One", "from": "a", "hold": True})
        self.assertTrue(stale_ok)
        self.a.engine.fetch("apps")
        self.assertEqual(set(self.a.engine.all("apps")), {"flatpak:org.example.One", "flatpak:org.example.Two"})


if __name__ == "__main__":
    unittest.main()
