# SPDX-License-Identifier: Apache-2.0
"""The sync engine: zones, change tokens, records and blobs (ADR-054 §2).

A zone is named locally by a label ("apps", "appdata:<app id>", "keychain")
and on the Hub by an opaque id derived from the account's naming key. Every
record is ``{"n": name, "v": value}`` sealed under the zone key of its
generation, with the version it will have bound into the associated data.
The local mirror keeps the last version seen of each record, which is the
base of the next write; a stale write comes back as a conflict and the
caller merges.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time
import zlib
from pathlib import Path

from . import crypto
from .crypto import CryptoError, b64, unb64
from .hub import Hub, HubError

MAX_BLOB = 4 * 1024 * 1024          # the Hub's limit for one stored piece (review M5)
MAX_RECORD_PLAIN = 4 * 1024 * 1024  # a record's inflated size; more is refused (review L2)
BLOB_ID = re.compile(r"[0-9a-f]{64}")


def state_directory(device: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "luma" / "connect-sync" / device


class LocalState:
    """Small values the device keeps for itself: change tokens, the record mirror, app states."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        os.chmod(path, 0o600)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tokens(zone TEXT PRIMARY KEY, seq INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS mirror(zone TEXT NOT NULL, name TEXT NOT NULL, version INTEGER NOT NULL,
                deleted INTEGER NOT NULL, value TEXT, writer TEXT, updated_at INTEGER, PRIMARY KEY(zone, name));
        """)

    def get(self, key: str, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key: str, value) -> None:
        with self.lock:
            self.db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def delete(self, key: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM kv WHERE key=?", (key,))

    def token(self, zone: str) -> int:
        with self.lock:
            row = self.db.execute("SELECT seq FROM tokens WHERE zone=?", (zone,)).fetchone()
        return row[0] if row else 0

    def set_token(self, zone: str, seq: int) -> None:
        with self.lock:
            self.db.execute("INSERT INTO tokens VALUES(?,?) ON CONFLICT(zone) DO UPDATE SET seq=excluded.seq", (zone, seq))

    def mirror(self, zone: str, name: str) -> tuple[int, object | None]:
        with self.lock:
            row = self.db.execute("SELECT version,deleted,value FROM mirror WHERE zone=? AND name=?", (zone, name)).fetchone()
        if not row:
            return 0, None
        return row[0], (None if row[1] else json.loads(row[2]))

    def mirror_all(self, zone: str) -> dict[str, tuple[int, object, str]]:
        with self.lock:
            rows = self.db.execute("SELECT name,version,value,writer FROM mirror WHERE zone=? AND deleted=0", (zone,)).fetchall()
        return {name: (version, json.loads(value), writer) for name, version, value, writer in rows}

    def set_mirror(self, zone: str, name: str, version: int, value, writer: str = "", updated: int = 0) -> None:
        with self.lock:
            self.db.execute("INSERT INTO mirror VALUES(?,?,?,?,?,?,?) ON CONFLICT(zone,name) DO UPDATE SET version=excluded.version,"
                            "deleted=excluded.deleted,value=excluded.value,writer=excluded.writer,updated_at=excluded.updated_at",
                            (zone, name, version, 1 if value is None else 0, None if value is None else json.dumps(value), writer, updated))

    def forget_zone(self, zone: str) -> None:
        with self.lock:
            self.db.execute("DELETE FROM mirror WHERE zone=?", (zone,))
            self.db.execute("DELETE FROM tokens WHERE zone=?", (zone,))


class Engine:
    def __init__(self, hub: Hub, account, local: LocalState) -> None:
        self.hub = hub
        self.account = account
        self.local = local
        self._zones: dict[str, dict] = {}
        self._keys: dict[tuple[str, int, int], bytes] = {}

    # ------------------------------------------------------------ zones
    def bundle(self):
        bundle = self.account.bundle()
        if bundle is None:
            raise HubError(403, "This device has not joined Sync.", "not_member")
        return bundle

    def zone_id(self, label: str) -> str:
        return self.bundle().zone_id(label)

    def refresh_zones(self) -> dict[str, dict]:
        self._zones = {zone["zone"]: zone for zone in self.hub.get("zones")["zones"]}
        # Every zone's generation is remembered, not only those this device wrote, so the floor set at the next
        # account key change covers them all (review S3).
        seen = self.local.get("zone_generations", {})
        grown = {zone: info["generation"] for zone, info in self._zones.items()
                 if isinstance(info.get("generation"), int) and info["generation"] > seen.get(zone, 0)}
        if grown:
            self.local.put("zone_generations", {**seen, **grown})
        return self._zones

    def zone(self, label: str, create: bool = True, owner: str | None = None) -> dict | None:
        zone = self.zone_id(label)
        if zone not in self._zones:
            self.refresh_zones()
        if zone not in self._zones and create:
            bundle = self.bundle()
            key = secrets.token_bytes(32)
            try:
                self.hub.put(f"zones/{zone}", {"generation": 1, "epoch": bundle.epoch, "envelope": b64(bundle.wrap_zone_key(zone, 1, key)),
                                               **({"owner": owner} if owner else {})})
            except HubError as error:
                if error.code != "zone_exists":
                    raise
            self.refresh_zones()
        return self._zones.get(zone)

    def key(self, zone: str, generation: int) -> bytes:
        bundle = self.bundle()
        cached = self._keys.get((zone, generation, bundle.epoch))
        if cached:
            return cached
        info = self._zones.get(zone) or self.refresh_zones().get(zone)
        entry = next((k for k in (info or {}).get("keys", []) if k["generation"] == generation), None)
        if entry is None or entry["epoch"] != bundle.epoch:
            info = self.refresh_zones().get(zone)
            entry = next((k for k in (info or {}).get("keys", []) if k["generation"] == generation), None)
        if entry is None:
            raise CryptoError("a zone key is missing")
        key = bundle.unwrap_zone_key(zone, generation, unb64(entry["envelope"]))
        self._keys[(zone, generation, bundle.epoch)] = key
        return key

    def current(self, zone: str) -> tuple[int, bytes]:
        """The generation new writes use. Never one older than this device has seen or than the floor set at the last
        account key change: the Hub reports generations, devices decide which they accept (review H2.4)."""
        generation = (self._zones.get(zone) or self.refresh_zones()[zone])["generation"]
        seen = self.local.get("zone_generations", {})
        floor = self.local.get("zone_floor", {}).get(zone, 0)
        if generation < max(seen.get(zone, 0), floor):
            generation = (self.refresh_zones().get(zone) or {}).get("generation", 0)
            if generation < max(seen.get(zone, 0), floor):
                raise HubError(409, "This zone's key hasn't been replaced yet; writing waits.", "stale_generation")
        if generation > seen.get(zone, 0):
            self.local.put("zone_generations", {**seen, zone: generation})
        return generation, self.key(zone, generation)

    def delete_zone(self, label: str) -> None:
        zone = self.zone_id(label)
        try:
            self.hub.delete(f"zones/{zone}")
        except HubError as error:
            if error.status != 404:
                raise
        self.local.forget_zone(zone)
        self._zones.pop(zone, None)

    # ------------------------------------------------------------ records
    def _open(self, zone: str, row: dict) -> tuple[str, object | None, bool, str]:
        """(name, value, deleted, writer) from a row; a deletion is a sealed tombstone too (review M2)."""
        if not row.get("ciphertext"):
            raise CryptoError("a record has no sealed body")
        key = self.key(zone, row["generation"])
        plain = crypto.open_sealed(key, unb64(row["ciphertext"]),
                                   crypto.record_ad(self.bundle().account, zone, row["name"], row["generation"], row["version"]))
        inflater = zlib.decompressobj()
        text = inflater.decompress(plain, MAX_RECORD_PLAIN)
        if inflater.unconsumed_tail:
            raise CryptoError("a record is larger than allowed")
        body = json.loads(text)
        if crypto.record_name(self.bundle().names, zone, body["n"]) != row["name"]:
            raise CryptoError("a record's name does not match its contents")
        deleted = body.get("d") is True
        if deleted != bool(row["deleted"]):
            raise CryptoError("a record's deletion flag does not match its contents")
        return body["n"], None if deleted else body["v"], deleted, str(body.get("w") or "")

    def fetch(self, label: str) -> list[tuple[str, object | None, str]]:
        """Records changed since this device last looked: (name, value or None when deleted, writer)."""
        info = self.zone(label, create=False)
        if info is None:
            return []
        zone = info["zone"]
        changed = []
        after = self.local.token(zone)
        while True:
            page = self.hub.get(f"zones/{zone}/changes?after={after}&limit=200")
            for row in page["records"]:
                try:
                    name, value, _deleted, writer = self._open(zone, row)
                except (CryptoError, ValueError, KeyError):
                    continue        # unreadable or forged: ignored, never applied
                self.local.set_mirror(zone, name, row["version"], value, writer, row["updated_at"])
                changed.append((name, value, writer))
            after = page["seq"]
            if not page["more"]:
                break
        self.local.set_token(zone, after)
        return changed

    def get(self, label: str, name: str) -> tuple[int, object | None]:
        return self.local.mirror(self.zone_id(label), name)

    def all(self, label: str) -> dict[str, tuple[int, object, str]]:
        return self.local.mirror_all(self.zone_id(label))

    def save(self, label: str, name: str, value, *, owner: str | None = None) -> tuple[bool, object | None]:
        """Write one record on top of the version this device last saw. (False, current) on a conflict."""
        info = self.zone(label, owner=owner)
        zone = info["zone"]  # type: ignore[index]
        base, _ = self.local.mirror(zone, name)
        hashed = crypto.record_name(self.bundle().names, zone, name)
        generation, key = self.current(zone)
        body = {"n": name, "w": self.hub.device, "t": int(time.time() * 1000)}
        body.update({"d": True} if value is None else {"v": value})
        plain = zlib.compress(crypto.canonical(body), 6)
        entry: dict[str, object] = {"name": hashed, "base": base, "generation": generation,
                                    "ciphertext": b64(crypto.seal(key, plain, crypto.record_ad(self.bundle().account, zone, hashed, generation, base + 1)))}
        if value is None:
            entry["deleted"] = True
        answer = self.hub.post(f"zones/{zone}/records", {"records": [entry]})
        if answer["saved"]:
            self.local.set_mirror(zone, name, answer["saved"][0]["version"], value, self.hub.device, int(time.time() * 1000))
            return True, value
        current = answer["conflicts"][0]
        now, writer = None, current.get("writer", "")
        if current.get("ciphertext"):
            try:
                _, now, _deleted, writer = self._open(zone, current)
            except (CryptoError, ValueError, KeyError):
                now = None
        self.local.set_mirror(zone, name, current["version"], now, writer, current.get("updated_at", 0))
        return False, now

    # ------------------------------------------------------------ blobs (one ciphertext each, the same from any source)
    def blob_id(self, zone: str, data: bytes) -> str:
        _generation, key = self.current(zone)
        return crypto.blob_id(key, zone, data)

    def missing(self, zone: str, ids: list[str]) -> set[str]:
        out: set[str] = set()
        for start in range(0, len(ids), 1000):
            out.update(self.hub.post(f"zones/{zone}/blobs/check", {"ids": ids[start:start + 1000]})["missing"])
        return out

    def seal_blob(self, zone: str, data: bytes, pad_to: int | None = None) -> tuple[str, int, bytes]:
        """(id, generation, sealed bytes): generation (4) ‖ format ‖ nonce ‖ AEAD(length (4) ‖ data ‖ zero padding).

        No compression: sizes would then depend on content. ``pad_to`` is the plaintext length to pad to (Padmé, by the caller).
        """
        generation, key = self.current(zone)
        blob = crypto.blob_id(key, zone, data)
        target = max(len(data), pad_to or 0)
        body = len(data).to_bytes(4, "big") + data + bytes(target - len(data))
        sealed = generation.to_bytes(4, "big") + crypto.seal(key, body, crypto.blob_ad(self.bundle().account, zone, blob, generation))
        if len(sealed) > MAX_BLOB:
            raise ValueError("a piece would be larger than the Hub accepts")
        return blob, generation, sealed

    def upload(self, zone: str, blob: str, sealed: bytes) -> None:
        self._retrying(lambda: self.hub.put_blob(zone, blob, sealed))

    def put_blob(self, zone: str, data: bytes, pad_to: int | None = None) -> tuple[str, int]:
        blob, generation, sealed = self.seal_blob(zone, data, pad_to)
        if blob in self.missing(zone, [blob]):
            self.upload(zone, blob, sealed)
        return blob, generation

    def get_blob(self, zone: str, blob: str) -> bytes:
        if not BLOB_ID.fullmatch(blob):
            raise CryptoError("not a piece id")
        sealed = self._retrying(lambda: self.hub.get_blob(zone, blob))
        if len(sealed) > MAX_BLOB or len(sealed) < 5:
            raise CryptoError("a piece has the wrong size")
        generation = int.from_bytes(sealed[:4], "big")
        key = self.key(zone, generation)
        body = crypto.open_sealed(key, sealed[4:], crypto.blob_ad(self.bundle().account, zone, blob, generation))
        length = int.from_bytes(body[:4], "big")
        if length > len(body) - 4 or any(body[4 + length:]):
            raise CryptoError("a piece is malformed")
        data = body[4:4 + length]
        if crypto.blob_id(key, zone, data) != blob:
            raise CryptoError("a piece did not match its name")
        return data

    def list_blobs(self, zone: str) -> list[dict]:
        return self.hub.get(f"zones/{zone}/blobs")["blobs"]

    def delete_blobs(self, zone: str, ids: list[str]) -> None:
        for start in range(0, len(ids), 1000):
            self.hub.post(f"zones/{zone}/blobs/delete", {"ids": ids[start:start + 1000]})

    @staticmethod
    def _retrying(action, attempts: int = 4):
        for attempt in range(attempts):
            try:
                return action()
            except HubError as error:
                if error.status == 429 and error.retry_after and error.retry_after <= 120 and attempt + 1 < attempts:
                    time.sleep(error.retry_after)
                    continue
                raise
        return None
