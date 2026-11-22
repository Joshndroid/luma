# SPDX-License-Identifier: Apache-2.0
"""The person's folders as an application sees them (ADR-052, Permissions 1-3).

Every folder at the top of the home folder is at its real path in the
application's sandbox. A folder it may use is the real folder; one it may not
use is a refusal stub: an empty folder the application can see (``stat``) and
not read, enter or write. With SELinux the stub is a person-owned folder of
type ``luma_application.refused`` (``luma_application.cil``), and every refusal
is an AVC naming the folder, which ``refusal_watch`` turns into one offer.
Without SELinux it is a root-owned folder of mode 0000 shipped in the image:
still a permission error, only not reported. It is a mount over the real
folder, so nothing in the sandbox can look past it, and a nested user
namespace (a browser's sandbox) inherits it locked.

Hidden folders are never mounted: the application's private home has its own
(``~/.ssh``, ``~/.gnupg``, keyrings, browser profiles, other applications'
data and sign-in scripts never appear). ``PROTECTED`` repeats that list for
the formats Luma does not launch itself (Flatpak).

Grants change while the application runs. Each start gets its own view,
``$XDG_RUNTIME_DIR/luma-permissions/views/APP/INSTANCE/FOLDER``, a stub
mounted in a *keeper* mount namespace with shared propagation; the sandbox
binds the stubs, and a grant mounts the real folder on top of the stub in the
keeper, which the sandbox receives as a slave. Revoking unmounts it again.

* Store trees and AppImages (bubblewrap): the keeper is
  ``luma-app-views.service``, a user namespace that only holds mounts.
  bubblewrap runs inside it (``nsenter``) and waits (``--block-fd``) until
  its binds exist and the grants are on top, so the application never starts
  before its folders are right.
* Capsules (podman): the keeper is podman's own rootless namespace
  (``podman unshare``). The stubs are shared with ``rslave``; folders granted
  when it starts are bound directly, so revoking one of those needs a restart.

Without a keeper (no user namespaces) the view is static: stubs and real
folders are bound at start, and a change applies at the next start.
"""
from __future__ import annotations

import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

REFUSED_TYPE = "luma_application.refused"
CLOSED_STUB = Path("/usr/share/luma-installer/refused")
KEEPER_UNIT = "luma-app-views.service"

# Never granted to any application, in any format (ADR-052, Permissions 1).
# Relative to the home folder. Store trees and capsules never see any hidden
# entry of the real home; Flatpaks are narrowed by these (flatpak_permissions).
PROTECTED = (
    ".ssh", ".gnupg", ".pki", ".password-store", ".local/share/keyrings", ".local/share/kwalletd",
    ".mozilla", ".librewolf", ".waterfox", ".thunderbird", ".config/google-chrome", ".config/chromium",
    ".config/BraveSoftware", ".config/microsoft-edge", ".config/vivaldi", ".config/opera",
    ".config/viola", ".var/app/com.rhyme.viola",
    ".local/share/luma", ".local/share/containers", ".local/share/flatpak",
    ".config/autostart", ".config/systemd", ".config/environment.d", ".config/plasma-workspace",
    ".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zprofile", ".zshenv", ".zlogin",
    ".xprofile", ".xinitrc", ".xsession", ".config/fish",
)
# Files cannot be covered by an empty folder: these are listed for Flatpak as
# read-only, so a Flatpak with the home folder can read but never change them.
PROTECTED_FILES = frozenset({".bashrc", ".bash_profile", ".bash_login", ".profile", ".zshrc", ".zprofile",
                             ".zshenv", ".zlogin", ".xprofile", ".xinitrc", ".xsession"})

_MS_BIND = 4096
_MS_REC = 16384
_MS_SHARED = 1 << 20
_MNT_DETACH = 2


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


def home() -> Path:
    return Path.home().resolve()


def folders(root: Path | None = None) -> list[str]:
    """The visible folders at the top of the home folder."""
    root = root or home()
    found = []
    try:
        entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return found
    for entry in entries:
        if entry.name.startswith(".") or "/" in entry.name:
            continue
        try:
            if entry.is_dir(follow_symlinks=False):
                found.append(entry.name)
        except OSError:
            continue
    return found


def selinux_refusals() -> bool:
    """True where a refusal is reported: SELinux enforcing with the Luma policy loaded."""
    try:
        return Path("/sys/fs/selinux/enforce").read_text().strip() == "1"
    except OSError:
        return False


def stub(folder: str) -> Path:
    """The refusal stub for one folder name, made if needed."""
    if selinux_refusals():
        root = runtime_dir() / "luma-refused"
        path = root / folder
        try:
            root.mkdir(mode=0o755, exist_ok=True)
            path.mkdir(mode=0o755, exist_ok=True)
            for item in (root, path):
                label = f"unconfined_u:object_r:{REFUSED_TYPE}:s0".encode()
                if os.getxattr(item, "security.selinux").rstrip(b"\0") != label:
                    os.setxattr(item, "security.selinux", label)
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not any(path.iterdir()):
                return path
        except OSError:
            pass
    return CLOSED_STUB


def view_root(app: str, instance: str) -> Path:
    return runtime_dir() / "luma-permissions" / "views" / _component(app) / _component(instance)


def _component(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "._-" else "_" for character in value)
    return cleaned.strip(".") or "_"


# -- mounts (run inside a keeper) --------------------------------------------

def _libc():
    return ctypes.CDLL(None, use_errno=True)


def _check(result: int, what: str) -> None:
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, f"{what}: {os.strerror(error)}")


def bind(source: str, target: str) -> None:
    _check(_libc().mount(source.encode(), target.encode(), None, _MS_BIND, None), f"bind {target}")


def unmount(target: str) -> None:
    _check(_libc().umount2(target.encode(), _MNT_DETACH), f"unmount {target}")


def make_shared(target: str) -> None:
    _check(_libc().mount(None, target.encode(), None, _MS_SHARED, None), f"share {target}")


def mount_points() -> dict[str, int]:
    """Mount point -> how many mounts are stacked there, in this mount namespace."""
    counts: dict[str, int] = {}
    with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            fields = line.split()
            if len(fields) > 4:
                point = fields[4].encode().decode("unicode_escape")
                counts[point] = counts.get(point, 0) + 1
    return counts


def _same(path: str, other: str) -> bool:
    try:
        a, b = os.stat(path), os.stat(other)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def sync(entries: list[dict[str, object]], *, share_root: str = "") -> list[str]:
    """Make each view folder show what it should. Runs in the keeper.

    ``entries``: {"view": path, "stub": path, "real": path, "granted": bool}.
    The stub stays at the bottom; a grant is the real folder on top of it.
    Returns what changed.
    """
    changed: list[str] = []
    if share_root:
        if mount_points().get(share_root, 0) == 0:
            bind(share_root, share_root)
            make_shared(share_root)
    for entry in entries:
        view, stub_path, real = str(entry["view"]), str(entry["stub"]), str(entry["real"])
        granted = bool(entry.get("granted"))
        Path(view).mkdir(mode=0o700, parents=True, exist_ok=True)
        for _ in range(8):
            stacked = mount_points().get(view, 0)
            top_real = stacked > 0 and _same(view, real)
            if stacked == 0:
                bind(stub_path, view)
                changed.append(f"stub {view}")
            elif granted and not top_real:
                bind(real, view)
                changed.append(f"grant {view}")
            elif not granted and top_real:
                unmount(view)
                changed.append(f"revoke {view}")
            else:
                break
    return changed


def _enter(pid: int) -> None:
    """Join a keeper's user and mount namespaces (a fresh, single-threaded process only)."""
    for kind, flag in (("user", os.CLONE_NEWUSER), ("mnt", os.CLONE_NEWNS)):
        descriptor = os.open(f"/proc/{pid}/ns/{kind}", os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.setns(descriptor, flag)
        finally:
            os.close(descriptor)


# -- keepers -----------------------------------------------------------------

def keeper_pid(start: bool = True) -> int:
    """The bubblewrap keeper's process, started if needed; 0 when there is none."""
    if not shutil.which("systemctl") or not os.environ.get("XDG_RUNTIME_DIR"):
        return 0
    for attempt in range(2):
        try:
            shown = subprocess.run(["systemctl", "--user", "show", "-P", "MainPID", KEEPER_UNIT],
                                   capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return 0
        pid = int(shown.stdout.strip() or 0) if shown.stdout.strip().isdigit() else 0
        if pid and _is_keeper(pid):
            return pid
        if not start or attempt:
            return 0
        subprocess.run(["systemctl", "--user", "start", KEEPER_UNIT], capture_output=True, timeout=20)
    return 0


def _is_keeper(pid: int) -> bool:
    try:
        info = os.stat(f"/proc/{pid}")
        return info.st_uid == os.getuid() and os.readlink(f"/proc/{pid}/ns/mnt") != os.readlink("/proc/self/ns/mnt")
    except OSError:
        return False


def run_in_keeper(pid: int, entries: list[dict[str, object]]) -> list[str]:
    """Apply ``entries`` inside the bubblewrap keeper (a helper process joins it)."""
    return _helper(["--pid", str(pid)], entries)


def run_in_podman(entries: list[dict[str, object]], share_root: str) -> list[str]:
    """Apply ``entries`` inside podman's rootless namespace, where capsules start."""
    return _helper([], entries, prefix=["podman", "unshare"], share_root=share_root)


def _helper(arguments: list[str], entries: list[dict[str, object]], prefix: list[str] | None = None,
            share_root: str = "") -> list[str]:
    command = [*(prefix or []), sys.executable, "-I", "-m", "luma_installer.permission_views", "apply", *arguments]
    if share_root:
        command.extend(("--share", share_root))
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    try:
        done = subprocess.run(command, input=json.dumps(entries), capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OSError(f"the folder view could not be changed: {error}") from error
    if done.returncode:
        raise OSError(f"the folder view could not be changed: {done.stderr.strip()[-400:]}")
    return [line for line in done.stdout.splitlines() if line]


# -- one start -----------------------------------------------------------------

class View:
    """One start's folders: what the launcher binds, and how grants reach it."""

    def __init__(self, app: str, instance: str, granted: set[str], *, backend: str = "bwrap",
                 root: Path | None = None) -> None:
        self.app, self.instance, self.backend = app, instance, backend
        self.home = root or home()
        self.granted = set(granted)
        self.folders = folders(self.home)
        self.root = view_root(app, instance)
        self.keeper = 0

    def entries(self, granted: set[str] | None = None) -> list[dict[str, object]]:
        granted = self.granted if granted is None else granted
        return [{"view": str(self.root / name), "stub": str(stub(name)), "real": str(self.home / name),
                 "granted": name in granted} for name in self.folders]

    def prepare(self) -> bool:
        """Lay the stubs out in the keeper. False: no keeper, use ``static_binds``."""
        self._sweep()
        for name in self.folders:
            (self.root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        stubs_only = self.entries(set() if self.backend == "bwrap" else self.granted)
        try:
            if self.backend == "podman":
                run_in_podman(stubs_only, str(self.root))
                return True
            self.keeper = keeper_pid()
            if not self.keeper:
                return False
            run_in_keeper(self.keeper, stubs_only)
            return True
        except OSError:
            self.keeper = 0
            return False

    def _sweep(self) -> None:
        """Views of starts that ended without closing them (a killed launcher)."""
        parent = self.root.parent
        for state_path in sorted(parent.glob("*.json")) if parent.is_dir() else []:
            try:
                state = json.loads(state_path.read_text())
            except (OSError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            try:
                os.kill(int(state["pid"]), 0)
                continue
            except (OSError, ValueError, KeyError, TypeError):
                pass
            stale = View(self.app, state_path.stem, set(), backend=str(state.get("backend") or "bwrap"), root=self.home)
            stale.folders = [name for name in state.get("folders") or [] if isinstance(name, str)]
            stale.keeper = int(state.get("keeper") or 0)
            stale.close()

    def static_binds(self) -> list[tuple[str, str]]:
        return [(str(self.home / name) if name in self.granted else str(stub(name)), str(self.home / name))
                for name in self.folders]

    def binds(self) -> list[tuple[str, str]]:
        return [(str(self.root / name), str(self.home / name)) for name in self.folders]

    def gate_prefix(self) -> list[str]:
        """Put in front of the command (outside every other wrapper) when a keeper holds the view."""
        if not self.keeper:
            return []
        state = {"app": self.app, "instance": self.instance, "granted": sorted(self.granted), "home": str(self.home),
                 "folders": self.folders, "keeper": self.keeper}
        return [sys.executable, "-I", "-m", "luma_installer.permission_views", "gate", json.dumps(state), "--"]

    def nsenter(self) -> list[str]:
        return ["nsenter", f"--target={self.keeper}", "--user", "--mount", "--preserve-credentials"] if self.keeper else []

    def grant_now(self, sandbox_pid: int = 0, timeout: float = 5.0) -> None:
        """Put the grants on top once the sandbox has bound its stubs."""
        if sandbox_pid:
            wanted = {str(self.home / name) for name in self.folders}
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    with open(f"/proc/{sandbox_pid}/mountinfo", encoding="utf-8", errors="replace") as stream:
                        points = {line.split()[4] for line in stream if len(line.split()) > 4}
                except OSError:
                    break
                if wanted <= points:
                    break
                time.sleep(0.02)
        self.apply(self.granted)

    def apply(self, granted: set[str]) -> list[str]:
        self.granted = set(granted)
        entries = [entry for entry in self.entries() if Path(str(entry["view"])).is_dir()]
        if self.backend == "podman":
            return run_in_podman(entries, str(self.root))
        if self.keeper:
            return run_in_keeper(self.keeper, entries)
        return []

    def record(self) -> None:
        """What a running start is, for the permission service to find."""
        state = {"app": self.app, "instance": self.instance, "backend": self.backend, "keeper": self.keeper,
                 "pid": os.getpid(), "home": str(self.home), "folders": self.folders}
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root.parent / f"{_component(self.instance)}.json").write_text(json.dumps(state))

    def close(self) -> None:
        try:
            (self.root.parent / f"{_component(self.instance)}.json").unlink()
        except OSError:
            pass
        entries = [{**entry, "granted": False} for entry in self.entries()]
        try:
            if self.backend == "podman":
                _helper(["--release"], entries, prefix=["podman", "unshare"])
            elif self.keeper:
                _helper(["--pid", str(self.keeper), "--release"], entries)
        except OSError:
            pass
        shutil.rmtree(self.root, ignore_errors=True)
        try:
            self.root.parent.rmdir()
        except OSError:
            pass


# -- the gate: bubblewrap waits until the folders are right -----------------------

def gate(view: View, command: list[str]) -> int:
    """Run ``command`` (which contains ``bwrap``) so that the application starts only
    after its sandbox has bound the stubs and the grants are on top of them."""
    import signal
    if "bwrap" not in command:
        return subprocess.call(command)
    position = command.index("bwrap") + 1
    block_read, block_write = os.pipe()
    info_read, info_write = os.pipe()
    command = [*command[:position], "--block-fd", str(block_read), "--info-fd", str(info_write), *command[position:]]
    process = subprocess.Popen(command, pass_fds=(block_read, info_write))
    os.close(block_read)
    os.close(info_write)
    forward = lambda number, _frame: process.send_signal(number)  # noqa: E731
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, forward)
    child = 0
    with os.fdopen(info_read, "rb") as stream:
        try:
            text = b""
            while b"child-pid" not in text or b"}" not in text:
                chunk = stream.read1(4096) if hasattr(stream, "read1") else stream.read(4096)
                if not chunk:
                    break
                text += chunk
            child = int(json.loads(text.decode().split("}")[0] + "}").get("child-pid") or 0)
        except (ValueError, OSError):
            child = 0
    try:
        view.grant_now(child)
    except OSError as error:
        print(f"luma: folders not granted for this start: {error}", file=sys.stderr)
    try:
        os.write(block_write, b"1")
    except OSError:
        pass
    os.close(block_write)
    return process.wait()


def gate_main(arguments: list[str]) -> int:
    split = arguments.index("--")
    state = json.loads(arguments[split - 1])
    view = View(str(state["app"]), str(state["instance"]), set(state.get("granted") or []),
                root=Path(str(state["home"])))
    view.folders = [name for name in state.get("folders") or [] if isinstance(name, str)]
    view.keeper = int(state.get("keeper") or 0)
    return gate(view, arguments[split + 1:])


def running(app: str) -> list[dict[str, object]]:
    """The starts of ``app`` that are running now."""
    root = runtime_dir() / "luma-permissions" / "views" / _component(app)
    found = []
    for path in sorted(root.glob("*.json")) if root.is_dir() else []:
        try:
            state = json.loads(path.read_text())
            os.kill(int(state["pid"]), 0)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        found.append(state)
    return found


def apply_running(app: str, granted: set[str]) -> list[str]:
    """Make every running start of ``app`` match ``granted`` now."""
    changed: list[str] = []
    for state in running(app):
        view = View(app, str(state["instance"]), granted, backend=str(state.get("backend") or "bwrap"),
                    root=Path(str(state.get("home") or home())))
        view.folders = [name for name in state.get("folders") or [] if isinstance(name, str)]
        view.keeper = int(state.get("keeper") or 0)
        if view.backend == "bwrap" and not view.keeper:
            continue
        try:
            changed.extend(view.apply(granted))
        except OSError:
            continue
    return changed


def _release(entries: list[dict[str, object]]) -> None:
    for entry in entries:
        view = str(entry["view"])
        for _ in range(8):
            if mount_points().get(view, 0) == 0:
                break
            try:
                unmount(view)
            except OSError:
                break


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["gate"]:
        return gate_main(arguments[1:])
    if not arguments or arguments[0] != "apply":
        print("usage: permission_views apply [--pid PID] [--share PATH] [--release] < entries.json", file=sys.stderr)
        return 2
    pid = int(arguments[arguments.index("--pid") + 1]) if "--pid" in arguments else 0
    share = arguments[arguments.index("--share") + 1] if "--share" in arguments else ""
    entries = json.loads(sys.stdin.read() or "[]")
    if pid:
        _enter(pid)
    if "--release" in arguments:
        _release(entries)
        return 0
    for line in sync(entries, share_root=share):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
