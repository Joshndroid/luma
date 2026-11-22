# SPDX-License-Identifier: Apache-2.0
"""Keys and encryption for Luma Connect Sync (ADR-054 §1).

Only vetted primitives, composed the way the ADR says:

* device keys: Ed25519 (identity, signatures) and X25519 (envelopes), from
  python-cryptography;
* envelopes: HPKE (RFC 9180) base mode, X25519 / HKDF-SHA256 /
  ChaCha20-Poly1305, from python-cryptography's ``hpke``;
* records, blobs and wrapped zone keys: XChaCha20-Poly1305 from libsodium,
  a fresh random 24-byte nonce each time, associated data binding account,
  zone, name, key generation and version;
* names: HMAC-SHA256; derived keys: HKDF-SHA256;
* the recovery key: 24 BIP-39 words (256 bits and a checksum), stretched
  with Argon2id.

The account key bundle is what an envelope carries: the account sync key
(ASK) of an epoch, the stable naming key, the recovery public key and the
members' signing keys, chained to the previous epoch's ASK.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, hpke, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.exceptions import InvalidSignature

from . import sodium

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


def mac(key: bytes, *parts: str | bytes) -> bytes:
    message = b"\0".join(part.encode() if isinstance(part, str) else part for part in parts)
    return hmac.new(key, message, hashlib.sha256).digest()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


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

    def proof(self, account: str, device: str) -> bytes:
        """The Hub checks this when the keys are registered (DEVICE_PROOF in the Hub's sync/store.mjs)."""
        return self.sign.sign(f"luma-sync-device-v1\0{account}\0{device}\0{b64(self.box_pub)}".encode())


def verify(sign_pub: bytes, signature: bytes, message: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(sign_pub).verify(signature, message)
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------- the account key bundle

@dataclass
class Bundle:
    account: str
    epoch: int
    ask: bytes                 # the account sync key of this epoch
    names: bytes               # stable: zone ids, record names and blob ids stay the same across epochs
    recovery_pub: bytes
    members: dict[str, str]    # device -> signing key (base64url), as of this epoch
    prev: bytes = b""          # HMAC under the previous epoch's ASK, empty at epoch 1

    def body(self) -> dict[str, object]:
        return {"v": 1, "account": self.account, "epoch": self.epoch, "ask": b64(self.ask), "names": b64(self.names),
                "recovery_pub": b64(self.recovery_pub), "members": dict(sorted(self.members.items()))}

    def to_bytes(self) -> bytes:
        return canonical({**self.body(), "prev": b64(self.prev)})

    @classmethod
    def from_bytes(cls, data: bytes) -> "Bundle":
        try:
            value = json.loads(data)
            if value.get("v") != 1:
                raise ValueError("version")
            bundle = cls(str(value["account"]), int(value["epoch"]), unb64(value["ask"]), unb64(value["names"]),
                         unb64(value["recovery_pub"]), {str(k): str(v) for k, v in dict(value["members"]).items()},
                         unb64(value.get("prev") or ""))
        except (ValueError, KeyError, TypeError) as error:
            raise CryptoError("the account key could not be read") from error
        if len(bundle.ask) != 32 or len(bundle.names) != 32 or len(bundle.recovery_pub) != 32:
            raise CryptoError("the account key could not be read")
        return bundle

    @classmethod
    def first(cls, account: str, recovery_pub: bytes, device: str, sign_pub: bytes) -> "Bundle":
        return cls(account, 1, secrets.token_bytes(32), secrets.token_bytes(32), recovery_pub, {device: b64(sign_pub)})

    def successor(self, members: dict[str, str]) -> "Bundle":
        """A new ASK after a revocation, chained to this one."""
        nxt = Bundle(self.account, self.epoch + 1, secrets.token_bytes(32), self.names, self.recovery_pub, members)
        nxt.prev = mac(hkdf(self.ask, "luma-sync-epoch-chain-v1"), canonical(nxt.body()))
        return nxt

    def follows(self, previous: "Bundle") -> bool:
        return (self.epoch == previous.epoch + 1 and self.account == previous.account and self.names == previous.names
                and hmac.compare_digest(self.prev, mac(hkdf(previous.ask, "luma-sync-epoch-chain-v1"), canonical(self.body()))))

    # -- derived values
    def cert(self, device: str, sign_pub: bytes, box_pub: bytes) -> bytes:
        """Membership: only a holder of this epoch's ASK can make it."""
        return mac(hkdf(self.ask, "luma-sync-member-v1"), self.account, device, sign_pub, box_pub, str(self.epoch))

    def cert_ok(self, device: str, sign_pub: bytes, box_pub: bytes, cert: bytes) -> bool:
        return hmac.compare_digest(cert, self.cert(device, sign_pub, box_pub))

    def zone_id(self, label: str) -> str:
        return mac(hkdf(self.names, "luma-sync-zone-id-v1"), self.account, label)[:16].hex()

    def join_nonce(self, join_id: str, device: str, commitment: bytes) -> bytes:
        """The approvers' half of a join code: the same on every member device."""
        return mac(hkdf(self.ask, "luma-sync-join-v1"), self.account, join_id, device, commitment)

    def wrap_zone_key(self, zone: str, generation: int, key: bytes) -> bytes:
        return seal(self.ask, key, f"luma-sync-zone-key-v1\0{self.account}\0{zone}\0{generation}\0{self.epoch}".encode())

    def unwrap_zone_key(self, zone: str, generation: int, envelope: bytes) -> bytes:
        return open_sealed(self.ask, envelope, f"luma-sync-zone-key-v1\0{self.account}\0{zone}\0{generation}\0{self.epoch}".encode())


# ---------------------------------------------------------------- envelopes (HPKE)

def _info(account: str, target: str, epoch: int) -> bytes:
    return f"luma-sync-envelope-v1\0{account}\0{target}\0{epoch}".encode()


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
    return f"luma-sync-envelope-sig-v1\0{account}\0{target}\0{epoch}\0".encode() + hashlib.sha256(ciphertext).digest()


# ---------------------------------------------------------------- joins

def join_commitment(device_nonce: bytes) -> bytes:
    """Matches the Hub's check in joinStep('reveal')."""
    return hashlib.sha256(b"luma-sync-join-commit-v1\0" + device_nonce).digest()


def join_code(account: str, device: str, sign_pub: bytes, box_pub: bytes, device_nonce: bytes, approver_nonce: bytes) -> str:
    digest = hashlib.sha256(b"\0".join([b"luma-sync-join-code-v1", account.encode(), device.encode(), sign_pub, box_pub,
                                        device_nonce, approver_nonce])).digest()
    return f"{int.from_bytes(digest[:8], 'big') % 1_000_000:06d}"


# ---------------------------------------------------------------- records, blobs, zone keys

def seal(key: bytes, plaintext: bytes, associated: bytes) -> bytes:
    nonce = secrets.token_bytes(sodium.NONCE_BYTES)
    return nonce + sodium.encrypt(key, nonce, plaintext, associated)


def open_sealed(key: bytes, data: bytes, associated: bytes) -> bytes:
    if len(data) < sodium.NONCE_BYTES + sodium.TAG_BYTES:
        raise CryptoError("too short to be encrypted")
    try:
        return sodium.decrypt(key, data[:sodium.NONCE_BYTES], data[sodium.NONCE_BYTES:], associated)
    except sodium.SodiumError as error:
        raise CryptoError(str(error)) from None


def record_ad(account: str, zone: str, name: str, generation: int, version: int) -> bytes:
    return f"luma-sync-record-v1\0{account}\0{zone}\0{name}\0{generation}\0{version}".encode()


def blob_ad(account: str, zone: str, blob: str, generation: int) -> bytes:
    return f"luma-sync-blob-v1\0{account}\0{zone}\0{blob}\0{generation}".encode()


def record_name(names: bytes, zone: str, name: str) -> str:
    return mac(hkdf(names, "luma-sync-record-name-v1"), zone, name).hex()


def blob_id(names: bytes, zone: str, data: bytes) -> str:
    """Deduplicates within a zone only; the key is the account's, so equal files in two accounts get unrelated ids."""
    return mac(hkdf(names, "luma-sync-blob-id-v1\0" + zone), data).hex()


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


def recovery_keypair(entropy: bytes, account: str) -> X25519PrivateKey:
    """One recovery key per account (ADR-054 §1.6); each use of it takes its own HKDF label."""
    salt = hashlib.sha256(b"luma-recovery-v1\0" + account.encode()).digest()[:16]
    root = Argon2id(salt=salt, **ARGON).derive(entropy)
    return X25519PrivateKey.from_private_bytes(hkdf(root, "luma-sync-recovery-x25519-v1"))


def wrap_recovery(bundle: Bundle) -> bytes:
    return wrap(bundle, "recovery", bundle.recovery_pub)


def random_bytes(n: int) -> bytes:
    return os.urandom(n)
