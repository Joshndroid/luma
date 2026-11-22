# SPDX-License-Identifier: Apache-2.0
"""The key hierarchy on a device (ADR-054 §1): register, turn on, join by
code or recovery key, approve or deny, revoke and replace the account key.

Joining by code. The new device commits to a random half (n_d) and asks to
join. The first member device online posts the approvers' half, n_a, which
every member derives from the account key, so all of them show the same
code and each can check the posted half. The new device then reveals n_d;
both sides show six digits over the account, the device, its two keys and
both halves. The person compares and approves on any member. The Hub can't
substitute keys without the codes differing: it must commit to n_d before
it learns n_a, and it cannot compute n_a. On approval the new device also
checks n_a against the account key it received, so a key that did not come
from a member is refused.

A replacement account key (after a revocation) carries an HMAC under the
previous one and is signed by the member that made it, whose signing key is
listed inside that same chained bundle.
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field

from . import crypto
from .crypto import Bundle, CryptoError, DeviceKeys, b64, unb64
from .hub import Hub, HubError
from .keystore import KeystoreError


class SyncStateError(Exception):
    """A plain-words reason Sync can't go on, with a code the UI acts on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class PendingSetup:
    entropy: bytes
    words: list[str]
    check: list[int]


@dataclass
class JoinView:
    state: str                         # waiting | code | approved | denied | none
    code: str = ""
    join: str = ""
    approvers: list[dict[str, str]] = field(default_factory=list)


class Account:
    def __init__(self, hub: Hub, keystore, local, device_class: str = "computer") -> None:
        self.hub = hub
        self.keystore = keystore
        self.local = local              # engine.LocalState: small non-secret values
        self.device = hub.device
        self.device_class = device_class
        self._keys: DeviceKeys | None = None
        self._bundle: Bundle | None = None
        self._state: dict | None = None

    # ------------------------------------------------------------ local keys
    def keys(self) -> DeviceKeys:
        if self._keys is None:
            raw = self.keystore.get(self.device, "device-keys")
            if raw is None:
                keys = DeviceKeys.generate()
                self.keystore.put(self.device, "device-keys", keys.to_bytes())
                raw = self.keystore.get(self.device, "device-keys")
                if raw is None:
                    raise KeystoreError("The keyring did not keep this device's keys.")
            self._keys = DeviceKeys.from_bytes(raw)
        return self._keys

    def bundle(self) -> Bundle | None:
        if self._bundle is None:
            raw = self.keystore.get(self.device, "bundle")
            self._bundle = Bundle.from_bytes(raw) if raw else None
        return self._bundle

    def _keep(self, bundle: Bundle) -> None:
        self.keystore.put(self.device, "bundle", bundle.to_bytes())
        self._bundle = bundle

    def forget(self) -> None:
        """This device was removed: its copy of the account key goes; local data stays."""
        self.keystore.delete(self.device, "bundle")
        self._bundle = None

    # ------------------------------------------------------------ the Hub's view
    def state(self, fresh: bool = True) -> dict:
        if fresh or self._state is None:
            self._state = self.hub.get("account")
        return self._state

    @property
    def account(self) -> str:
        return str(self.state(fresh=False)["account"])

    def me(self, state: dict | None = None) -> dict | None:
        state = state or self.state(fresh=False)
        return next((d for d in state["devices"] if d["device"] == self.device), None)

    def register(self) -> dict:
        keys = self.keys()
        state = self.state()
        mine = self.me(state)
        if mine and mine["state"] != "revoked" and unb64(mine["sign_pub"]) == keys.sign_pub and mine.get("class") == self.device_class:
            return mine
        try:
            self.hub.put("devices/self", {"sign_pub": b64(keys.sign_pub), "box_pub": b64(keys.box_pub),
                                          "proof": b64(keys.proof(state["account"], self.device)), "class": self.device_class})
        except HubError as error:
            if error.code != "old_keys":
                raise
            # This device was removed; its old keys are never used again. New keys, and a join that needs approval.
            self.forget()
            keys = DeviceKeys.generate()
            self.keystore.put(self.device, "device-keys", keys.to_bytes())
            self._keys = keys
            self.hub.put("devices/self", {"sign_pub": b64(keys.sign_pub), "box_pub": b64(keys.box_pub),
                                          "proof": b64(keys.proof(state["account"], self.device)), "class": self.device_class})
        return self.me(self.state())  # type: ignore[return-value]

    # ------------------------------------------------------------ turning Sync on
    def begin_setup(self) -> PendingSetup:
        entropy = crypto.new_recovery_entropy()
        words = crypto.recovery_words(entropy)
        check = sorted(secrets.SystemRandom().sample(range(1, 25), 3))
        return PendingSetup(entropy, words, check)

    def finish_setup(self, pending: PendingSetup, answers: dict[int, str]) -> None:
        if sorted(answers) != pending.check or any(pending.words[i - 1] != answers[i].strip().lower() for i in pending.check):
            raise SyncStateError("check_failed", "Those words don't match your recovery key.")
        self.setup(pending.entropy)

    def setup(self, entropy: bytes) -> None:
        state = self.state()
        if state["epoch"]:
            raise SyncStateError("already_set_up", "Sync is already on for this account. Join it from another device instead.")
        self.register()
        keys = self.keys()
        recovery = crypto.recovery_keypair(entropy, state["account"])
        bundle = Bundle.first(state["account"], recovery.public_key().public_bytes(**crypto.RAW), self.device, keys.sign_pub)
        envelope = crypto.wrap(bundle, self.device, keys.box_pub)
        self.hub.post("keys/epoch", {
            "epoch": 1,
            "envelopes": [{"device": self.device, "ciphertext": b64(envelope),
                           "signature": b64(keys.sign.sign(crypto.envelope_message(bundle.account, self.device, 1, envelope)))}],
            "recovery": b64(crypto.wrap_recovery(bundle)),
            "certs": {self.device: b64(bundle.cert(self.device, keys.sign_pub, keys.box_pub))},
        })
        self._keep(bundle)
        self.keystore.put(self.device, "recovery", " ".join(crypto.recovery_words(entropy)).encode())

    def recovery_words(self) -> list[str]:
        raw = self.keystore.get(self.device, "recovery")
        if not raw:
            raise SyncStateError("no_recovery", "This device doesn't hold the recovery key. Show it on the device that turned Sync on, or join with it here.")
        return raw.decode().split()

    # ------------------------------------------------------------ keeping up with the account key
    def refresh(self) -> str:
        """Bring the local account key up to date. Returns member | pending | new | revoked."""
        state = self.state()
        mine = self.me(state)
        if not state["epoch"]:
            return "new"
        if mine is None or mine["state"] == "pending":
            return "pending"
        if mine["state"] == "revoked":
            return "revoked"
        bundle = self.bundle()
        if bundle is None or bundle.epoch < state["epoch"]:
            self._take_envelope(bundle)
        if state.get("rekey"):
            self.rekey()
        return "member"

    def _take_envelope(self, previous: Bundle | None) -> None:
        envelope = self.hub.get("keys/envelope")
        ciphertext = unb64(envelope["ciphertext"])
        bundle = crypto.unwrap(ciphertext, self.keys().box, self.account, self.device, int(envelope["epoch"]))
        if previous is None:
            raise SyncStateError("join_again", "This device's copy of the account key is missing. Join again with a code or the recovery key.")
        if bundle.epoch == previous.epoch:
            return
        if not bundle.follows(previous):
            raise SyncStateError("join_again", "Your devices replaced the account key while this one was away. Join again with a code or the recovery key.")
        signer = bundle.members.get(envelope["wrapper"])
        if not signer or not crypto.verify(unb64(signer), unb64(envelope["signature"]),
                                           crypto.envelope_message(bundle.account, self.device, bundle.epoch, ciphertext)):
            raise SyncStateError("join_again", "A new account key arrived that no device of yours signed. It was refused.")
        self._keep(bundle)

    def trusted_members(self, state: dict | None = None) -> list[dict]:
        """Member devices whose membership the current account key vouches for."""
        bundle = self.bundle()
        state = state or self.state()
        out = []
        for device in state["devices"]:
            if device["state"] != "member" or not device.get("cert") or device.get("cert_epoch") != (bundle.epoch if bundle else -1):
                continue
            if bundle and bundle.cert_ok(device["device"], unb64(device["sign_pub"]), unb64(device["box_pub"]), unb64(device["cert"])):
                out.append(device)
        return out

    def rekey(self, new_recovery: bytes | None = None) -> int:
        """A device was removed: a new account key for the devices that remain, and new zone keys for new writes.

        With new_recovery (32 bytes of fresh entropy) the recovery key is replaced in the same step, so a removed
        device that kept the old words can't use them: the recovery envelope is for the new key only.
        """
        bundle = self.bundle()
        if bundle is None:
            raise SyncStateError("join_again", "This device has no account key.")
        state = self.state()
        if state["epoch"] != bundle.epoch:
            self._take_envelope(bundle)
            bundle = self.bundle()
            state = self.state()
            if not state.get("rekey") and new_recovery is None:
                return bundle.epoch  # type: ignore[union-attr]
        keys = self.keys()
        members = [d for d in self.trusted_members(state)]
        if not any(d["device"] == self.device for d in members):
            raise SyncStateError("join_again", "This device isn't a trusted member any more.")
        new = bundle.successor({d["device"]: d["sign_pub"] for d in members})  # type: ignore[union-attr]
        if new_recovery is not None:
            new.recovery_pub = crypto.recovery_keypair(new_recovery, new.account).public_key().public_bytes(**crypto.RAW)
            new.prev = crypto.mac(crypto.hkdf(bundle.ask, "luma-sync-epoch-chain-v1"), crypto.canonical(new.body()))  # type: ignore[union-attr]
        envelopes, certs = [], {}
        for device in members:
            envelope = crypto.wrap(new, device["device"], unb64(device["box_pub"]))
            envelopes.append({"device": device["device"], "ciphertext": b64(envelope),
                              "signature": b64(keys.sign.sign(crypto.envelope_message(new.account, device["device"], new.epoch, envelope)))})
            certs[device["device"]] = b64(new.cert(device["device"], unb64(device["sign_pub"]), unb64(device["box_pub"])))
        zone_keys = []
        for zone in self.hub.get("zones")["zones"]:
            for key in zone["keys"]:
                plain = bundle.unwrap_zone_key(zone["zone"], key["generation"], unb64(key["envelope"]))  # type: ignore[union-attr]
                zone_keys.append({"zone": zone["zone"], "generation": key["generation"],
                                  "envelope": b64(new.wrap_zone_key(zone["zone"], key["generation"], plain))})
            fresh = zone["generation"] + 1
            zone_keys.append({"zone": zone["zone"], "generation": fresh,
                              "envelope": b64(new.wrap_zone_key(zone["zone"], fresh, secrets.token_bytes(32)))})
        try:
            self.hub.post("keys/epoch", {"epoch": new.epoch, "envelopes": envelopes, "recovery": b64(crypto.wrap_recovery(new)),
                                         "certs": certs, "zone_keys": zone_keys})
        except HubError as error:
            if error.code == "epoch_conflict":   # another device replaced it first
                self._take_envelope(bundle)
                return self.bundle().epoch  # type: ignore[union-attr]
            raise
        self._keep(new)
        if new_recovery is not None:
            self.keystore.put(self.device, "recovery", " ".join(crypto.recovery_words(new_recovery)).encode())
        return new.epoch

    def revoke(self, device: str, new_recovery: bool = False) -> tuple[int, list[str] | None]:
        """Remove a device; the remaining ones get a new account key and new zone keys. Returns (epoch, new words or None)."""
        if device == self.device:
            raise SyncStateError("self", "Remove this device from another one, or turn Sync off here.")
        entropy = crypto.new_recovery_entropy() if new_recovery else None
        self.hub.revoke(device)
        epoch = self.rekey(entropy)
        return epoch, crypto.recovery_words(entropy) if entropy else None

    # ------------------------------------------------------------ joining (the new device)
    def request_join(self) -> JoinView:
        self.register()
        nonce = secrets.token_bytes(32)
        answer = self.hub.post("joins", {"commitment": b64(crypto.join_commitment(nonce))})
        self.local.put("join", {"id": answer["id"], "nonce": b64(nonce), "approver_nonce": None})
        return self.join_view()

    def join_view(self) -> JoinView:
        pending = self.local.get("join")
        if not pending:
            return JoinView("none")
        state = self.state()
        approvers = [{"device": d["device"], "name": d["name"], "class": d.get("class", "computer")}
                     for d in state["devices"] if d["state"] == "member"]
        mine = self.me(state)
        if mine and mine["state"] == "member":
            self._accept_approval(pending)
            return JoinView("approved", join=pending["id"], approvers=approvers)
        row = next((j for j in self.hub.get("joins")["joins"] if j["id"] == pending["id"]), None)
        if row is None:
            return JoinView("expired", join=pending["id"], approvers=approvers)
        if row.get("answer") == "denied":
            return JoinView("denied", join=row["id"], approvers=approvers)
        if not row.get("approver_nonce"):
            return JoinView("waiting", join=row["id"], approvers=approvers)
        if not row.get("device_nonce"):
            self.hub.post(f"joins/{row['id']}/reveal", {"nonce": pending["nonce"]})
        pending["approver_nonce"] = row["approver_nonce"]
        pending["commitment"] = row["commitment"]
        self.local.put("join", pending)
        keys = self.keys()
        code = crypto.join_code(self.account, self.device, keys.sign_pub, keys.box_pub, unb64(pending["nonce"]), unb64(row["approver_nonce"]))
        return JoinView("code", code=code, join=row["id"], approvers=approvers)

    def _accept_approval(self, pending: dict) -> None:
        if self.bundle() is not None and self.bundle().epoch == self.state(fresh=False)["epoch"]:  # type: ignore[union-attr]
            self.local.delete("join")
            return
        envelope = self.hub.get("keys/envelope")
        bundle = crypto.unwrap(unb64(envelope["ciphertext"]), self.keys().box, self.account, self.device, int(envelope["epoch"]))
        if envelope["wrapper"] != self.device:
            expected = bundle.join_nonce(pending["id"], self.device, unb64(pending.get("commitment") or ""))
            if not pending.get("approver_nonce") or expected != unb64(pending["approver_nonce"]):
                raise SyncStateError("not_approved", "The approval didn't come from one of your devices. It was refused.")
        self._keep(bundle)
        self.local.delete("join")

    def cancel_join(self) -> None:
        pending = self.local.get("join")
        if pending:
            try:
                self.hub.delete(f"joins/{pending['id']}")
            except HubError:
                pass
            self.local.delete("join")

    def join_with_recovery(self, words: list[str]) -> None:
        try:
            entropy = crypto.recovery_entropy(words)
        except CryptoError as error:
            raise SyncStateError("bad_key", str(error)) from None
        self.register()
        envelope = self.hub.get("keys/recovery")
        private = crypto.recovery_keypair(entropy, self.account)
        try:
            bundle = crypto.unwrap(unb64(envelope["ciphertext"]), private, self.account, "recovery", int(envelope["epoch"]))
        except CryptoError:
            raise SyncStateError("wrong_key", "That key doesn't match this account") from None
        if bundle.recovery_pub != private.public_key().public_bytes(**crypto.RAW):
            raise SyncStateError("wrong_key", "That key doesn't match this account")
        keys = self.keys()
        mine = crypto.wrap(bundle, self.device, keys.box_pub)
        try:
            self.hub.post("keys/envelope", {"device": self.device, "epoch": bundle.epoch, "ciphertext": b64(mine),
                                            "signature": b64(keys.sign.sign(crypto.envelope_message(bundle.account, self.device, bundle.epoch, mine))),
                                            "cert": b64(bundle.cert(self.device, keys.sign_pub, keys.box_pub))})
        except HubError as error:
            if error.code == "approval_required":
                raise SyncStateError("approval_required", "This device was removed from Sync before. Approve it on one of your devices instead.") from None
            raise
        self._keep(bundle)
        self.keystore.put(self.device, "recovery", " ".join(words).encode())
        self.local.delete("join")

    # ------------------------------------------------------------ approving (an existing device)
    def join_requests(self) -> list[dict]:
        """Open requests, each with its code once both halves are in. Posts the approvers' half when it's missing."""
        bundle = self.bundle()
        if bundle is None:
            return []
        out = []
        for row in self.hub.get("joins")["joins"]:
            if row.get("answer") or row["device"] == self.device:
                continue
            expected = bundle.join_nonce(row["id"], row["device"], unb64(row["commitment"]))
            if not row.get("approver_nonce"):
                try:
                    self.hub.post(f"joins/{row['id']}/nonce", {"nonce": b64(expected)})
                except HubError as error:
                    if error.code != "join_mismatch":
                        raise
                row = next((j for j in self.hub.get("joins")["joins"] if j["id"] == row["id"]), row)
            view = {"id": row["id"], "device": row["device"], "name": row["name"], "class": row.get("class", "computer"),
                    "created_at": row["created_at"], "code": "", "tampered": False}
            if row.get("approver_nonce") and unb64(row["approver_nonce"]) != expected:
                view["tampered"] = True
            elif row.get("device_nonce"):
                view["code"] = crypto.join_code(bundle.account, row["device"], unb64(row["sign_pub"]), unb64(row["box_pub"]),
                                                unb64(row["device_nonce"]), expected)
            out.append(view)
        return out

    def approve(self, join_id: str, code: str) -> None:
        bundle = self.bundle()
        if bundle is None:
            raise SyncStateError("not_member", "This device hasn't joined Sync.")
        row = next((j for j in self.hub.get("joins")["joins"] if j["id"] == join_id and not j.get("answer")), None)
        if row is None:
            raise SyncStateError("join_unknown", "That request was answered on another device or withdrawn.")
        view = next((v for v in self.join_requests() if v["id"] == join_id), None)
        if view is None or view["tampered"] or not view["code"]:
            raise SyncStateError("join_incomplete", "The code isn't ready yet.")
        if not secrets.compare_digest(view["code"], "".join(ch for ch in code if ch.isdigit())):
            raise SyncStateError("code_mismatch", "The codes don't match. Don't approve this device.")
        keys = self.keys()
        box_pub, sign_pub = unb64(row["box_pub"]), unb64(row["sign_pub"])
        envelope = crypto.wrap(bundle, row["device"], box_pub)
        self.hub.post("keys/envelope", {"device": row["device"], "epoch": bundle.epoch, "ciphertext": b64(envelope), "join": join_id,
                                        "signature": b64(keys.sign.sign(crypto.envelope_message(bundle.account, row["device"], bundle.epoch, envelope))),
                                        "cert": b64(bundle.cert(row["device"], sign_pub, box_pub))})

    def deny(self, join_id: str) -> None:
        self.hub.post(f"joins/{join_id}/deny", {})


def dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True)
