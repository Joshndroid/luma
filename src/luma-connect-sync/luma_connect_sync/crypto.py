# SPDX-License-Identifier: Apache-2.0
"""Keys and encryption for Luma Connect Sync (ADR-054 §1), wire format v2.

Only vetted primitives, composed the way the ADR says:

* device keys: Ed25519 (identity, signatures) and X25519 (envelopes), from
  python-cryptography;
* envelopes: HPKE (RFC 9180) base mode, X25519 / HKDF-SHA256 /
  ChaCha20-Poly1305, from python-cryptography's ``hpke``; authenticated by the
  wrapping member's Ed25519 signature, never by HPKE itself;
* records, blobs and wrapped zone keys: XChaCha20-Poly1305 from libsodium,
  a fresh random 24-byte nonce each time; every sealed object starts with a
  format byte that is also bound into its associated data;
* names: HMAC-SHA256 over length-prefixed fields; derived keys: HKDF-SHA256;
* the recovery key: 24 BIP-39 words (256 bits and a checksum), stretched
  with Argon2id; from the result, an X25519 key for the recovery envelope and
  an Ed25519 key that proves the recovery key to the Hub.

The account key bundle is what an envelope carries: the account sync key
(ASK) of an epoch, the stable naming key, the members' keys, the devices
removed, which devices hold the recovery words, and the recovery public keys.
From epoch 2 on it is chained to the previous ASK and signed by a member of
the previous epoch.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import struct
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, hpke, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import sodium

FORMAT = 2                      # the first byte of every sealed record, blob and zone-key wrap
SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.CHACHA20_POLY1305)
RAW = dict(encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
PRIVATE_RAW = dict(encoding=serialization.Encoding.Raw, format=serialization.PrivateFormat.Raw,
                   encryption_algorithm=serialization.NoEncryption())
# Argon2id for the recovery key: 64 MiB, 3 passes. The 24 words already
# carry 256 bits, so this is a margin, not the protection.
ARGON = dict(length=32, iterations=3, lanes=4, memory_cost=65536)


class CryptoError(Exception):
    """Something failed to verify or decrypt. Never carries key material."""


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hkdf(key: bytes, info: str, length: int = 32) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=None, info=info.encode()).derive(key)


def lp(*parts: str | bytes | int) -> bytes:
    """Length-prefixed concatenation: unambiguous whatever the fields hold."""
    out = bytearray()
    for part in parts:
        data = str(part).encode() if isinstance(part, int) else part.encode() if isinstance(part, str) else part
        out += struct.pack(">I", len(data)) + data
    return bytes(out)


def mac(key: bytes, *parts: str | bytes | int) -> bytes:
    return hmac.new(key, lp(*parts), hashlib.sha256).digest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def fingerprint(*keys: bytes) -> str:
    """What a person can compare by eye: the first 16 bytes of SHA-256 over the keys, in groups of four."""
    digest = hashlib.sha256(b"".join(keys)).hexdigest()[:32]
    return " ".join(digest[i:i + 4] for i in range(0, 32, 4))


# ---------------------------------------------------------------- device keys

@dataclass
class DeviceKeys:
    sign: Ed25519PrivateKey
    box: X25519PrivateKey

    @classmethod
    def generate(cls) -> "DeviceKeys":
        return cls(Ed25519PrivateKey.generate(), X25519PrivateKey.generate())

    @classmethod
    def from_bytes(cls, data: bytes) -> "DeviceKeys":
        if len(data) != 64:
            raise CryptoError("stored device keys are damaged")
        return cls(Ed25519PrivateKey.from_private_bytes(data[:32]), X25519PrivateKey.from_private_bytes(data[32:]))

    def to_bytes(self) -> bytes:
        return self.sign.private_bytes(**PRIVATE_RAW) + self.box.private_bytes(**PRIVATE_RAW)

    @property
    def sign_pub(self) -> bytes:
        return self.sign.public_key().public_bytes(**RAW)

    @property
    def box_pub(self) -> bytes:
        return self.box.public_key().public_bytes(**RAW)

    def entry(self) -> dict[str, str]:
        return {"sign": b64(self.sign_pub), "box": b64(self.box_pub)}

    def proof(self, account: str, device: str) -> bytes:
        """The Hub checks this when the keys are registered (DEVICE_PROOF in the Hub's sync/store.mjs)."""
        return self.sign.sign(f"luma-sync-device-v1\0{account}\0{device}\0{b64(self.box_pub)}".encode())


def verify(sign_pub: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(sign_pub).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------- the recovery key's two keys

@dataclass
class RecoveryKeys:
    box: X25519PrivateKey          # opens the recovery envelope
    auth: Ed25519PrivateKey        # proves the recovery key to the Hub and signs a recovery join

    @property
    def box_pub(self) -> bytes:
        return self.box.public_key().public_bytes(**RAW)

    @property
    def auth_pub(self) -> bytes:
        return self.auth.public_key().public_bytes(**RAW)


# ---------------------------------------------------------------- the account key bundle

@dataclass
class Bundle:
    account: str
    epoch: int
    ask: bytes                                   # the account sync key of this epoch
    names: bytes                                 # stable: zone ids and record names stay the same across epochs
    recovery_pub: bytes
    recovery_auth_pub: bytes
    members: dict[str, dict[str, str]]           # device -> {"sign": b64, "box": b64}, as of this epoch
    removed: list[str] = field(default_factory=list)          # "<device>/<b64 sign_pub>" for every device key ever removed; never re-admitted
    holds_recovery: list[str] = field(default_factory=list)   # devices that keep the 24 words
    signer: str = ""                             # a member of the previous epoch (empty at epoch 1)
    prev: bytes = b""                            # HMAC under the previous epoch's ASK
    sig: bytes = b""                             # the signer's Ed25519 signature over everything above

    def body(self) -> dict[str, object]:
        return {"v": 2, "account": self.account, "epoch": self.epoch, "ask": b64(self.ask), "names": b64(self.names),
                "recovery_pub": b64(self.recovery_pub), "recovery_auth_pub": b64(self.recovery_auth_pub),
                "members": {k: dict(sorted(v.items())) for k, v in sorted(self.members.items())},
                "removed": sorted(set(self.removed)), "holds_recovery": sorted(set(self.holds_recovery)), "signer": self.signer}

    def signed_bytes(self) -> bytes:
        return b"luma-sync-bundle-v2\0" + canonical({**self.body(), "prev": b64(self.prev)})

    def to_bytes(self) -> bytes:
        return canonical({**self.body(), "prev": b64(self.prev), "sig": b64(self.sig)})

    @classmethod
    def from_bytes(cls, data: bytes) -> "Bundle":
        try:
            value = json.loads(data)
            if value.get("v") != 2:
                raise ValueError("version")
            members = {str(k): {"sign": str(v["sign"]), "box": str(v["box"])} for k, v in dict(value["members"]).items()}
            bundle = cls(str(value["account"]), int(value["epoch"]), unb64(value["ask"]), unb64(value["names"]),
                         unb64(value["recovery_pub"]), unb64(value["recovery_auth_pub"]), members,
                         [str(d) for d in value.get("removed") or []], [str(d) for d in value.get("holds_recovery") or []],
                         str(value.get("signer") or ""), unb64(value.get("prev") or ""), unb64(value.get("sig") or ""))
        except (ValueError, KeyError, TypeError) as error:
            raise CryptoError("the account key could not be read") from error
        if len(bundle.ask) != 32 or len(bundle.names) != 32 or len(bundle.recovery_pub) != 32 or len(bundle.recovery_auth_pub) != 32:
            raise CryptoError("the account key could not be read")
        if any(is_removed(bundle.removed, device, keys["sign"]) for device, keys in bundle.members.items()):
            raise CryptoError("the account key lists a removed device as a member")
        return bundle

    @classmethod
    def first(cls, account: str, recovery: RecoveryKeys, device: str, keys: DeviceKeys) -> "Bundle":
        return cls(account, 1, secrets.token_bytes(32), secrets.token_bytes(32), recovery.box_pub, recovery.auth_pub,
                   {device: keys.entry()}, [], [device])

    def successor(self, members: dict[str, dict[str, str]], removed: list[str], signer: str, signer_keys: DeviceKeys,
                  holds_recovery: list[str], recovery: RecoveryKeys | None = None) -> "Bundle":
        """A new ASK after a revocation: chained to this one and signed by a member of this epoch (the caller checks it is one)."""
        gone = sorted(set(self.removed) | set(removed))
        kept = {d: k for d, k in members.items() if not is_removed(gone, d, k["sign"])}
        nxt = Bundle(self.account, self.epoch + 1, secrets.token_bytes(32), self.names,
                     recovery.box_pub if recovery else self.recovery_pub, recovery.auth_pub if recovery else self.recovery_auth_pub,
                     kept, gone, [d for d in holds_recovery if d in kept], signer)
        nxt.prev = hmac.new(hkdf(self.ask, "luma-sync-epoch-chain-v2"), canonical(nxt.body()), hashlib.sha256).digest()
        nxt.sig = signer_keys.sign.sign(nxt.signed_bytes())
        return nxt

    def follows(self, previous: "Bundle", approved: dict[str, dict[str, str]] | None = None) -> bool:
        """The checks every device makes before adopting a new account key (review H2)."""
        if not (self.epoch == previous.epoch + 1 and self.account == previous.account and self.names == previous.names):
            return False
        expected = hmac.new(hkdf(previous.ask, "luma-sync-epoch-chain-v2"), canonical(self.body()), hashlib.sha256).digest()
        if not hmac.compare_digest(self.prev, expected):
            return False
        # Signed by a member of the previous epoch (in its bundle, or approved during it) that is not being removed.
        allowed = {d: {"sign": k["sign"], "box": k["box"]} for d, k in (dict(previous.members) | dict(approved or {})).items()}
        if self.signer not in allowed or is_removed(self.removed, self.signer, allowed[self.signer]["sign"]):
            return False
        if not verify(unb64(allowed[self.signer]["sign"]), self.sig, self.signed_bytes()):
            return False
        # Nobody removed comes back; nobody new appears except devices approved during the previous epoch.
        if not set(previous.removed) <= set(self.removed) or any(is_removed(self.removed, d, k["sign"]) for d, k in self.members.items()):
            return False
        return all(device in allowed and allowed[device] == keys for device, keys in self.members.items())

    # -- derived values
    def cert(self, device: str, sign_pub: bytes, box_pub: bytes) -> bytes:
        return mac(hkdf(self.ask, "luma-sync-member-v2"), self.account, device, sign_pub, box_pub, self.epoch)

    def cert_ok(self, device: str, sign_pub: bytes, box_pub: bytes, cert: bytes) -> bool:
        return hmac.compare_digest(cert, self.cert(device, sign_pub, box_pub))

    def zone_id(self, label: str) -> str:
        return mac(hkdf(self.names, "luma-sync-zone-id-v2"), self.account, label)[:16].hex()

    def join_nonce(self, join_id: str, device: str, commitment: bytes) -> bytes:
        """The approvers' half of a join code: the same on every member device."""
        return mac(hkdf(self.ask, "luma-sync-join-v2"), self.account, join_id, device, commitment)

    def _wrap_key(self) -> bytes:
        return hkdf(self.ask, "luma-sync-zone-wrap-v1")

    def wrap_zone_key(self, zone: str, generation: int, key: bytes) -> bytes:
        return seal(self._wrap_key(), key, lp("luma-sync-zone-key-v2", self.account, zone, generation, self.epoch))

    def unwrap_zone_key(self, zone: str, generation: int, envelope: bytes) -> bytes:
        return open_sealed(self._wrap_key(), envelope, lp("luma-sync-zone-key-v2", self.account, zone, generation, self.epoch))


def removal(device: str, sign_pub_b64: str) -> str:
    """A removed device's entry: its id with the signing key it had. Those keys never come back; the id may, with new
    keys, through an approval the person checks by code."""
    return f"{device}/{sign_pub_b64}"


def is_removed(removed, device: str, sign_pub_b64: str) -> bool:
    return removal(device, sign_pub_b64) in set(removed)


# ---------------------------------------------------------------- envelopes (HPKE) and membership statements

def _info(account: str, target: str, epoch: int) -> bytes:
    return lp("luma-sync-envelope-v2", account, target, epoch)


def wrap(bundle: Bundle, target: str, box_pub: bytes) -> bytes:
    return SUITE.encrypt(bundle.to_bytes(), X25519PublicKey.from_public_bytes(box_pub), _info(bundle.account, target, bundle.epoch))


def unwrap(ciphertext: bytes, private: X25519PrivateKey, account: str, target: str, epoch: int) -> Bundle:
    try:
        bundle = Bundle.from_bytes(SUITE.decrypt(ciphertext, private, _info(account, target, epoch)))
    except CryptoError:
        raise
    except Exception as error:  # noqa: BLE001 - any failure to open is the same answer
        raise CryptoError("this key is not for this device") from error
    if bundle.account != account or bundle.epoch != epoch:
        raise CryptoError("this key belongs to another account or epoch")
    return bundle


def envelope_message(account: str, target: str, epoch: int, ciphertext: bytes) -> bytes:
    """What a wrapping member signs; the Hub checks it too (sync/store.mjs envelopeMessage)."""
    return f"luma-sync-envelope-sig-v1\0{account}\0{target}\0{epoch}\0".encode() + hashlib.sha256(ciphertext).digest()


def request_message(device: str, method: str, path: str, at: int, body: bytes) -> bytes:
    """What a device signs on every Sync request that changes something (review S5): the bearer token alone, which
    other software on the computer may be able to read, never changes or deletes anything. The Hub checks it under
    the device's registered key (sync/routes.mjs requestMessage)."""
    return f"luma-sync-request-v1\0{device}\0{method}\0{path}\0{at}\0".encode() + hashlib.sha256(body).digest()


def statement_bytes(statement: dict[str, object]) -> bytes:
    """A membership statement as signed; the Hub checks it too (sync/store.mjs statementBytes)."""
    return b"luma-sync-membership-v1\0" + canonical(statement)


def add_statement(account: str, epoch: int, device: str, sign_pub: bytes, box_pub: bytes, via: str, signer: str,
                  envelope: bytes, cert: bytes) -> dict[str, object]:
    return {"v": 1, "kind": "add", "account": account, "epoch": epoch, "device": device, "sign_pub": b64(sign_pub),
            "box_pub": b64(box_pub), "via": via, "signer": signer,
            "envelope_sha256": hashlib.sha256(envelope).hexdigest(), "cert_sha256": hashlib.sha256(cert).hexdigest()}


# ---------------------------------------------------------------- joins

def join_commitment(device_nonce: bytes) -> bytes:
    """Matches the Hub's check in joinStep('reveal'); the approver checks it again."""
    return hashlib.sha256(b"luma-sync-join-commit-v1\0" + device_nonce).digest()


def join_code(account: str, join_id: str, device: str, sign_pub: bytes, box_pub: bytes, commitment: bytes,
              approver_nonce: bytes, device_nonce: bytes) -> str:
    """Six digits over the whole transcript (review C1): length-prefixed, join id and commitment included."""
    digest = hashlib.sha256(lp("luma-sync-join-code-v2", account, join_id, device, sign_pub, box_pub, commitment,
                               approver_nonce, device_nonce)).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"


# ---------------------------------------------------------------- records, blobs, zone keys

def seal(key: bytes, plaintext: bytes, associated: bytes) -> bytes:
    """[format byte][24-byte nonce][ciphertext and tag]; the format byte is bound into the AD."""
    nonce = secrets.token_bytes(sodium.NONCE_BYTES)
    return bytes([FORMAT]) + nonce + sodium.encrypt(key, nonce, plaintext, bytes([FORMAT]) + associated)


def open_sealed(key: bytes, data: bytes, associated: bytes) -> bytes:
    if len(data) < 1 + sodium.NONCE_BYTES + sodium.TAG_BYTES or data[0] != FORMAT:
        raise CryptoError("not a sealed object of this format")
    try:
        return sodium.decrypt(key, data[1:1 + sodium.NONCE_BYTES], data[1 + sodium.NONCE_BYTES:], bytes([FORMAT]) + associated)
    except sodium.SodiumError as error:
        raise CryptoError(str(error)) from None


def record_ad(account: str, zone: str, name: str, generation: int, version: int) -> bytes:
    return lp("luma-sync-record-v2", account, zone, name, generation, version)


def blob_ad(account: str, zone: str, blob: str, generation: int) -> bytes:
    return lp("luma-sync-blob-v2", account, zone, blob, generation)


def record_name(names: bytes, zone: str, name: str) -> str:
    return mac(hkdf(names, "luma-sync-record-name-v2"), zone, name).hex()


def blob_id(zone_key: bytes, zone: str, data: bytes) -> str:
    """Keyed by the zone key generation: dedup works within one generation and resets when a removal rotates it."""
    return hmac.new(hkdf(zone_key, "luma-sync-blob-id-v2\0" + zone), data, hashlib.sha256).hexdigest()


def padme(length: int) -> int:
    """Padmé (Nikitin et al., PETS 2019): round a length up so it leaks O(log log L) bits, at most ~12% overhead."""
    if length <= 1:
        return length
    exponent = length.bit_length() - 1
    significant = exponent.bit_length()
    last_bits = exponent - significant
    mask = (1 << last_bits) - 1
    return (length + mask) & ~mask


# ---------------------------------------------------------------- the recovery key

_WORDS: list[str] | None = None


def wordlist() -> list[str]:
    global _WORDS
    if _WORDS is None:
        words = (Path(__file__).with_name("bip39-english.txt")).read_text().split()
        if len(words) != 2048 or hashlib.sha256(("\n".join(words) + "\n").encode()).hexdigest() != \
                "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda":
            raise CryptoError("the recovery word list is damaged")
        _WORDS = words
    return _WORDS


def new_recovery_entropy() -> bytes:
    return secrets.token_bytes(32)


def recovery_words(entropy: bytes) -> list[str]:
    if len(entropy) != 32:
        raise ValueError("entropy must be 32 bytes")
    bits = int.from_bytes(entropy + hashlib.sha256(entropy).digest()[:1], "big")
    words = wordlist()
    return [words[(bits >> (11 * (23 - i))) & 0x7FF] for i in range(24)]


def normalise_words(text: str) -> list[str]:
    """Spaces and capitals don't matter; numbering from a printed sheet is ignored."""
    return [word for word in (part.strip(".,;:").lower() for part in text.split()) if word and not word.isdigit()]


def recovery_entropy(words: list[str]) -> bytes:
    table = {word: index for index, word in enumerate(wordlist())}
    if len(words) != 24 or any(word not in table for word in words):
        raise CryptoError("That isn't a recovery key: it needs 24 words from the list.")
    bits = 0
    for word in words:
        bits = (bits << 11) | table[word]
    data = bits.to_bytes(33, "big")
    entropy, checksum = data[:32], data[32]
    if hashlib.sha256(entropy).digest()[0] != checksum:
        raise CryptoError("That isn't a recovery key: a word is wrong or out of order.")
    return entropy


def recovery_keys(entropy: bytes, account: str) -> RecoveryKeys:
    """One recovery key per account (ADR-054 §1.6); each use of it takes its own HKDF label."""
    salt = hashlib.sha256(b"luma-recovery-v1\0" + account.encode()).digest()[:16]
    root = Argon2id(salt=salt, **ARGON).derive(entropy)
    return RecoveryKeys(X25519PrivateKey.from_private_bytes(hkdf(root, "luma-sync-recovery-x25519-v1")),
                        Ed25519PrivateKey.from_private_bytes(hkdf(root, "luma-sync-recovery-auth-v1")))


def recovery_delete_message(account: str, at: int) -> bytes:
    return f"luma-sync-delete-account-v1\0{account}\0{at}".encode()


def wrap_recovery(bundle: Bundle) -> bytes:
    return wrap(bundle, "recovery", bundle.recovery_pub)


def random_bytes(n: int) -> bytes:
    return os.urandom(n)
