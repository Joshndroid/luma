# SPDX-License-Identifier: Apache-2.0
"""Re-review proofs of concept (SECURITY-REREVIEW.md N1, C1-oracle, H1-forgery).
Drop beside tests/test_review.py and run: python3 -m unittest test_rereview_poc
Each test FAILS while the weakness exists (it asserts the safe behaviour)."""
from __future__ import annotations

import os

from test_review import Members

from luma_connect_sync import crypto
from luma_connect_sync.account import SyncStateError
from luma_connect_sync.crypto import b64, unb64
from luma_connect_sync.hub import Hub, HubError, Registration


class N1ApproveFetchesTwice(Members):
    """approve() wraps to the keys of its first GET joins, but checks the code on a second GET."""

    def test_approver_never_wraps_to_keys_it_did_not_show(self) -> None:
        self.fake.verify = False                     # the Hub is the attacker; it keeps whatever is posted
        d = self.device("ThinkPad")
        d.account.request_join()
        self.a.account.join_requests()
        code = d.account.join_view().code            # the person reads this on D ...
        card = next(c for c in self.a.account.join_requests() if c["code"])
        self.assertEqual(card["code"], code)         # ... and A shows the same code: honest so far
        evil = crypto.DeviceKeys.generate()
        calls = {"n": 0}

        def rewrite(asking, row):
            if asking != self.a.hub.device:
                return row
            calls["n"] += 1
            # approve(): the first GET is `rows` (used for wrapping), the second is inside join_requests() (used for the code)
            return {**row, "sign_pub": b64(evil.sign_pub), "box_pub": b64(evil.box_pub)} if calls["n"] == 1 else row
        self.fake.rewrite_join = rewrite
        try:
            self.a.account.approve(card["id"], code)
        except SyncStateError:
            return                                   # refused: safe
        sealed = unb64(self.hub["envelopes"][d.hub.device]["ciphertext"])
        try:
            stolen = crypto.unwrap(sealed, evil.box, self.account, d.hub.device, 1)
        except crypto.CryptoError:
            return                                   # not wrapped to the Hub's key: safe
        self.fail(f"the Hub opened the account key bundle (ASK {stolen.ask.hex()[:8]}...) with its own key")


class C1ApproverIsAnOracle(Members):
    """join_requests() posts the approvers' half for every row the Hub serves, with no person involved."""

    def test_approver_does_not_answer_unbounded_rows(self) -> None:
        self.fake.verify = False
        d = self.device("ThinkPad")
        d.account.register()
        rows = []
        for i in range(200):
            nonce = os.urandom(32)
            rows.append({"id": b64(os.urandom(16)), "device": d.hub.device, "commitment": b64(crypto.join_commitment(nonce)),
                         "approver_nonce": None, "device_nonce": None, "answer": None, "name": "ThinkPad", "class": "computer",
                         "sign_pub": b64(d.account.keys().sign_pub), "box_pub": b64(d.account.keys().box_pub), "created_at": 0})
        posted = []
        real_post = self.a.hub.post

        def post(path, value):
            if path.startswith("joins/") and path.endswith("/nonce"):
                posted.append(path)
                return {"id": path.split("/")[1]}
            return real_post(path, value)
        self.a.hub.post = post
        real_get = self.a.hub.get
        self.a.hub.get = lambda path, *a, **k: {"joins": rows} if path == "joins" else real_get(path, *a, **k)
        self.a.account.join_requests()
        self.assertLessEqual(len(posted), 3, f"the approver answered {len(posted)} join rows for one device without anyone looking")


class H1HubForgedAccount(Members):
    """The Hub serves the joining device an approvers' half made under its own account key, then a matching approval."""

    def test_joining_device_refuses_a_hub_made_account(self) -> None:
        self.fake.verify = False
        d = self.device("ThinkPad")
        d.account.request_join()
        join = next(iter(self.hub["joins"].values()))
        m = crypto.DeviceKeys.generate()
        fake = crypto.Bundle.first(self.account, crypto.recovery_keys(crypto.new_recovery_entropy(), self.account), "hub-member", m)
        forged_n_a = fake.join_nonce(join["id"], d.hub.device, unb64(join["commitment"]))
        self.fake.rewrite_join = lambda asking, row: {**row, "approver_nonce": b64(forged_n_a)} if asking == d.hub.device else row
        self.assertEqual(d.account.join_view().state, "code")      # D fixes the Hub's n_a and reveals
        sealed = crypto.wrap(fake, d.hub.device, d.account.keys().box_pub)
        self.hub["envelopes"][d.hub.device] = {"epoch": 1, "wrapper": "hub-member", "ciphertext": b64(sealed),
                                               "signature": b64(m.sign.sign(crypto.envelope_message(self.account, d.hub.device, 1, sealed)))}
        self.hub["devices"][d.hub.device]["state"] = "member"
        try:
            state = d.account.join_view().state
        except SyncStateError:
            return
        self.assertNotEqual(state, "approved", "D joined an account the Hub made, with nobody approving on a real device")


class B3PersonConfirmsOnTheJoiningDevice(Members):
    """B3: a "didn't match" on the joining device ends the join; an approval that arrives later is not taken."""

    def test_mismatch_ends_the_join(self) -> None:
        d = self.device("ThinkPad")
        d.account.request_join()
        self.a.account.join_requests()
        code = d.account.join_view().code
        self.assertEqual(d.account.confirm_join(False).state, "tampered")
        with self.assertRaises(SyncStateError):
            self.a.account.approve(next(iter(self.hub["joins"].values()), {}).get("id", "x"), code)
        self.assertIsNone(d.account.bundle())


class S1NewRecoveryKeyIsShown(Members):
    """S1: a background key change that replaces the recovery key leaves a notice and the new words, never silence."""

    def test_removal_through_connect_leaves_a_notice(self) -> None:
        self.b.account.members()                          # a normal cycle on b
        self.b.hub.revoke(self.a.hub.device)              # the person removes the setup device on the web
        self.assertEqual(self.b.account.refresh(), "member")
        notice = self.b.local.get("recovery_replaced")
        self.assertTrue(notice, "the person is told to write down the new recovery key")
        words = self.b.account.recovery_words()
        self.assertEqual(len(words), 24)
        self.assertNotEqual(words, self.pending.words)


class S2NoOldWordsIntoTheNextEpoch(Members):
    """S2: a device that joined with the old words while the removal was pending doesn't get the new key."""

    def test_late_recovery_join_is_dropped(self) -> None:
        self.b.account.members()                          # b last counted the members before the removal
        self.b.hub.revoke(self.a.hub.device)              # the device that kept the words is removed
        self.fake.verify = False                          # a Hub that lets it in meanwhile (the real one refuses: see below)
        m = self.device("Attacker")
        m.account.join_with_recovery(self.pending.words)  # someone with the old words gets in before b rekeys
        self.b.account.refresh()
        new = self.b.account.bundle()
        self.assertEqual(new.epoch, 2)
        self.assertNotIn(m.hub.device, new.members)
        self.assertNotIn(m.hub.device, self.hub["envelopes"])


class S3FloorCoversEveryZone(Members):
    """S3: after a removal, a device refuses old zone keys even for zones it never wrote."""

    def test_floor_for_a_zone_this_device_only_read(self) -> None:
        c = self.device("Phone")
        self.joined(self.a, c)
        self.a.engine.save("apps", "flatpak:org.example.One", {"id": "org.example.One"})
        self.b.engine.fetch("apps")                       # b reads the zone; it never writes it
        self.a.account.revoke(c.hub.device)
        self.assertEqual(self.b.account.refresh(), "member")
        zone = self.a.engine.zone_id("apps")
        z = self.hub["zones"][zone]
        z["generation"] = 1                               # the Hub (with the removed device) hides the new key
        z["keys"] = {1: z["keys"][1]}
        self.b.engine._zones.clear()
        with self.assertRaises(HubError) as stale:
            self.b.engine.save("apps", "flatpak:org.example.Two", {"id": "org.example.Two"})
        self.assertEqual(stale.exception.code, "stale_generation")


class S5TokenAloneDeletesNothing(Members):
    """S5: the bearer token without the device's key (e.g. read from device.json by an app) can't delete a zone."""

    def test_unsigned_delete_is_refused(self) -> None:
        self.a.engine.save("apps", "x", {"v": 1})
        zone = self.a.engine.zone_id("apps")
        thief = Hub(Registration(self.a.hub.device, self.a.hub.registration.token, self.url, "x"))   # no signing key
        with self.assertRaises(HubError) as refused:
            thief.delete(f"zones/{zone}")
        self.assertEqual(refused.exception.status, 401)
        self.assertIn(zone, self.hub["zones"])
        self.a.engine.delete_zone("apps")                 # the device itself still can
        self.assertNotIn(zone, self.hub["zones"])


class S2ChainNoOldWordsIntoTheNextEpoch(Members):
    """S2 chain: a device let in with the old words during a pending removal approves another; neither gets the new key."""

    def test_recovery_joiner_and_the_device_it_approved_are_dropped(self) -> None:
        self.b.account.members()                          # b last counted the members before the removal
        self.b.hub.revoke(self.a.hub.device)              # the device that kept the words is removed; the new key waits
        self.fake.verify = False                          # a Hub that lets new members in meanwhile
        r2 = self.device("Attacker")
        r2.account.join_with_recovery(self.pending.words)
        r3 = self.device("Attacker 2")
        r3.account.request_join()
        r2.account.join_requests()
        code = r3.account.join_view().code
        r2.account.approve(next(c for c in r2.account.join_requests() if c["code"])["id"], code)
        self.assertEqual(r3.account.confirm_join(True).state, "approved")
        self.b.account.refresh()
        new = self.b.account.bundle()
        self.assertEqual(new.epoch, 2)
        self.assertNotIn(r2.hub.device, new.members)
        self.assertNotIn(r3.hub.device, new.members, "a device approved by the late recovery joiner doesn't get the new key either")
        self.assertEqual(set(self.hub["envelopes"]), {self.b.hub.device})

    def test_the_hub_refuses_new_members_while_the_key_change_waits(self) -> None:
        self.b.hub.revoke(self.a.hub.device)
        r2 = self.device("Attacker")
        with self.assertRaises(HubError) as refused:
            r2.account.join_with_recovery(self.pending.words)
        self.assertEqual(refused.exception.code, "rekey_pending")
