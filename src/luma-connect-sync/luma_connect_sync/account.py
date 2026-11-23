# SPDX-License-Identifier: Apache-2.0
"""The key hierarchy on a device (ADR-054 §1): register, turn on, join by
code or recovery key, approve or deny, revoke and replace the account key.

Nothing the Hub says about membership is trusted on its own (security review
C1, C2, H1, H2):

* **Who is a member** comes from the signed account key bundle of the epoch
  and from signed membership statements: an approval is signed by a member
  of the epoch, a recovery join by the recovery key. A device the Hub lists
  as a member without a valid statement gets no key.
* **Joining by code.** The new device commits to its half (n_d) and asks to
  join. The first member online posts the approvers' half n_a, derived from
  the account key, so every member shows the same code and can check the
  posted value. The new device fixes the first n_a it sees, never accepts
  another, and reveals n_d once. The approver checks that n_d opens the
  commitment. Both show six digits over the whole transcript (account, join
  id, device, its keys, commitment, n_a, n_d). A Hub that substitutes keys
  must commit before it learns n_a and can't grind afterwards.
* **Accepting an approval.** The new device opens the envelope and checks
  that the bundle derives the n_a it fixed, that the wrapper is a member of
  that bundle other than itself, and that the wrapper's signature verifies.
* **Replacing the account key.** A new bundle is chained to the previous ASK,
  signed by a member of the previous epoch, never lists a removed device and
  only lists devices that were members or were approved during that epoch.
  New zone key generations are chosen by the device that makes the key, and
  every device refuses to write with a generation older than the floor it
  recorded when the epoch changed.
"""
from __future__ import annotations

import json
import secrets
import time
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
    state: str                         # none | waiting | code | confirmed | approved | denied | expired | tampered
    # "code": shown, waiting for the person to say on this device that both codes matched (review B3);
    # "confirmed": they did; the approval is taken when it arrives.
    code: str = ""
    join: str = ""
    approvers: list[dict[str, str]] = field(default_factory=list)
    fingerprint: str = ""


class Account:
    def __init__(self, hub: Hub, keystore, local, device_class: str = "computer") -> None:
        self.hub = hub
        hub.signer = lambda message: self.keys().sign.sign(message)
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
                    raise KeystoreError("The keystore did not keep this device's keys.")
            self._keys = DeviceKeys.from_bytes(raw)
        return self._keys

    def bundle(self) -> Bundle | None:
        if self._bundle is None:
            raw = self.keystore.get(self.device, "bundle")
            self._bundle = Bundle.from_bytes(raw) if raw else None
        return self._bundle

    def _keep(self, bundle: Bundle) -> None:
        previous = self.bundle()
        self.keystore.put(self.device, "bundle", bundle.to_bytes())
        self._bundle = bundle
        removed = set(self.local.get("removed", [])) | set(bundle.removed)
        self.local.put("removed", sorted(removed))
        if previous is not None and bundle.epoch > previous.epoch:
            # New writes only with zone keys made for this epoch (review H2.4).
            seen = self.local.get("zone_generations", {})
            self.local.put("zone_floor", {zone: generation + 1 for zone, generation in seen.items()})

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

    # ------------------------------------------------------------ who is a member (signed, never the Hub's list)
    def membership_log(self) -> list[dict]:
        try:
            return self.hub.get("membership")["statements"]
        except HubError as error:
            if error.status == 404:
                return []
            raise

    def members(self, bundle: Bundle | None = None, log: list[dict] | None = None, keep_removed: bool = False) -> dict[str, dict[str, str]]:
        """The members of the bundle's epoch: the bundle's own list, plus devices added during it by a signed statement."""
        bundle = bundle or self.bundle()
        if bundle is None:
            return {}
        members = {device: dict(keys) for device, keys in bundle.members.items()}
        removed = set(bundle.removed) | set(self.local.get("removed", []))
        current = self._bundle is not None and self._bundle.epoch == bundle.epoch and self._bundle.ask == bundle.ask
        seen = self.local.get("known_members", {})
        # Devices this device already counted as members of the epoch stay members even when their approver is
        # removed later; a statement a removed device signs after that is ignored.
        known = set(seen.get("members", [])) if current and seen.get("epoch") == bundle.epoch else set()
        for row in log if log is not None else self.membership_log():
            try:
                statement = json.loads(unb64(row["statement"]))
            except (ValueError, KeyError):
                continue
            if (statement.get("kind") != "add" or statement.get("account") != bundle.account or statement.get("epoch") != bundle.epoch
                    or (not keep_removed and crypto.is_removed(removed, str(statement.get("device")), str(statement.get("sign_pub"))))
                    or statement.get("device") in members):
                continue
            signer = statement.get("signer")
            if signer == "recovery":
                key = bundle.recovery_auth_pub
            elif signer in members and (not crypto.is_removed(removed, signer, members[signer]["sign"]) or statement.get("device") in known):
                key = unb64(members[signer]["sign"])
            else:
                continue
            if crypto.verify(key, unb64(row["signature"]), crypto.statement_bytes(statement)):
                members[str(statement["device"])] = {"sign": str(statement["sign_pub"]), "box": str(statement["box_pub"]),
                                                     **({"via": "recovery"} if signer == "recovery" else {})}
        if keep_removed:
            return members
        members = {d: k for d, k in members.items() if not crypto.is_removed(removed, d, k["sign"])}
        if current:
            self.local.put("known_members", {"epoch": bundle.epoch, "members": sorted(set(members) | known)})
        return members

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
        recovery = crypto.recovery_keys(entropy, state["account"])
        bundle = Bundle.first(state["account"], recovery, self.device, keys)
        envelope = crypto.wrap(bundle, self.device, keys.box_pub)
        self.hub.post("keys/epoch", {
            "epoch": 1,
            "envelopes": [{"device": self.device, "ciphertext": b64(envelope),
                           "signature": b64(keys.sign.sign(crypto.envelope_message(bundle.account, self.device, 1, envelope)))}],
            "recovery": b64(crypto.wrap_recovery(bundle)), "recovery_auth_pub": b64(recovery.auth_pub),
            "certs": {self.device: b64(bundle.cert(self.device, keys.sign_pub, keys.box_pub))},
        })
        self._keep(bundle)
        # Only the device that made the key keeps the words (review H3, M6), sealed to this user.
        self.keystore.put(self.device, "recovery", " ".join(crypto.recovery_words(entropy)).encode())

    def recovery_words(self) -> list[str]:
        raw = self.keystore.get(self.device, "recovery")
        if not raw:
            raise SyncStateError("no_recovery", "This device doesn't hold the recovery key. Show it on the device that turned Sync on.")
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
        if crypto.is_removed(self.local.get("removed", []), self.device, b64(self.keys().sign_pub)):
            return "revoked"
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
        approved = self.members(previous)
        gone = set(self.local.get("removed", []))
        # A key made by, or for, a device this one removed is never taken, whatever the Hub serves (review H2).
        signer_keys = previous.members.get(bundle.signer) or approved.get(bundle.signer) or {}
        if not bundle.follows(previous, approved) or crypto.is_removed(gone, bundle.signer, str(signer_keys.get("sign"))) \
                or any(crypto.is_removed(gone, d, k["sign"]) for d, k in bundle.members.items()):
            raise SyncStateError("join_again", "A new account key arrived that your devices didn't make. It was refused.")
        signer = (previous.members.get(bundle.signer) or approved.get(bundle.signer) or {}).get("sign")
        if envelope.get("wrapper") != bundle.signer or not signer or not crypto.verify(
                unb64(signer), unb64(envelope["signature"]), crypto.envelope_message(bundle.account, self.device, bundle.epoch, ciphertext)):
            raise SyncStateError("join_again", "A new account key arrived that no device of yours signed. It was refused.")
        if crypto.is_removed(bundle.removed, self.device, b64(self.keys().sign_pub)):
            self.local.put("removed", sorted(set(self.local.get("removed", [])) | set(bundle.removed)))
            return
        self._keep(bundle)

    def rekey(self, exclude: set[str] | None = None, new_recovery: bytes | None = None) -> tuple[int, list[str] | None]:
        """Remove devices: a new account key for the members that remain, new zone key generations for new writes.

        Members come from the signed bundle and statements, never from the Hub's list (review H2). If a removed
        device held the recovery words, the recovery key is replaced too (review M6); the new words are returned.
        """
        bundle = self.bundle()
        if bundle is None:
            raise SyncStateError("join_again", "This device has no account key.")
        state = self.state()
        if state["epoch"] != bundle.epoch:
            self._take_envelope(bundle)
            bundle = self.bundle()  # type: ignore[assignment]
            state = self.state()
            if not state.get("rekey") and not exclude:
                return bundle.epoch, None  # type: ignore[union-attr]
        keys = self.keys()
        hub_revoked = {d["device"] for d in state["devices"] if d["state"] == "revoked"}
        # The devices being removed first, so nothing they signed for a device this one never counted gets in (S4).
        first = self.members(bundle, keep_removed=True)
        leaving = {crypto.removal(d, k["sign"]) for d, k in first.items() if d in (exclude or set()) or d in hub_revoked}
        if leaving:
            self.local.put("removed", sorted(set(self.local.get("removed", [])) | leaving))
        members = self.members(bundle, keep_removed=True)
        gone = set(self.local.get("removed", []))
        removed = {d for d, k in members.items()
                   if d in (exclude or set()) or d in hub_revoked or crypto.is_removed(gone, d, k["sign"])}
        if self.device in removed:
            raise SyncStateError("self", "This device can't remove itself.")
        if self.device not in members:
            raise SyncStateError("join_again", "This device isn't a member any more.")
        entries = {crypto.removal(d, members[d]["sign"]) for d in removed}
        holders = [d for d in bundle.holds_recovery if d not in removed]  # type: ignore[union-attr]
        words = None
        if new_recovery is None and set(bundle.holds_recovery) & removed:  # type: ignore[union-attr]
            new_recovery = crypto.new_recovery_entropy()
        recovery = crypto.recovery_keys(new_recovery, bundle.account) if new_recovery is not None else None  # type: ignore[union-attr]
        if recovery is not None:
            holders = [self.device]
            words = crypto.recovery_words(new_recovery)  # type: ignore[arg-type]
            # The old words are being retired: no device added during this epoch that this device hadn't counted
            # before the removal goes into the new one, whoever let it in: one that came in with the old words, and
            # any it then approved (review S2 and its chain).
            seen = self.local.get("known_members", {})
            known = set(seen.get("members", [])) if seen.get("epoch") == bundle.epoch else set()  # type: ignore[union-attr]
            late = {d for d in members if d not in bundle.members and d not in known and d != self.device}  # type: ignore[union-attr]
            removed |= late
            entries |= {crypto.removal(d, members[d]["sign"]) for d in late}
        remaining = {d: {"sign": k["sign"], "box": k["box"]} for d, k in members.items() if d not in removed}
        # Remembered before anything is sent, so a key the removed device makes in the meantime is refused below.
        self.local.put("removed", sorted(set(self.local.get("removed", [])) | entries))
        new = bundle.successor(remaining, sorted(entries), self.device, keys, holders, recovery)  # type: ignore[union-attr]
        envelopes, certs = [], {}
        for device, entry in remaining.items():
            envelope = crypto.wrap(new, device, unb64(entry["box"]))
            envelopes.append({"device": device, "ciphertext": b64(envelope),
                              "signature": b64(keys.sign.sign(crypto.envelope_message(new.account, device, new.epoch, envelope)))})
            certs[device] = b64(new.cert(device, unb64(entry["sign"]), unb64(entry["box"])))
        zone_keys, fresh_generations = [], {}
        for zone in self.hub.get("zones")["zones"]:
            for key in zone["keys"]:
                plain = bundle.unwrap_zone_key(zone["zone"], key["generation"], unb64(key["envelope"]))  # type: ignore[union-attr]
                zone_keys.append({"zone": zone["zone"], "generation": key["generation"],
                                  "envelope": b64(new.wrap_zone_key(zone["zone"], key["generation"], plain))})
            fresh = max(zone["generation"], self.local.get("zone_generations", {}).get(zone["zone"], 0)) + 1
            fresh_generations[zone["zone"]] = fresh
            zone_keys.append({"zone": zone["zone"], "generation": fresh,
                              "envelope": b64(new.wrap_zone_key(zone["zone"], fresh, secrets.token_bytes(32)))})
        body = {"epoch": new.epoch, "envelopes": envelopes, "recovery": b64(crypto.wrap_recovery(new)), "certs": certs, "zone_keys": zone_keys}
        if recovery is not None:
            body["recovery_auth_pub"] = b64(recovery.auth_pub)
        try:
            self.hub.post("keys/epoch", body)
        except HubError as error:
            if error.code == "epoch_conflict":   # another device replaced it first; adopt it only if it passes every check
                self._take_envelope(bundle)
                return self.bundle().epoch, None  # type: ignore[union-attr]
            raise
        self._keep(new)
        self.local.put("zone_floor", {**self.local.get("zone_floor", {}), **fresh_generations})
        if words:
            self.keystore.put(self.device, "recovery", " ".join(words).encode())
            # Never silent (review S1): the person is asked to write the new words down until they have seen them.
            self.local.put("recovery_replaced", {"epoch": new.epoch, "at": int(time.time())})
        return new.epoch, words

    def revoke(self, device: str, new_recovery: bool = False) -> tuple[int, list[str] | None]:
        """Remove a device. Returns (epoch, new recovery words or None); new words come whenever the device held them."""
        if device == self.device:
            raise SyncStateError("self", "Remove this device from another one, or turn Sync off here.")
        # Kept here first: from now on no key made by or for it is taken, whatever the Hub serves (review H2).
        bundle = self.bundle()
        keys = self.members(bundle).get(device) if bundle else None
        if keys:
            self.local.put("removed", sorted(set(self.local.get("removed", [])) | {crypto.removal(device, keys["sign"])}))
        self.hub.revoke(device)
        return self.rekey({device}, crypto.new_recovery_entropy() if new_recovery else None)

    # ------------------------------------------------------------ joining (the new device)
    def request_join(self) -> JoinView:
        self.register()
        nonce = secrets.token_bytes(32)
        commitment = crypto.join_commitment(nonce)
        answer = self.hub.post("joins", {"commitment": b64(commitment)})
        self.local.put("join", {"id": answer["id"], "nonce": b64(nonce), "commitment": b64(commitment),
                                "approver_nonce": None, "revealed": False, "code": ""})
        return self.join_view()

    def join_view(self) -> JoinView:
        pending = self.local.get("join")
        if not pending:
            return JoinView("none")
        if pending.get("tampered"):
            return JoinView("tampered", join=pending["id"])
        state = self.state()
        keys = self.keys()
        approvers = [{"device": d["device"], "name": d["name"], "class": d.get("class", "computer")}
                     for d in state["devices"] if d["state"] == "member"]
        mine = self.me(state)
        if mine and mine["state"] == "member" and pending.get("revealed"):
            if not pending.get("confirmed"):
                # Only after the person said on this device that the codes matched (review B3): the Hub chose the
                # approvers' half this device fixed, so nothing else here can tell a real approval from a Hub-made account.
                return JoinView("code", code=pending["code"], join=pending["id"], approvers=approvers,
                                fingerprint=crypto.fingerprint(keys.sign_pub, keys.box_pub))
            self._accept_approval(pending)
            return JoinView("approved", join=pending["id"], approvers=approvers)
        row = next((j for j in self.hub.get("joins")["joins"] if j["id"] == pending["id"]), None)
        if row is None:
            return JoinView("expired", join=pending["id"], approvers=approvers)
        if row.get("answer") == "denied":
            return JoinView("denied", join=row["id"], approvers=approvers)
        if row.get("commitment") != pending["commitment"]:
            return self._tampered(pending)
        if not row.get("approver_nonce"):
            return JoinView("waiting", join=row["id"], approvers=approvers)
        # The first approvers' half this device sees is the one; another one later means someone is in the middle.
        if pending.get("approver_nonce") is None:
            pending["approver_nonce"] = row["approver_nonce"]
            self.local.put("join", pending)
        elif pending["approver_nonce"] != row["approver_nonce"]:
            return self._tampered(pending)
        if not pending.get("revealed"):
            pending["code"] = crypto.join_code(self.account, pending["id"], self.device, keys.sign_pub, keys.box_pub,
                                               unb64(pending["commitment"]), unb64(pending["approver_nonce"]), unb64(pending["nonce"]))
            pending["revealed"] = True
            self.local.put("join", pending)
            self.hub.post(f"joins/{row['id']}/reveal", {"nonce": pending["nonce"]})
        return JoinView("confirmed" if pending.get("confirmed") else "code", code=pending["code"], join=row["id"],
                        approvers=approvers, fingerprint=crypto.fingerprint(keys.sign_pub, keys.box_pub))

    def confirm_join(self, matched: bool) -> JoinView:
        """The person's answer on this device: the code on the other device was the same (and they approved it there),
        or it wasn't. A mismatch ends the join as tampered."""
        pending = self.local.get("join")
        if not pending or not pending.get("revealed"):
            raise SyncStateError("join_incomplete", "There is no code to compare yet.")
        if not matched:
            view = self._tampered(pending)
            try:
                self.hub.delete(f"joins/{pending['id']}")
            except HubError:
                pass
            return view
        pending["confirmed"] = True
        self.local.put("join", pending)
        return self.join_view()

    def _tampered(self, pending: dict) -> JoinView:
        pending["tampered"] = True
        self.local.put("join", pending)
        return JoinView("tampered", join=pending["id"])

    def _accept_approval(self, pending: dict) -> None:
        """Only an approval that comes from a member and matches the code this device showed (review H1)."""
        if self.bundle() is not None and self.bundle().epoch == self.state(fresh=False)["epoch"]:  # type: ignore[union-attr]
            self.local.delete("join")
            return
        envelope = self.hub.get("keys/envelope")
        ciphertext = unb64(envelope["ciphertext"])
        bundle = crypto.unwrap(ciphertext, self.keys().box, self.account, self.device, int(envelope["epoch"]))
        wrapper = envelope.get("wrapper")
        members = self.members(bundle)
        refused = SyncStateError("not_approved", "The approval didn't come from one of your devices. It was refused.")
        if wrapper == self.device or wrapper not in members or not pending.get("approver_nonce"):
            raise refused
        if bundle.join_nonce(pending["id"], self.device, unb64(pending["commitment"])) != unb64(pending["approver_nonce"]):
            raise refused
        if not crypto.verify(unb64(members[wrapper]["sign"]), unb64(envelope["signature"]),
                             crypto.envelope_message(bundle.account, self.device, bundle.epoch, ciphertext)):
            raise refused
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
        recovery = crypto.recovery_keys(entropy, self.account)
        try:
            bundle = crypto.unwrap(unb64(envelope["ciphertext"]), recovery.box, self.account, "recovery", int(envelope["epoch"]))
        except CryptoError:
            raise SyncStateError("wrong_key", "That key doesn't match this account") from None
        if bundle.recovery_pub != recovery.box_pub or bundle.recovery_auth_pub != recovery.auth_pub:
            raise SyncStateError("wrong_key", "That key doesn't match this account")
        keys = self.keys()
        if crypto.is_removed(bundle.removed, self.device, b64(keys.sign_pub)):
            raise SyncStateError("approval_required", "This device was removed from Sync before. Approve it on one of your devices instead.")
        mine = crypto.wrap(bundle, self.device, keys.box_pub)
        cert = bundle.cert(self.device, keys.sign_pub, keys.box_pub)
        statement = crypto.add_statement(bundle.account, bundle.epoch, self.device, keys.sign_pub, keys.box_pub, "recovery", "recovery", mine, cert)
        try:
            self.hub.post("keys/envelope", {
                "device": self.device, "epoch": bundle.epoch, "ciphertext": b64(mine), "cert": b64(cert),
                "signature": b64(keys.sign.sign(crypto.envelope_message(bundle.account, self.device, bundle.epoch, mine))),
                "statement": b64(crypto.canonical(statement)),
                "statement_signature": b64(recovery.auth.sign(crypto.statement_bytes(statement)))})
        except HubError as error:
            if error.code == "approval_required":
                raise SyncStateError("approval_required", "This device was removed from Sync before. Approve it on one of your devices instead.") from None
            raise
        self._keep(bundle)
        self.local.delete("join")     # the words are not kept here (review H3, M6)

    # ------------------------------------------------------------ approving (an existing device)
    JOIN_ANSWERS_PER_DAY = 3

    def join_requests(self) -> list[dict]:
        """Open requests, each with its code once both halves are in and check out.

        The approvers' half is posted for at most one open request per device and a few new requests a day (review B2):
        every half posted fixes one code a Hub could later match, so each answered request becomes a card the person
        sees. Requests beyond that are listed as held, without a code.
        """
        bundle = self.bundle()
        if bundle is None:
            return []
        rows = self.hub.get("joins")["joins"]
        out = []
        answered = self.local.get("join_answers", {})          # join id -> when this device posted its half
        now = time.time()
        answered = {k: v for k, v in answered.items() if now - v < 7 * 86400}
        today = sum(1 for v in answered.values() if now - v < 86400)
        newest: dict[str, dict] = {}
        for row in rows:
            if row.get("answer") or row["device"] == self.device:
                continue
            if row["device"] not in newest or row.get("created_at", 0) > newest[row["device"]].get("created_at", 0):
                newest[row["device"]] = row
        held = 0
        for row in rows:
            if row.get("answer") or row["device"] == self.device:
                continue
            if newest[row["device"]] is not row:
                held += 1
                continue
            expected = bundle.join_nonce(row["id"], row["device"], unb64(row["commitment"]))
            if not row.get("approver_nonce") and row["id"] not in answered:
                if today >= self.JOIN_ANSWERS_PER_DAY:
                    held += 1
                    out.append(self._card(bundle, row, expected, held=True))
                    continue
                try:
                    self.hub.post(f"joins/{row['id']}/nonce", {"nonce": b64(expected)})
                except HubError as error:
                    if error.code != "join_mismatch":
                        raise
                answered[row["id"]] = now
                today += 1
                row = next((j for j in self.hub.get("joins")["joins"] if j["id"] == row["id"]), row)
            out.append(self._card(bundle, row, expected))
        self.local.put("join_answers", answered)
        self.local.put("join_requests_held", held)
        return out

    @staticmethod
    def _card(bundle: Bundle, row: dict, expected: bytes, held: bool = False) -> dict:
        """What the person sees for one request, computed from that one row only: its keys, its halves, its code."""
        commitment = unb64(row["commitment"])
        sign_pub, box_pub = unb64(row["sign_pub"]), unb64(row["box_pub"])
        view = {"id": row["id"], "device": row["device"], "name": row["name"], "class": row.get("class", "computer"),
                "created_at": row["created_at"], "code": "", "tampered": False, "held": held,
                "sign_pub": row["sign_pub"], "box_pub": row["box_pub"],
                "fingerprint": crypto.fingerprint(sign_pub, box_pub)}
        if held:
            return view
        if row.get("approver_nonce") and unb64(row["approver_nonce"]) != expected:
            view["tampered"] = True
        elif row.get("device_nonce"):
            device_nonce = unb64(row["device_nonce"])
            # The revealed half must open the commitment made before n_a existed (review C1).
            if crypto.join_commitment(device_nonce) != commitment:
                view["tampered"] = True
            else:
                view["code"] = crypto.join_code(bundle.account, row["id"], row["device"], sign_pub, box_pub,
                                                commitment, expected, device_nonce)
        return view

    def approve(self, join_id: str, code: str) -> None:
        bundle = self.bundle()
        if bundle is None:
            raise SyncStateError("not_member", "This device hasn't joined Sync.")
        if self.device not in self.members(bundle):
            raise SyncStateError("not_member", "This device isn't a member any more.")
        # One fetch: the code is checked on, and the key wrapped to, the same row (review B1).
        row = next((j for j in self.hub.get("joins")["joins"] if j["id"] == join_id and not j.get("answer")), None)
        if row is None:
            raise SyncStateError("join_unknown", "That request was answered on another device or withdrawn.")
        if row["device"] == self.device:
            raise SyncStateError("join_unknown", "That request is this device's own.")
        view = self._card(bundle, row, bundle.join_nonce(row["id"], row["device"], unb64(row["commitment"])))
        if view["tampered"] or not view["code"]:
            raise SyncStateError("join_incomplete" if not view["tampered"] else "tampered",
                                 "The code isn't ready yet." if not view["tampered"] else "This request was tampered with. Don't approve it.")
        if crypto.is_removed(set(bundle.removed) | set(self.local.get("removed", [])), row["device"], row["sign_pub"]):
            raise SyncStateError("removed", "That device was removed from Sync with these keys. They can't come back.")
        if not secrets.compare_digest(view["code"], "".join(ch for ch in code if ch.isdigit())):
            raise SyncStateError("code_mismatch", "The codes don't match. Don't approve this device.")
        keys = self.keys()
        box_pub, sign_pub = unb64(view["box_pub"]), unb64(view["sign_pub"])
        envelope = crypto.wrap(bundle, row["device"], box_pub)
        cert = bundle.cert(row["device"], sign_pub, box_pub)
        statement = crypto.add_statement(bundle.account, bundle.epoch, row["device"], sign_pub, box_pub, "approval", self.device, envelope, cert)
        self.hub.post("keys/envelope", {
            "device": row["device"], "epoch": bundle.epoch, "ciphertext": b64(envelope), "join": join_id, "cert": b64(cert),
            "signature": b64(keys.sign.sign(crypto.envelope_message(bundle.account, row["device"], bundle.epoch, envelope))),
            "statement": b64(crypto.canonical(statement)),
            "statement_signature": b64(keys.sign.sign(crypto.statement_bytes(statement)))})

    def deny(self, join_id: str) -> None:
        self.hub.post(f"joins/{join_id}/deny", {})

    def delete_everything(self, words: list[str]) -> None:
        """Delete synced data for the whole account: the Hub wants the recovery key for this (review C2.4)."""
        try:
            entropy = crypto.recovery_entropy(words)
        except CryptoError as error:
            raise SyncStateError("bad_key", str(error)) from None
        recovery = crypto.recovery_keys(entropy, self.account)
        at = int(time.time())
        self.hub.call("DELETE", "account", {"at": at, "proof": b64(recovery.auth.sign(crypto.recovery_delete_message(self.account, at)))})


def dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True)
