# SPDX-License-Identifier: Apache-2.0
"""Where this device keeps its Sync secrets (ADR-054 §1.1, security review H3).

Three secrets, each tied to this device's Connect device id:

* ``device-keys``: the Ed25519 and X25519 private keys;
* ``bundle``: the account key bundle of the current epoch;
* ``recovery``: the 24 words, only on the device that made the current
  recovery key, so Settings can show them behind a password check.

They are *not* in the Secret Service: any app allowed to talk to it could
read them. Each is a file in ``~/.local/share/luma/sync/keys`` sealed with
``systemd-creds --user`` to this user on this machine (and its TPM when it
has one); only the host's credential service opens it, which no sandbox can
reach. Secrets go over stdin and stdout, never argv. A keystore that can't
seal stops Sync; nothing falls back to a weaker store. Older previews that
kept them in the Secret Service are moved over and the items deleted.
Tests point ``LUMA_CONNECT_SYNC_KEYSTORE`` at a plain directory
(``file:/path``); that mode is never chosen on its own.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
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


class CredsKeystore:
    """Secrets sealed with systemd-creds --user."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "luma" / "sync" / "keys"

    def _path(self, device: str, kind: str) -> Path:
        if kind not in KINDS or not device.replace("-", "").isalnum():
            raise KeystoreError("not a keystore entry")
        return self.root / f"{device}.{kind}.cred"

    @staticmethod
    def _creds(*arguments: str, data: bytes | None = None) -> bytes:
        reason = ""
        # The credential service can be briefly unreachable while the session starts; try a few times.
        for attempt in range(4):
            try:
                result = subprocess.run(["systemd-creds", "--user", *arguments], input=data, capture_output=True, timeout=60)
            except (OSError, subprocess.SubprocessError) as error:
                reason = type(error).__name__
            else:
                if not result.returncode:
                    return result.stdout
                # systemd-creds' own message; it never contains the secret.
                reason = (result.stderr.decode(errors="replace").strip().splitlines() or [f"exit {result.returncode}"])[-1][:200]
                if "TPM" in reason or "integrity" in reason:
                    # Sealed to a security chip that was reset or replaced: no retry will open it.
                    raise KeystoreError("This device's Sync keys can't be opened any more (its security chip was reset or "
                                        "replaced). Remove it from another device, then join again.")
            time.sleep(1 + 2 * attempt)
        raise KeystoreError(f"This device's credential service refused Sync's keys ({reason}).")

    def get(self, device: str, kind: str) -> bytes | None:
        path = self._path(device, kind)
        if not path.is_file():
            return self._migrate(device, kind)
        return self._creds("decrypt", f"--name=luma-sync-{kind}", str(path), "-")

    def put(self, device: str, kind: str, secret: bytes) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        path = self._path(device, kind)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            self._creds("encrypt", f"--name=luma-sync-{kind}", "-", str(temporary), data=secret)
            os.chmod(temporary, 0o600)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, device: str, kind: str) -> None:
        self._path(device, kind).unlink(missing_ok=True)

    def _migrate(self, device: str, kind: str) -> bytes | None:
        try:
            old = SecretServiceKeystore()
            value = old.get(device, kind)
        except KeystoreError:
            return None
        if value is None:
            return None
        self.put(device, kind, value)
        try:
            old.delete(device, kind)
        except KeystoreError:
            pass
        return value


def open_keystore():
    configured = os.environ.get("LUMA_CONNECT_SYNC_KEYSTORE", "")
    if configured.startswith("file:"):
        return FileKeystore(Path(configured[5:]))
    return CredsKeystore()


def encode(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True).encode()
