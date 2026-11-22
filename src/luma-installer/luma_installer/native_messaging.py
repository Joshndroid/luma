# SPDX-License-Identifier: Apache-2.0
"""Browser-to-application native messaging between sandboxes (ADR-052, Permissions 6).

A browser extension (a password manager's, say) talks to a program of its
application over stdin/stdout ("native messaging"). The browser normally
starts that program itself from a manifest; across sandboxes it cannot, and
must not: the program has to run in its own application's sandbox, with that
application's data, and the browser must never see that data, nor the
application the browser's.

The broker joins them without either reading the other:

* Hosts are registered by application ID: at install Luma reads the manifests
  an application's package ships (``etc/opt/chrome/native-messaging-hosts``,
  ``etc/chromium/...``, ``usr/lib/mozilla/native-messaging-hosts``, ...).
* A browser is registered by application ID too. Into its own configuration
  Luma writes, for every host, a manifest whose program is a small shim, and
  it gets one socket of its own,
  ``$XDG_RUNTIME_DIR/luma-native-messaging/BROWSER/broker.sock``.
* The browser starts the shim as it would start any host. The shim connects,
  says which host and which extension origin, and relays its stdin and stdout.
* The broker checks who is calling (the socket is the browser's alone, and the
  caller's process must be that browser: ``app_identity``), that the host
  allows that origin, and starts the host in its own sandbox with the
  connection as its stdin and stdout. Nothing passes through the broker after
  that, and nothing is shared but the messages.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

from . import app_identity

MANIFEST_FOLDERS = ("etc/opt/chrome/native-messaging-hosts", "etc/chromium/native-messaging-hosts",
                    "etc/opt/edge/native-messaging-hosts", "etc/opt/brave/native-messaging-hosts",
                    "usr/lib/mozilla/native-messaging-hosts", "usr/lib64/mozilla/native-messaging-hosts",
                    "usr/share/mozilla/native-messaging-hosts", "etc/viola/native-messaging-hosts")
HEADER_LIMIT = 4096

SHIM = r'''#!/usr/bin/env python3
# Luma native-messaging shim: relays this host's stdin and stdout to Luma's broker.
import json, os, socket, sys, threading
SOCKET = {socket_path!r}
HOST = {host!r}
origin = next((a for a in sys.argv[1:] if "://" in a or a.startswith("{{")), sys.argv[1] if len(sys.argv) > 1 else "")
connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.connect(SOCKET)
connection.sendall((json.dumps({{"host": HOST, "origin": origin}}) + "\n").encode())
def upstream():
    while True:
        data = os.read(0, 65536)
        if not data:
            break
        connection.sendall(data)
    try:
        connection.shutdown(socket.SHUT_WR)
    except OSError:
        pass
threading.Thread(target=upstream, daemon=True).start()
while True:
    data = connection.recv(65536)
    if not data:
        break
    os.write(1, data)
'''


def _data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def registry_path() -> Path:
    return _data_home() / "luma" / "native-messaging" / "registry.json"


def load() -> dict[str, dict[str, object]]:
    try:
        registry = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        registry = {}
    registry.setdefault("hosts", {})
    registry.setdefault("browsers", {})
    return registry


def save(registry: dict[str, dict[str, object]]) -> None:
    path = registry_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".partial")
    temporary.write_text(json.dumps(registry, indent=1, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def manifests_in_tree(root: Path) -> list[dict[str, object]]:
    """The native-messaging host manifests an application's package ships."""
    found: list[dict[str, object]] = []
    for folder in MANIFEST_FOLDERS:
        directory = root / folder
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, dict) or manifest.get("type", "stdio") != "stdio":
                continue
            name, program = str(manifest.get("name") or ""), str(manifest.get("path") or "")
            if not name or not program.startswith("/"):
                continue
            found.append({"name": name, "path": program, "description": str(manifest.get("description") or ""),
                          "allowed_origins": [str(item) for item in manifest.get("allowed_origins") or []],
                          "allowed_extensions": [str(item) for item in manifest.get("allowed_extensions") or []],
                          "family": "mozilla" if "mozilla" in folder else "chromium"})
    return found


def register_hosts(app: str, application_id: str, manifests: list[dict[str, object]]) -> list[str]:
    """Record an application's hosts; every registered browser learns them."""
    registry = load()
    hosts = dict(registry["hosts"])
    for name in [name for name, host in hosts.items() if host.get("app") == app]:
        hosts.pop(name)
    for manifest in manifests:
        hosts[str(manifest["name"])] = {**manifest, "app": app, "application_id": application_id}
    registry["hosts"] = hosts
    save(registry)
    for browser in registry["browsers"]:
        install_into_browser(browser, registry)
    return [str(manifest["name"]) for manifest in manifests]


def forget_app(app: str) -> None:
    registry = load()
    registry["hosts"] = {name: host for name, host in registry["hosts"].items() if host.get("app") != app}
    registry["browsers"].pop(app, None)
    save(registry)
    for browser in registry["browsers"]:
        install_into_browser(browser, registry)


def socket_path(browser: str) -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    return runtime / "luma-native-messaging" / browser / "broker.sock"


def browser_folders(browser: str, kind: str) -> tuple[list[Path], Path]:
    """(where its manifests go, where the shim goes), as paths valid inside its sandbox too."""
    if kind == "flatpak":
        base = Path.home() / ".var/app" / browser
        # Chromium keeps its hosts beside "Local State" in the profile folder it was
        # given (Viola: data/viola-luma/profile), or in config/PRODUCT by default.
        profiles = {path.parent for folder in (base / "config", base / "data") if folder.is_dir()
                    for path in [*folder.glob("*/Local State"), *folder.glob("*/*/Local State")]
                    if "scoped_dir" not in str(path)}
        profiles |= {base / "config" / name for name in ("viola", "chromium", "google-chrome")
                     if (base / "config" / name).is_dir()}
        chromium = [path / "NativeMessagingHosts" for path in sorted(profiles)]
        return [*chromium, base / ".mozilla/native-messaging-hosts"], base / "data/luma-native-messaging"
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "luma/apps-data" / app_identity.installed_id(browser)
    return [data / ".config/google-chrome/NativeMessagingHosts", data / ".config/chromium/NativeMessagingHosts",
            data / ".mozilla/native-messaging-hosts"], data / ".local/share/luma-native-messaging"


def register_browser(browser: str, kind: str = "flatpak") -> Path:
    registry = load()
    registry["browsers"][browser] = {"kind": kind}
    save(registry)
    install_into_browser(browser, registry)
    if kind == "flatpak":
        relative = f"xdg-run/luma-native-messaging/{browser}"
        subprocess.run(["flatpak", "override", "--user", f"--filesystem={relative}", browser],
                       capture_output=True, timeout=30)
    return socket_path(browser)


def install_into_browser(browser: str, registry: dict[str, dict[str, object]] | None = None) -> None:
    registry = registry or load()
    kind = str(dict(registry["browsers"].get(browser) or {}).get("kind") or "flatpak")
    manifests, shim_root = browser_folders(browser, kind)
    shim_root.mkdir(parents=True, exist_ok=True)
    inside_socket = socket_path(browser)
    for old in shim_root.glob("*.py"):
        old.unlink()
    for folder in manifests:
        if folder.is_dir():
            for old in folder.glob("*.json"):
                try:
                    if json.loads(old.read_text()).get("x-luma-broker"):
                        old.unlink()
                except (OSError, ValueError):
                    continue
    for name, host in registry["hosts"].items():
        if host.get("app") == browser:
            continue
        shim = shim_root / f"{name}.py"
        shim.write_text(SHIM.format(socket_path=str(inside_socket), host=name), encoding="utf-8")
        shim.chmod(0o755)
        for folder in manifests:
            mozilla = ".mozilla" in str(folder)
            if mozilla != (host.get("family") == "mozilla") and host.get("family") in {"mozilla", "chromium"}:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            manifest = {"name": name, "description": str(host.get("description") or name), "path": str(shim),
                        "type": "stdio", "x-luma-broker": True}
            if mozilla:
                manifest["allowed_extensions"] = host.get("allowed_extensions") or []
            else:
                manifest["allowed_origins"] = host.get("allowed_origins") or []
            (folder / f"{name}.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")


def allowed(host: dict[str, object], origin: str) -> bool:
    origins = [str(item) for item in host.get("allowed_origins") or []]
    extensions = [str(item) for item in host.get("allowed_extensions") or []]
    return bool(origin) and (origin in origins or origin.rstrip("/") + "/" in origins or origin in extensions)


def host_command(host: dict[str, object], origin: str) -> list[str]:
    """The host's program, in its own application's sandbox."""
    return [sys.executable, "-I", "-m", "luma_installer.closure_launcher", "--command", str(host["application_id"]),
            "--", str(host["path"]), origin]


class Broker:
    def __init__(self, model=None, *, launch=None, identify=app_identity.identify, peer=None) -> None:
        self.model, self.identify = model, identify
        self.peer = peer or (lambda connection: struct.unpack(
            "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[0])
        self.launch = launch or (lambda command, connection: subprocess.Popen(
            command, stdin=connection.fileno(), stdout=connection.fileno(), stderr=subprocess.DEVNULL,
            start_new_session=True))
        self.servers: dict[str, socket.socket] = {}

    def start(self) -> None:
        def watch() -> None:
            import time
            while True:
                for browser in load()["browsers"]:
                    try:
                        self.listen(browser)
                    except OSError as error:
                        print(f"luma-native-messaging: {browser}: {error}", file=sys.stderr)
                time.sleep(5)
        threading.Thread(target=watch, daemon=True, name="nm-watch").start()

    def listen(self, browser: str) -> None:
        if browser in self.servers:
            return
        path = socket_path(browser)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(8)
        self.servers[browser] = server
        threading.Thread(target=self._serve, args=(browser, server), daemon=True, name=f"nm-{browser}").start()

    def _serve(self, browser: str, server: socket.socket) -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self.handle, args=(browser, connection), daemon=True).start()

    def handle(self, browser: str, connection: socket.socket) -> bool:
        with connection:
            try:
                pid = self.peer(connection)
                caller, _kind = self.identify(pid)
                if caller != browser:
                    print(f"luma-native-messaging: {browser}'s socket used by {caller or 'pid ' + str(pid)}; refused",
                          file=sys.stderr)
                    return False
                connection.settimeout(5)
                header = b""
                while not header.endswith(b"\n"):
                    byte = connection.recv(1)
                    if not byte or len(header) > HEADER_LIMIT:
                        return False
                    header += byte
                connection.settimeout(None)
                request = json.loads(header)
            except (OSError, ValueError, struct.error):
                return False
            host = load()["hosts"].get(str(request.get("host") or ""))
            origin = str(request.get("origin") or "")
            if not host or host.get("app") == browser or not allowed(host, origin):
                print(f"luma-native-messaging: {browser} asked for {request.get('host')!r} from {origin!r}; refused",
                      file=sys.stderr)
                return False
            if self.model is not None:
                self.model.record_use(str(host["app"]), f"native-messaging:{browser}")
            process = self.launch(host_command(host, origin), connection)
        process.wait()
        return True


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="luma-native-messaging")
    commands = parser.add_subparsers(dest="command", required=True)
    browser = commands.add_parser("register-browser")
    browser.add_argument("app")
    browser.add_argument("--kind", default="flatpak", choices=("flatpak", "luma"))
    commands.add_parser("list")
    values = parser.parse_args(argv)
    if values.command == "register-browser":
        print(register_browser(values.app, values.kind))
    else:
        print(json.dumps(load(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
