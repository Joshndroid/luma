# SPDX-License-Identifier: Apache-2.0
"""App state for apps that know nothing about Luma (ADR-054 §4), format v2.

An app's data folder (``~/.var/app/<id>`` for a Flatpak, its ADR-052 data
folder for a store app) is a snapshot tree: a manifest of every file with
its mode, size, modification time and SHA-256, and the encrypted pieces
that hold the bytes. The zone's ``head`` record names the newest snapshot.

What the Hub can learn from the pieces (security review M1):

* files up to 1 MiB are packed together, and a pack is padded to its Padmé
  length; files over 1 MiB are cut into fixed 1 MiB blocks after padding the
  whole file to its Padmé length with random blocks, so only that bucket of
  the length shows, never content-dependent boundaries;
* every piece of a snapshot is checked in one batch and uploaded in random
  order, so the Hub can't tell where one file ends;
* piece ids are keyed by the zone key generation (engine.blob_id).

An app's zone is named by its owner as well as its id (review H4): a
Flatpak by its remote's URL and signing key fingerprints, a store app by its
distribution, both resolved on this device. A same-id app from elsewhere
gets a different zone. The manifest names the owner and apply refuses a
mismatch.

Every file is opened beneath the folder one component at a time with
O_NOFOLLOW and checked with fstat against the walk (review M3); nothing is
written through a symlink, no symlink may point out of the folder or use
``..`` (L1), and the app must still be closed right before the swap.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import random
import secrets
import shutil
import stat
import subprocess
import tempfile
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import crypto
from .engine import Engine, LocalState

BLOCK = 1024 * 1024              # large files are cut into blocks of exactly this size
LARGE = 1024 * 1024              # files above this are blocks; smaller ones are packed
PACK_LIMIT = 3 * 1024 * 1024 + 512 * 1024   # a pack is flushed before it would pass this (its Padmé length stays under 4 MiB)
MANIFEST_PIECE = 1024 * 1024
MAX_FILE = 1024 ** 3             # ADR-054 §4.2: files over 1 GB never sync
BUDGET = 2 * 1024 ** 3           # §4.6: 2 GB per app folder unless the person opts in
QUIET_SECONDS = 20
VERSION_DAYS = 30
SQLITE_SUFFIXES = ("-wal", "-shm", "-journal")
DEFAULT_EXCLUDES = [
    "cache", ".cache", "tmp", "*.lock", "lock", "Singleton*", "*.pid", ".flatpak-info",
    "GPUCache", "ShaderCache", "GrShaderCache", "DawnCache", "shader-cache", "mesa_shader_cache*", "*.sock", ".luma-sync-*",
    # Locked with this device's portal secret; its items travel through the keychain zone instead (portal_secrets).
    "keyrings",
]
DATA_DIRS = [Path("/usr/share/luma/sync"), Path(__file__).resolve().parent.parent / "data" / "sync"]


class SnapshotAborted(Exception):
    """A file changed while it was read, the app opened, or a manifest broke a rule; nothing was changed."""


log = logging.getLogger("luma-connect-sync")


@dataclass
class Target:
    app: str
    kind: str                         # flatpak | store
    folder: Path
    name: str = ""
    owner: str = ""                   # who publishes it, resolved on this device (review H4)
    dconf: list[str] = field(default_factory=list)

    @property
    def zone(self) -> str:
        return f"appdata:{self.owner}:{self.app}"

    @property
    def keychain_name(self) -> str:
        return f"portal:{self.owner}:{self.app}"

    @property
    def syncs(self) -> bool:
        """An app whose owner can't be told apart from an impostor stays on this device."""
        return bool(self.owner) and not self.owner.startswith("local:")


def _lines(kind: str) -> list[str]:
    out: list[str] = []
    for root in DATA_DIRS:
        folder = root / kind
        if folder.is_dir():
            for path in sorted(folder.iterdir()):
                if path.suffix in {".conf", ".txt"} and not path.name.startswith(".") and path.is_file():
                    out += [line.strip() for line in path.read_text(errors="replace").splitlines() if line.strip() and not line.startswith("#")]
    return out


def excludes() -> list[str]:
    return DEFAULT_EXCLUDES + _lines("exclude.d")


def denied(app: str) -> bool:
    """Apps that sync themselves (mail, browsers, Steam, cloud drives) keep doing so."""
    return any(fnmatch.fnmatchcase(app, pattern) for pattern in _lines("deny.d"))


def is_excluded(relative: str, patterns: list[str], top_only: tuple[str, ...] = ("cache", ".cache", "tmp")) -> bool:
    parts = relative.split("/")
    if parts[0] in top_only:
        return True
    return any(fnmatch.fnmatchcase(part, pattern) for part in parts for pattern in patterns if pattern not in top_only)


def unit_of(relative: str) -> str:
    for suffix in SQLITE_SUFFIXES:
        if relative.endswith(suffix):
            return relative[: -len(suffix)]
    return relative


# ---------------------------------------------------------------- owners (review H4)

def _gpg_fingerprints(keyring: Path) -> list[str]:
    if not keyring.is_file():
        return []
    try:
        # A throwaway home: gpg refuses a missing one, and the person's own keyring is never touched.
        with tempfile.TemporaryDirectory(prefix="luma-sync-gpg-") as home:
            out = subprocess.run(["gpg", "--batch", "--no-options", "--no-autostart", "--homedir", home, "--with-colons",
                                  "--show-keys", str(keyring)], capture_output=True, text=True, timeout=20, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:") and len(line.split(":")) > 9})


def flatpak_owner(app: str, installed: dict[str, dict[str, str]] | None = None, device: str = "") -> str:
    """flatpak:<hash of the remote URL and its signing keys> on this device, or local:<device> when unsigned or unknown."""
    from . import apps
    info = (installed or apps.installed_flatpaks()).get(app)
    if not info:
        return f"local:{device}"
    remote = next((r for r in apps.remotes() if r["name"] == info["remote"] and r["installation"] == info["installation"]), None)
    if not remote:
        return f"local:{device}"
    base = Path.home() / ".local/share/flatpak/repo" if info["installation"] == "user" else Path("/var/lib/flatpak/repo")
    fingerprints = _gpg_fingerprints(base / f"{remote['name']}.trustedkeys.gpg")
    if not fingerprints:
        return f"local:{device}"
    return "flatpak:" + hashlib.sha256(crypto.lp(remote["url"].rstrip("/"), *fingerprints)).hexdigest()[:32]


def store_owner(record: dict[str, object], device: str = "") -> str:
    """A store app built from its distribution's own repositories: userland:<name>. One that needed others stays here."""
    tree = dict(record.get("tree") or {})
    origin = dict(tree.get("origin") or {})
    if origin.get("added_repositories"):
        return f"local:{device}"
    return f"userland:{tree.get('userland')}"


# ---------------------------------------------------------------- dconf (review finding 2, L5)

# Settings no app may own: the desktop's, the shell's, the session's, terminals that run commands, accounts, crypto, Luma's.
RESERVED_DCONF = ("/org/gnome/desktop/", "/org/gnome/shell/", "/org/gnome/mutter/", "/org/gnome/settings-daemon/",
                  "/org/gnome/terminal/", "/org/gnome/ptyxis/", "/org/gnome/online-accounts/", "/org/gnome/crypto/",
                  "/org/gnome/system/", "/org/gnome/login-screen/", "/org/freedesktop/", "/org/projectluma/", "/ca/desrt/", "/system/")


def allowed_dconf(app: str) -> set[str]:
    """The dconf paths an app's state may carry: its own id as a path, and nothing that is or holds a reserved one."""
    if not app or any(not part.replace("_", "").replace("-", "").isalnum() for part in app.split(".")) or app.count(".") < 2:
        return set()
    paths = {"/" + app.replace(".", "/") + "/", "/" + app.lower().replace(".", "/") + "/"}
    # If either spelling touches a reserved path, the app gets none: dconf paths are case-sensitive but people's apps aren't.
    if any(path.lower().startswith(reserved) or reserved.startswith(path.lower()) for path in paths for reserved in RESERVED_DCONF):
        return set()
    return paths


def dconf_paths(app: str) -> list[str]:
    return [path for path in sorted(allowed_dconf(app)) if dconf_dump(path)]


def dconf_dump(path: str) -> str:
    try:
        return subprocess.run(["dconf", "dump", path], capture_output=True, text=True, timeout=20, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def dconf_load(path: str, dump: str) -> None:
    subprocess.run(["dconf", "reset", "-f", path], capture_output=True, timeout=20, check=False)
    if dump.strip():
        subprocess.run(["dconf", "load", path], input=dump, text=True, capture_output=True, timeout=20, check=True)


# ---------------------------------------------------------------- manifest rules (review finding 4, L1, L3)

def safe_relative(rel: object) -> bool:
    """A manifest path: relative, normalised, no '..', no empty or '.' parts, no NUL."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\0" in rel:
        return False
    return all(part not in {"", ".", ".."} for part in rel.split("/"))


def safe_link(rel: str, target: object) -> bool:
    """A symlink target: relative, no '..' at all, no NUL. A chain of links can then never leave the folder."""
    if not isinstance(target, str) or not target or target.startswith("/") or "\0" in target:
        return False
    return all(part != ".." for part in target.split("/"))


def _part_ok(part: object) -> bool:
    return (isinstance(part, list) and len(part) == 4 and isinstance(part[0], str) and len(part[0]) == 64
            and all(c in "0123456789abcdef" for c in part[0]) and all(isinstance(x, int) and x >= 0 for x in part[1:])
            and part[1] >= 1)


def check_manifest(target: Target, manifest: dict) -> dict:
    """Refuse a manifest that names another owner or app, or a path that leaves the folder; drop links out of it and
    dconf paths that aren't the app's own."""
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise SnapshotAborted("the manifest has no file list")
    if manifest.get("app") != target.app or manifest.get("owner") != target.owner:
        raise SnapshotAborted("the manifest belongs to another app or publisher")
    links = set()
    clean = {}
    for rel, entry in files.items():
        if not safe_relative(rel) or not isinstance(entry, dict) or entry.get("t") not in {"f", "d", "l"}:
            raise SnapshotAborted("the manifest names a path outside the app's folder")
        if entry["t"] == "l":
            if not safe_link(rel, entry.get("to")):
                continue
            links.add(rel)
        if entry["t"] == "f":
            if not (isinstance(entry.get("s"), int) and 0 <= entry["s"] <= MAX_FILE and isinstance(entry.get("h"), str)
                    and all(_part_ok(p) for p in entry.get("p") or []) and all(_part_ok(p + [0, 0]) for p in entry.get("pad") or [])):
                raise SnapshotAborted("the manifest describes a file it can't hold")
            if sum(p[3] for p in entry.get("p") or []) != entry["s"]:
                raise SnapshotAborted("the manifest's pieces don't add up to the file")
        clean[rel] = entry
    for rel in clean:
        parts = rel.split("/")
        if any("/".join(parts[:i]) in links for i in range(1, len(parts))):
            raise SnapshotAborted("the manifest puts a file below a symlink")
    allowed = allowed_dconf(target.app) if target.kind == "flatpak" else set()
    dconf = {path: dump for path, dump in (manifest.get("dconf") or {}).items() if path in allowed and isinstance(dump, str)}
    return {**manifest, "files": clean, "dconf": dconf}


# ---------------------------------------------------------------- opening files beneath the folder (review M3)

def open_folder(folder: Path) -> int:
    return os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def open_beneath(root: int, rel: str, flags: int = os.O_RDONLY) -> int:
    """Open rel beneath the folder fd one component at a time, never following a symlink (like openat2
    RESOLVE_BENEATH|RESOLVE_NO_SYMLINKS). Raises OSError on a symlink anywhere in the path."""
    if not safe_relative(rel):
        raise OSError("unsafe path")
    parts = rel.split("/")
    current = os.dup(root)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            os.close(current)
            current = nxt
        return os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=current)
    finally:
        os.close(current)


def open_verified(root: int, rel: str, info: os.stat_result) -> int:
    """A regular file that is the same inode, size and mtime the walk saw."""
    fd = open_beneath(root, rel)
    now = os.fstat(fd)
    if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) != \
            (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
        os.close(fd)
        raise SnapshotAborted(f"{rel} changed while it was read")
    return fd


# ---------------------------------------------------------------- reading a folder

def _stat_key(info: os.stat_result) -> list[int]:
    return [info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode)]


def walk(folder: Path, patterns: list[str]):
    """(relative path, lstat) for everything that may sync, in a stable order. Never follows a symlink."""
    if folder.is_symlink() or not folder.is_dir():
        return
    stack = [""]
    while stack:
        relative = stack.pop()
        base = folder / relative if relative else folder
        try:
            entries = sorted(os.scandir(base), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            rel = f"{relative}/{entry.name}" if relative else entry.name
            if is_excluded(rel, patterns):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                yield rel, info
                stack.append(rel)
            elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                if stat.S_ISREG(info.st_mode) and info.st_size > MAX_FILE:
                    continue
                yield rel, info


def quick_state(folder: Path, patterns: list[str]) -> dict[str, list]:
    """Metadata only: enough to tell whether anything changed since a manifest."""
    out = {}
    for rel, info in walk(folder, patterns):
        if stat.S_ISDIR(info.st_mode):
            out[rel] = ["d", stat.S_IMODE(info.st_mode)]
        elif stat.S_ISLNK(info.st_mode):
            out[rel] = ["l", os.readlink(folder / rel)]
        else:
            out[rel] = ["f", *_stat_key(info)]
    return out


def manifest_state(manifest: dict | None) -> dict[str, list]:
    out = {}
    for rel, entry in ((manifest or {}).get("files") or {}).items():
        if entry["t"] == "d":
            out[rel] = ["d", entry["m"]]
        elif entry["t"] == "l":
            out[rel] = ["l", entry["to"]]
        else:
            out[rel] = ["f", entry["s"], entry["mt"], entry["m"]]
    return out


def folder_bytes(folder: Path, patterns: list[str]) -> int:
    return sum(info.st_size for _rel, info in walk(folder, patterns) if stat.S_ISREG(info.st_mode))


# ---------------------------------------------------------------- uploads: spooled, checked in one batch, sent in random order

class Uploads:
    def __init__(self, engine: Engine, zone: str, spool: Path) -> None:
        self.engine, self.zone = engine, zone
        self.spool = spool
        spool.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.pending: dict[str, Path] = {}
        self.bytes = 0

    def add(self, data: bytes, pad_to: int | None = None) -> list:
        """Seal one piece now (to the spool); returns its manifest reference [blob, generation]."""
        blob, generation, sealed = self.engine.seal_blob(self.zone, data, pad_to)
        if blob not in self.pending:
            path = self.spool / blob
            path.write_bytes(sealed)
            self.pending[blob] = path
        return [blob, generation]

    def send(self) -> int:
        ids = list(self.pending)
        random.SystemRandom().shuffle(ids)        # the check, too, doesn't show which pieces belong together (S6)
        missing = self.engine.missing(self.zone, ids)
        order = [blob for blob in ids if blob in missing]
        random.SystemRandom().shuffle(order)
        for blob in order:
            sealed = self.pending[blob].read_bytes()
            self.engine.upload(self.zone, blob, sealed)
            self.bytes += len(sealed)
        return self.bytes

    def close(self) -> None:
        shutil.rmtree(self.spool, ignore_errors=True)


# ---------------------------------------------------------------- the sync of one app

class AppData:
    def __init__(self, engine: Engine, local: LocalState, device: str, device_name: str = "") -> None:
        self.engine = engine
        self.local = local
        self.device = device
        self.device_name = device_name
        self.root = local.path.parent
        (self.root / "manifests").mkdir(mode=0o700, exist_ok=True)
        self.patterns = excludes()
        self.versions_root = Path.home() / ".local/share/luma/connect-sync/versions"
        self.still_quiet: Callable[[str], bool] = lambda app: True    # the service sets this from its watcher

    # -- local bookkeeping
    def info(self, app: str) -> dict:
        return self.local.get(f"app:{app}", {})

    def set_info(self, app: str, **values) -> dict:
        info = {**self.info(app), **values}
        self.local.put(f"app:{app}", info)
        return info

    def enabled(self, app: str) -> bool:
        return self.info(app).get("enabled", True) and not denied(app)

    def _manifest_path(self, target: Target) -> Path:
        return self.root / "manifests" / f"{hashlib.sha256(target.zone.encode()).hexdigest()[:24]}.json"

    def base_manifest(self, target: Target) -> dict | None:
        try:
            return json.loads(self._manifest_path(target).read_text())
        except (FileNotFoundError, ValueError):
            return None

    def _keep_base(self, target: Target, manifest: dict, snapshot: str) -> None:
        path = self._manifest_path(target)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest))
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        self.set_info(target.app, base=snapshot, owner=target.owner)

    def local_changed(self, target: Target, base: dict | None) -> bool:
        dconf = dconf_paths(target.app) if target.kind == "flatpak" else []
        if base is None:
            return bool(quick_state(target.folder, self.patterns)) or any(dconf_dump(p) for p in dconf)
        if quick_state(target.folder, self.patterns) != manifest_state(base):
            return True
        return any(dconf_dump(path) != dump for path, dump in (base.get("dconf") or {}).items()) or \
            bool(set(dconf) - set((base.get("dconf") or {}).keys()))

    # -- snapshot (upload)
    def snapshot(self, target: Target, parent: str | None, base: dict | None, version_date: int = 0) -> tuple[str, dict]:
        """Read the closed app's folder and upload what is new. Returns (snapshot id, manifest)."""
        if target.folder.is_symlink():
            raise SnapshotAborted("the app's data folder is a symlink")
        zone = self.engine.zone(target.zone)["zone"]  # type: ignore[index]
        previous = {rel: entry for rel, entry in ((base or {}).get("files") or {}).items() if entry["t"] == "f"}
        files: dict[str, dict] = {}
        uploads = Uploads(self.engine, zone, self.root / f".spool-{secrets.token_hex(4)}")
        pack: list[tuple[str, bytes]] = []
        pack_size = 0
        random_bytes = secrets.token_bytes

        def flush_pack() -> None:
            nonlocal pack, pack_size
            if not pack:
                return
            data = b"".join(content for _rel, content in pack)
            blob, generation = uploads.add(data, crypto.padme(len(data)))
            offset = 0
            for rel, content in pack:
                files[rel]["p"] = [[blob, generation, offset, len(content)]]
                offset += len(content)
            pack, pack_size = [], 0

        try:
            root = open_folder(target.folder) if target.folder.is_dir() else None
            units: dict[str, list[str]] = {}
            for rel, info in walk(target.folder, self.patterns):
                if stat.S_ISDIR(info.st_mode):
                    files[rel] = {"t": "d", "m": stat.S_IMODE(info.st_mode)}
                    continue
                if stat.S_ISLNK(info.st_mode):
                    files[rel] = {"t": "l", "to": os.readlink(target.folder / rel)}
                    continue
                units.setdefault(unit_of(rel), []).append(rel)
                old = previous.get(rel)
                key = _stat_key(info)
                if old and [old["s"], old["mt"], old["m"]] == key and old.get("p") is not None:
                    files[rel] = dict(old)
                    continue
                entry: dict = {"t": "f", "s": info.st_size, "mt": info.st_mtime_ns, "m": stat.S_IMODE(info.st_mode)}
                digest = hashlib.sha256()
                fd = open_verified(root, rel, info)  # type: ignore[arg-type]
                with os.fdopen(fd, "rb") as stream:
                    if info.st_size > LARGE:
                        parts, read = [], 0
                        while read < info.st_size:
                            block = stream.read(min(BLOCK, info.st_size - read))
                            if not block:
                                raise SnapshotAborted(f"{rel} changed while it was read")
                            digest.update(block)
                            parts.append(uploads.add(block, BLOCK) + [0, len(block)])
                            read += len(block)
                        # Pad the whole file to its Padmé length with random blocks: only that bucket shows.
                        padding, pads = crypto.padme(info.st_size) - BLOCK * len(parts), []
                        while padding > 0:
                            pads.append(uploads.add(random_bytes(min(BLOCK, padding)), BLOCK))
                            padding -= BLOCK
                        entry["p"], entry["pad"] = parts, pads
                    else:
                        content = stream.read(info.st_size + 1)
                        if len(content) != info.st_size:
                            raise SnapshotAborted(f"{rel} changed while it was read")
                        digest.update(content)
                        if content:
                            if pack_size + len(content) > PACK_LIMIT:
                                files[rel] = entry
                                flush_pack()
                            pack.append((rel, content))
                            pack_size += len(content)
                        else:
                            entry["p"] = []
                    if os.fstat(stream.fileno()).st_mtime_ns != info.st_mtime_ns:
                        raise SnapshotAborted(f"{rel} changed while it was read")
                entry["h"] = digest.hexdigest()
                files[rel] = entry
            flush_pack()
            if root is not None:
                os.close(root)
            for unit, members in units.items():
                if len(members) > 1:
                    for rel in members:
                        files[rel]["u"] = unit
            if not self.still_quiet(target.app):
                raise SnapshotAborted("the app opened while it was read")
            manifest = {"v": 2, "app": target.app, "owner": target.owner, "kind": target.kind, "files": files,
                        "dconf": {path: dconf_dump(path) for path in (dconf_paths(target.app) if target.kind == "flatpak" else [])},
                        "version_date": version_date, "device": self.device, "created": int(time.time())}
            manifest = self.store_manifest(zone, manifest, parent, uploads)
            manifest["_uploaded"] = uploads.send()
        finally:
            uploads.close()
        return manifest["_snapshot"], manifest

    def store_manifest(self, zone: str, manifest: dict, parent: str | None, uploads: Uploads | None = None) -> dict:
        """The manifest as padded pieces; its id is the hash of its canonical form."""
        clean = {key: value for key, value in manifest.items() if not key.startswith("_")}
        body = json.dumps(clean, sort_keys=True).encode()
        own = uploads is None
        uploads = uploads or Uploads(self.engine, zone, self.root / f".spool-{secrets.token_hex(4)}")
        try:
            pieces = [uploads.add(body[start:start + MANIFEST_PIECE], crypto.padme(len(body[start:start + MANIFEST_PIECE])))
                      for start in range(0, len(body), MANIFEST_PIECE)]
            if own:
                uploads.send()
        finally:
            if own:
                uploads.close()
        return {**clean, "_blobs": pieces, "_snapshot": hashlib.sha256(body).hexdigest()[:32], "_parent": parent}

    def publish(self, target: Target, snapshot: str, manifest: dict) -> bool:
        head = {"snapshot": snapshot, "parent": manifest.get("_parent"), "manifest": manifest["_blobs"], "device": self.device,
                "owner": target.owner, "device_name": self.device_name, "created": manifest["created"],
                "version_date": manifest.get("version_date", 0),
                "bytes": sum(e.get("s", 0) for e in manifest["files"].values() if e["t"] == "f")}
        ok, _current = self.engine.save(target.zone, "head", head)
        return ok

    def remote_manifest(self, target: Target, head: dict) -> dict:
        if head.get("owner") != target.owner:
            raise SnapshotAborted("the snapshot belongs to another publisher of this app")
        zone = self.engine.zone(target.zone, create=False)["zone"]  # type: ignore[index]
        pieces = head.get("manifest") or []
        if not all(isinstance(p, list) and len(p) == 2 and _part_ok(p + [0, 0]) for p in pieces) or len(pieces) > 64:
            raise SnapshotAborted("the snapshot's manifest list is malformed")
        body = b"".join(self.engine.get_blob(zone, blob) for blob, _generation in pieces)
        if hashlib.sha256(body).hexdigest()[:32] != head["snapshot"]:
            raise SnapshotAborted("the snapshot's manifest does not match its id")
        manifest = check_manifest(target, json.loads(body))
        manifest["_snapshot"] = head["snapshot"]
        return manifest

    # -- apply (download)
    def _assemble(self, zone: str, entry: dict, destination: Path, cache: dict[str, bytes]) -> None:
        """Write a file from its pieces, hashing while writing (review L2)."""
        digest = hashlib.sha256()
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            for blob, _generation, offset, length in entry["p"]:
                if blob not in cache:
                    if len(cache) > 8:
                        cache.clear()
                    cache[blob] = self.engine.get_blob(zone, blob)
                piece = cache[blob][offset:offset + length]
                if len(piece) != length:
                    raise SnapshotAborted("a piece is shorter than the manifest says")
                digest.update(piece)
                stream.write(piece)
        if digest.hexdigest() != entry["h"]:
            raise SnapshotAborted("a file did not match its manifest")

    def apply(self, target: Target, manifest: dict, *, local_keep: set[str] | None = None, progress=None) -> dict:
        """Build the folder described by manifest beside the current one and swap it in. Returns the manifest as applied."""
        zone = self.engine.zone(target.zone, create=False)["zone"]  # type: ignore[index]
        manifest = check_manifest(target, manifest)
        folder = target.folder
        if folder.is_symlink():
            raise SnapshotAborted("the app's data folder is a symlink")
        folder.parent.mkdir(parents=True, exist_ok=True)
        staging = folder.parent / f".{folder.name}.luma-sync-{secrets.token_hex(4)}"
        staging.mkdir(mode=0o700)
        current = {rel: info for rel, info in walk(folder, self.patterns)}
        root = open_folder(folder) if folder.is_dir() else None
        base = self.base_manifest(target) or {}
        known = {rel: e for rel, e in (base.get("files") or {}).items() if e["t"] == "f"}
        cache: dict[str, bytes] = {}
        total = sum(e.get("s", 0) for e in manifest["files"].values() if e["t"] == "f") or 1
        done = 0
        try:
            entries = sorted(manifest["files"].items())
            for rel, entry in entries:
                if entry["t"] == "d":
                    (staging / rel).mkdir(mode=0o700, parents=True, exist_ok=True)
            for rel, entry in entries:
                path = staging / rel
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                if path.parent.resolve() != staging.resolve() / Path(rel).parent:
                    raise SnapshotAborted("the manifest would write through a symlink")
                if entry["t"] == "l":
                    os.symlink(entry["to"], path)
                    continue
                if entry["t"] != "f":
                    continue
                here = current.get(rel)
                same = here is not None and stat.S_ISREG(here.st_mode) and (
                    (local_keep and rel in local_keep)
                    or (known.get(rel, {}).get("h") == entry["h"] and [known[rel]["s"], known[rel]["mt"], known[rel]["m"]] == _stat_key(here)))
                if same and root is not None:
                    # Copied from the verified descriptor, never linked by path through the live folder.
                    source = open_verified(root, rel, here)  # type: ignore[arg-type]
                    with os.fdopen(source, "rb") as reader, open(path, "xb") as writer:
                        shutil.copyfileobj(reader, writer, 1024 * 1024)
                else:
                    self._assemble(zone, entry, path, cache)
                os.chmod(path, int(entry["m"]) & 0o777)
                os.utime(path, ns=(entry["mt"], entry["mt"]))
                done += entry["s"]
                if progress:
                    progress(done / total)
            for rel, entry in entries:
                if entry["t"] == "d":
                    os.chmod(staging / rel, (int(entry["m"]) & 0o777) | 0o700)
            # What never syncs (caches, locks) stays as it was on this device.
            if folder.is_dir():
                for child in folder.iterdir():
                    if is_excluded(child.name, self.patterns) and not (staging / child.name).exists():
                        os.rename(child, staging / child.name)
            if not self.still_quiet(target.app):
                raise SnapshotAborted("the app opened during the update; it applies at the next quiet point")
            for path, dump in (manifest.get("dconf") or {}).items():
                dconf_load(path, dump)
            if folder.exists():
                old = folder.parent / f".{folder.name}.luma-sync-old-{secrets.token_hex(4)}"
                os.rename(folder, old)
                os.rename(staging, folder)
                shutil.rmtree(old, ignore_errors=True)
            else:
                os.rename(staging, folder)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        finally:
            if root is not None:
                os.close(root)
        applied = {key: value for key, value in manifest.items() if not key.startswith("_")}
        # The folder now holds exactly the manifest (mtimes included), so it becomes the base.
        self._keep_base(target, applied, manifest["_snapshot"])
        return applied

    # -- conflicts
    def keep_version(self, target: Target, rel: str, source: Path, who: str) -> Path:
        if not safe_relative(rel):
            raise SnapshotAborted("a kept version names a path outside the app's folder")
        stamp = time.strftime("%Y%m%d-%H%M")
        destination = self.versions_root / target.app / f"{stamp} {who}" / rel
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
        return destination

    def prune_versions(self) -> None:
        root = self.versions_root
        if not root.is_dir():
            return
        cutoff = time.time() - VERSION_DAYS * 86400
        for app in root.iterdir():
            for version in app.iterdir():
                if version.stat().st_mtime < cutoff:
                    shutil.rmtree(version, ignore_errors=True)

    def merge(self, target: Target, base: dict | None, local: dict, remote: dict, remote_name: str) -> tuple[dict, list[str]]:
        """File-by-file merge of two snapshots over their common base. Returns (merged manifest, kept versions)."""
        base_files = (base or {}).get("files") or {}
        lf, rf = local["files"], remote["files"]
        merged: dict[str, dict] = {}
        kept: list[str] = []

        def key(entry: dict | None):
            if entry is None:
                return None
            return (entry["t"], entry.get("h"), entry.get("to"), entry.get("m"))

        # SQLite units move together: decide per unit, then apply to every member.
        units: dict[str, set[str]] = {}
        for files in (base_files, lf, rf):
            for rel in files:
                units.setdefault(unit_of(rel), set()).add(rel)
        for unit, members in sorted(units.items()):
            l_changed = any(key(lf.get(rel)) != key(base_files.get(rel)) for rel in members)
            r_changed = any(key(rf.get(rel)) != key(base_files.get(rel)) for rel in members)
            if r_changed and not l_changed:
                side = "remote"
            elif l_changed and not r_changed:
                side = "local"
            elif not l_changed:
                side = "local"
            else:
                newest_l = max((lf[r].get("mt", 0) for r in members if r in lf), default=-1)
                newest_r = max((rf[r].get("mt", 0) for r in members if r in rf), default=-1)
                side = "local" if newest_l >= newest_r else "remote"
                if all(key(lf.get(r)) == key(rf.get(r)) for r in members):
                    side = "local"
                else:
                    loser = "remote" if side == "local" else "local"
                    for rel in members:
                        source = (rf if loser == "remote" else lf).get(rel)
                        if source and source["t"] == "f":
                            kept.append(f"{loser}:{rel}")
            chosen = lf if side == "local" else rf
            for rel in members:
                if rel in chosen:
                    merged[rel] = chosen[rel]
        result = dict(local)
        result["files"] = merged
        result["dconf"] = {**(remote.get("dconf") or {}), **(local.get("dconf") or {})}
        return result, kept

    # -- one app, start to finish
    def sync_app(self, target: Target, *, quiet: bool, version_date: int = 0, progress=None) -> str:
        """Upload, apply or merge one app's folder. ``quiet``: closed long enough to touch.

        Returns the app's state: off | local_only | over_budget | waiting | waiting_update | synced.
        """
        from . import portal_secrets
        app = target.app
        if not self.enabled(app):
            return self._state(app, "off")
        if not target.syncs:
            return self._state(app, "local_only")
        if self.engine.zone(target.zone, create=False) is not None:
            self.engine.fetch(target.zone)
        _version, head = self.engine.get(target.zone, "head")
        info = self.info(app)
        base_id = info.get("base") if info.get("owner") == target.owner else None
        remote_new = bool(head) and head["snapshot"] != base_id
        if not quiet:
            if remote_new:
                log.info("%s: newer state waits until the app closes", app)
            return self._state(app, "waiting" if remote_new else info.get("state", "synced"))
        if folder_bytes(target.folder, self.patterns) > BUDGET and not info.get("large_ok"):
            return self._state(app, "over_budget")
        if remote_new and version_date and head.get("version_date", 0) > version_date:
            return self._state(app, "waiting_update")
        base = self.base_manifest(target) if base_id else None
        local_new = self.local_changed(target, base) or (target.kind == "flatpak" and portal_secrets.changed(self.engine, self.local, target))
        if target.kind == "flatpak" and not remote_new:
            # A sign-in can change without the folder changing (the keyring file never syncs).
            portal_secrets.restore(self.engine, self.local, target)
        if not local_new and not remote_new:
            return self._state(app, "synced")
        if remote_new and not local_new:
            self._set_progress(app, "restoring", 0.0)
            remote = self.remote_manifest(target, head)
            self.apply(target, remote, progress=lambda value: self._set_progress(app, "restoring", value))
            log.info("%s: state from %s applied", app, head.get("device_name") or head.get("device"))
            if target.kind == "flatpak":
                portal_secrets.restore(self.engine, self.local, target)
            return self._state(app, "synced", from_device=head.get("device_name") or head.get("device"))
        for _attempt in range(3):
            if target.kind == "flatpak":
                portal_secrets.publish(self.engine, self.local, target)
            snapshot, manifest = self.snapshot(target, base_id, base, version_date)
            if not remote_new:
                if self.publish(target, snapshot, manifest):
                    self._keep_base(target, {k: v for k, v in manifest.items() if not k.startswith("_")}, snapshot)
                    return self._state(app, "synced")
                _version, head = self.engine.get(target.zone, "head")
                remote_new = True
            remote = self.remote_manifest(target, head)  # type: ignore[arg-type]
            merged, kept = self.merge(target, base, manifest, remote, head.get("device_name") or "another device")  # type: ignore[union-attr]
            zone = self.engine.zone(target.zone, create=False)["zone"]  # type: ignore[index]
            who = head.get("device_name") or head.get("device") or "another device"  # type: ignore[union-attr]
            stamp = time.strftime("%-d %b")
            for item in kept:
                side, rel = item.split(":", 1)
                # The losing copy stays in the zone as a labelled sibling every device can list and restore
                # (ADR-054 §5a: unmergeable conflicts keep both), and as a file here for 30 days.
                entry = (manifest if side == "local" else remote)["files"][rel]
                owner = (self.device_name or "this device") if side == "local" else who
                self.engine.save(target.zone, f"sibling:{rel}:{entry['h'][:16]}", {
                    "path": rel, "label": f"Version from {owner}, {stamp}", "device": self.device if side == "local" else head.get("device"),
                    "at": int(time.time()), "file": entry, "current": head["snapshot"]})  # type: ignore[index,union-attr]
                if side == "local":
                    self.keep_version(target, rel, target.folder / rel, self.device_name or "this device")
                else:
                    temporary = self.root / f".version-{secrets.token_hex(4)}"
                    try:
                        self._assemble(zone, remote["files"][rel], temporary, {})
                        self.keep_version(target, rel, temporary, who)
                    finally:
                        temporary.unlink(missing_ok=True)
            if target.kind == "flatpak":
                portal_secrets.restore(self.engine, self.local, target)
            merged = self.store_manifest(zone, merged, head["snapshot"])  # type: ignore[index]
            local_keep = {rel for rel, entry in merged["files"].items() if manifest["files"].get(rel) == entry}
            self.apply(target, merged, local_keep=local_keep)
            if self.publish(target, merged["_snapshot"], merged):
                if kept:
                    conflicts = self.local.get("conflicts", [])
                    conflicts.append({"app": app, "name": target.name or app, "from": who, "at": int(time.time()),
                                      "files": [item.split(":", 1)[1] for item in kept][:50]})
                    self.local.put("conflicts", conflicts[-50:])
                return self._state(app, "synced", conflict=bool(kept))
            base_id, base = merged["_snapshot"], self.base_manifest(target)
            _version, head = self.engine.get(target.zone, "head")
            remote_new = bool(head) and head["snapshot"] != base_id
            if not remote_new:
                return self._state(app, "synced")
        return self._state(app, "waiting")

    def _state(self, app: str, state: str, **extra) -> str:
        values = {"state": state, "progress": None, **extra}
        if state == "synced":
            values["last_synced"] = int(time.time())
        self.set_info(app, **values)
        return state

    def _set_progress(self, app: str, state: str, value: float) -> None:
        self.set_info(app, state=state, progress=round(value, 3))

    def siblings(self, target: Target) -> list[dict]:
        """Kept versions of files that changed on two devices, newest first."""
        return sorted((value for name, (_v, value, _w) in self.engine.all(target.zone).items() if name.startswith("sibling:")),
                      key=lambda value: -value["at"])

    def delete_synced(self, target: Target) -> None:
        """Delete synced data: the zone goes from the Hub; this device's folder stays."""
        self.engine.delete_zone(target.zone)
        self.set_info(target.app, base=None, state="off", enabled=False)
        self._manifest_path(target).unlink(missing_ok=True)


def targets(device: str = "") -> dict[str, Target]:
    from . import apps
    out: dict[str, Target] = {}
    installed = apps.installed_flatpaks()
    for app in installed:
        out[app] = Target(app, "flatpak", Path.home() / ".var/app" / app, app, flatpak_owner(app, installed, device))
    for record in apps.store_records():
        try:
            from luma_installer.desktop import read_record
            data_home = Path(str(read_record(str(record["id"])).get("data_home") or ""))
        except Exception:  # noqa: BLE001
            data_home = Path()
        if not str(data_home) or str(data_home) == ".":
            data_home = Path.home() / ".local/share/luma/apps-data" / str(record["id"])
        out[str(record["id"])] = Target(str(record["id"]), "store", data_home, str(record["name"]), store_owner(record, device))
    return out
