# SPDX-License-Identifier: Apache-2.0
"""Where this device keeps its Sync secrets: the Secret Service (ADR-054 §1.1).

Three items, each tied to this device's Connect device id:

* ``device-keys``: the Ed25519 and X25519 private keys;
* ``bundle``: the account key bundle of the current epoch;
* ``recovery``: the 24 words, kept so Settings can show them again behind a
  password check. Never logged, never in a property.

A locked or missing keyring stops Sync; nothing is written anywhere else.
Tests and headless runs can point ``LUMA_CONNECT_SYNC_KEYSTORE`` at a
directory instead (``file:/path``); that mode is never chosen on its own.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

SCHEMA = "org.projectluma.ConnectSync"
KINDS = ("device-keys", "bundle", "recovery")


class KeystoreError(Exception):
    pass


class FileKeystore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, device: str, kind: str) -> Path:
        return self.root / f"{device}.{kind}"

    def get(self, device: str, kind: str) -> bytes | None:
        try:
            return self._path(device, kind).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, device: str, kind: str, secret: bytes) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._path(device, kind)
        temporary = path.with_suffix(path.suffix + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(secret)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def delete(self, device: str, kind: str) -> None:
        self._path(device, kind).unlink(missing_ok=True)


class SecretServiceKeystore:
    def __init__(self) -> None:
        try:
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret
        except (ImportError, ValueError) as error:
            raise KeystoreError("libsecret is not available.") from error
        self.Secret = Secret
        self.schema = Secret.Schema.new(SCHEMA, Secret.SchemaFlags.NONE,
                                        {"kind": Secret.SchemaAttributeType.STRING, "device": Secret.SchemaAttributeType.STRING})

    def _attributes(self, device: str, kind: str) -> dict[str, str]:
        return {"kind": kind, "device": device}

    def get(self, device: str, kind: str) -> bytes | None:
        try:
            value = self.Secret.password_lookup_sync(self.schema, self._attributes(device, kind), None)
        except Exception as error:  # noqa: BLE001 - GLib.Error from a locked or absent keyring
            raise KeystoreError("The keyring is locked or not running.") from error
        return None if value is None else base64.b64decode(value)

    def put(self, device: str, kind: str, secret: bytes) -> None:
        label = {"device-keys": "Luma Connect Sync device keys", "bundle": "Luma Connect Sync account key",
                 "recovery": "Luma Connect Sync recovery key"}[kind]
        try:
            ok = self.Secret.password_store_sync(self.schema, self._attributes(device, kind), self.Secret.COLLECTION_DEFAULT,
                                                 label, base64.b64encode(secret).decode(), None)
        except Exception as error:  # noqa: BLE001
            raise KeystoreError("The keyring is locked or not running.") from error
        if not ok:
            raise KeystoreError("The keyring did not store the key.")

    def delete(self, device: str, kind: str) -> None:
        try:
            self.Secret.password_clear_sync(self.schema, self._attributes(device, kind), None)
        except Exception as error:  # noqa: BLE001
            raise KeystoreError("The keyring is locked or not running.") from error


def open_keystore():
    configured = os.environ.get("LUMA_CONNECT_SYNC_KEYSTORE", "")
    if configured.startswith("file:"):
        return FileKeystore(Path(configured[5:]))
    return SecretServiceKeystore()


def encode(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True).encode()
