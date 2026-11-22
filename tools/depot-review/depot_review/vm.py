# SPDX-License-Identifier: Apache-2.0
"""Install, launch, screenshot and uninstall an app in a throwaway Luma VM.

A base disk is installed once from published Luma installation media (the
way Try Luma provisions its VM), with one person account that logs in
automatically and root SSH for this host only. Each run boots a copy-on-write
overlay of that disk, so nothing an app does survives the run.

  python3 -m depot_review.vm build-base --iso ISO --out DIR [--arch x86_64]
  python3 -m depot_review.vm run --base DISK --key KEY --package FILE --app-id ID --out DIR
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

USER = "luma-review"
WIDTH, HEIGHT = 1920, 1080


def log(message: str) -> None:
    print(time.strftime("%H:%M:%S"), message, file=sys.stderr, flush=True)


# The review desktop never idles, dims, locks or blanks: a screen that goes off
# mid-check can't be watched. Locked, so a session setting can't undo it.
REVIEW_DCONF = """[org/gnome/desktop/session]
idle-delay=uint32 0
[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false
[org/gnome/desktop/lockdown]
disable-lock-screen=true
[org/gnome/desktop/notifications]
show-banners=false
[org/gnome/settings-daemon/plugins/power]
idle-dim=false
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
"""
REVIEW_DCONF_LOCKS = """/org/gnome/desktop/session/idle-delay
/org/gnome/desktop/screensaver/lock-enabled
/org/gnome/desktop/screensaver/idle-activation-enabled
/org/gnome/settings-daemon/plugins/power/idle-dim
/org/gnome/settings-daemon/plugins/power/sleep-inactive-ac-type
"""


def write_review_dconf() -> str:
    """Shell that installs the review dconf policy (kickstart %post and prepare-base)."""
    return ("mkdir -p /etc/dconf/db/local.d/locks\n"
            f"cat > /etc/dconf/db/local.d/90-depot-review <<'EOF'\n{REVIEW_DCONF}EOF\n"
            f"cat > /etc/dconf/db/local.d/locks/90-depot-review <<'EOF'\n{REVIEW_DCONF_LOCKS}EOF\n"
            "dconf update || true\n")


def kickstart(public_key: str, console: str) -> str:
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,3}( \S+)?", public_key):
        raise ValueError("invalid review SSH key")
    return f'''text
lang en_US.UTF-8
keyboard us
timezone UTC --utc
network --bootproto=dhcp --device=link --activate --hostname=depot-review
ignoredisk --only-use=vda
zerombr
clearpart --all --initlabel --disklabel=gpt --drives=vda
autopart --type=btrfs --noswap
bootloader --timeout=1 --append="rhgb quiet consoleblank=0 systemd.default_device_timeout_sec=600 console=tty0 console={console}"
firstboot --disable
user --name={USER} --gecos="Depot Review" --lock
rootpw --lock
sshkey --username=root "{public_key}"
services --enabled=sshd
firewall --enabled --service=ssh
poweroff
%include /usr/share/anaconda/interactive-defaults.ks
%post --erroronfail
set -e
mkdir -p /etc/gdm /var/lib/AccountsService/users /etc/dconf/db/local.d
printf '[daemon]\\nAutomaticLoginEnable=True\\nAutomaticLogin={USER}\\n' > /etc/gdm/custom.conf
printf '[User]\\nSystemAccount=false\\n' > /var/lib/AccountsService/users/{USER}
install -d -o {USER} -g {USER} /home/{USER}/.config /home/{USER}/.local /home/{USER}/.local/state /home/{USER}/.local/state/luma-setup
printf 'yes\\n' > /home/{USER}/.config/gnome-initial-setup-done
: > /home/{USER}/.local/state/luma-setup/welcome-done
chown {USER}:{USER} /home/{USER}/.config/gnome-initial-setup-done /home/{USER}/.local/state/luma-setup/welcome-done
{write_review_dconf()}systemctl mask {" ".join(ANDROID_SYSTEM_UNITS)}
systemctl --global mask {" ".join(REVIEW_USER_MASKS)}
systemctl set-default graphical.target
restorecon -RF /etc/gdm /var/lib/AccountsService/users /home/{USER} /etc/dconf || true
%end
%post --nochroot
printf '\\nDEPOT_REVIEW_BASE_COMPLETE\\n' > /dev/{console}
%end
'''


def qemu_base(arch: str) -> list[str]:
    if arch == "x86_64":
        accel = ["-enable-kvm", "-cpu", "host"] if os.access("/dev/kvm", os.W_OK) else ["-cpu", "max"]
        return ["qemu-system-x86_64", "-machine", "q35", *accel]
    if arch == "aarch64":
        firmware = "/usr/share/edk2/aarch64/QEMU_EFI-silent-pflash.raw"
        return ["qemu-system-aarch64", "-machine", "virt", "-cpu", "max", "-bios",
                "/usr/share/edk2/aarch64/QEMU_EFI.fd" if not os.path.exists(firmware) else firmware]
    raise ValueError(arch)


def build_base(iso: Path, out: Path, arch: str = "x86_64", disk_gib: int = 40) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    key = out / "review-key"
    if not key.exists():
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "depot-review", "-f", str(key)],
                       check=True)
    public = key.with_suffix(".pub").read_text().strip()
    console = "ttyS0" if arch == "x86_64" else "ttyAMA0"
    work = Path(tempfile.mkdtemp(prefix="base-", dir=out))
    label = subprocess.run(["isoinfo", "-d", "-i", str(iso)], capture_output=True, text=True).stdout
    match = re.search(r"Volume id:\s*(\S+)", label)
    if not match:
        raise RuntimeError("cannot read the ISO volume label")
    label = match.group(1)
    for name in ("vmlinuz", "initrd.img"):
        with (work / name).open("wb") as target:
            subprocess.run(["bsdtar", "-xOf", str(iso), f"images/pxeboot/{name}"], stdout=target, check=True)
    (work / "ks.cfg").write_text(kickstart(public, console))
    subprocess.run(["xorriso", "-as", "mkisofs", "-quiet", "-o", str(work / "seed.iso"), "-V", "DEPOT_REVIEW_KS",
                    "-J", "-r", str(work / "ks.cfg")], check=True)
    disk = work / "base.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", str(disk), f"{disk_gib}G"], check=True)
    serial = work / "install-serial.log"
    command = qemu_base(arch) + [
        "-m", "6144", "-smp", "4", "-nographic", "-no-reboot",
        "-drive", f"file={disk},if=virtio,format=qcow2,cache=unsafe,discard=unmap",
        "-drive", f"file={iso},media=cdrom,readonly=on",
        "-drive", f"file={work / 'seed.iso'},media=cdrom,readonly=on",
        "-kernel", str(work / "vmlinuz"), "-initrd", str(work / "initrd.img"),
        "-append", f"inst.ks=hd:LABEL=DEPOT_REVIEW_KS:/ks.cfg inst.stage2=hd:LABEL={label} inst.text "
                   f"inst.notmux console={console}",
        "-nic", "user,model=virtio-net-pci", "-serial", f"file:{serial}", "-monitor", "none",
        "-object", "rng-random,filename=/dev/urandom,id=rng0", "-device", "virtio-rng-pci,rng=rng0",
    ]
    log(f"installing {iso.name} into {disk}")
    subprocess.run(command, check=True, timeout=5400, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    if "DEPOT_REVIEW_BASE_COMPLETE" not in serial.read_text(errors="replace"):
        raise RuntimeError(f"install did not complete; see {serial}")
    final = out / f"base-{arch}.qcow2"
    disk.replace(final)
    (out / f"base-{arch}.json").write_text(json.dumps({"iso": str(iso), "label": label,
                                                       "built": time.strftime("%FT%TZ", time.gmtime())}) + "\n")
    shutil.rmtree(work)
    prepare_base(final, arch)
    log(f"base ready: {final}")
    return final


# Jobs that only matter on a real computer and hold the desktop back in a
# throwaway VM: update checks, caches, Snap seeding, network-online waits.
REVIEW_MASKS = ("luma-update-check.service", "luma-update-check.timer",
                "fedora-atomic-desktop-appstream-cache-refresh.service",
                "fedora-atomic-desktop-mandb-update.service", "snapd.seeded.service", "snapd.service",
                "snapd.socket", "NetworkManager-wait-online.service",
                "luma-android-initialize.service", "waydroid-container.service")
# Android never runs in the review VM: Flatpak checks don't need it, and once
# luma-android-initialize has set it up (any boot without /var/lib/waydroid/
# waydroid.cfg, prepare-base's own boots included) the session's prewarm starts
# it in every run, taking a quarter of the VM's memory and a core of dex2oat.
REVIEW_USER_MASKS = ("luma-android-prewarm.timer", "luma-android-prewarm.service", "luma-android-session.service")
ANDROID_SYSTEM_UNITS = ("luma-android-initialize.service", "waydroid-container.service")


def android_off(runtime: bool = True) -> str:
    """Shell that stops Android and keeps it from starting (for this boot only
    with runtime=True, which also covers bases prepared before the masks)."""
    scope = "--runtime " if runtime else ""
    return (f"systemctl mask {scope}--now {' '.join(ANDROID_SYSTEM_UNITS)} >/dev/null 2>&1; "
            f"systemctl --global mask {scope}{' '.join(REVIEW_USER_MASKS)} >/dev/null 2>&1; true")


def qmp_path(work: Path) -> Path:
    """The run's QMP socket. AF_UNIX paths stop at 108 bytes, and job folders
    can be deeper than that; then the socket lives in the temp folder."""
    path = work / "qmp.sock"
    if len(os.fsencode(path)) < 100:
        return path
    return Path(tempfile.gettempdir()) / f"depot-qmp-{zlib.crc32(os.fsencode(work)):08x}-{os.getpid()}.sock"


def boot(disk: Path, arch: str, work: Path, port: int, memory: str = "4096", cpus: str = "2"):
    display = (["-device", f"virtio-vga,xres={WIDTH},yres={HEIGHT}"] if arch == "x86_64"
               else ["-device", f"virtio-gpu-pci,xres={WIDTH},yres={HEIGHT}"])
    command = qemu_base(arch) + [
        "-m", memory, "-smp", cpus, "-display", "none", *display,
        "-drive", f"file={disk},if=virtio,format=qcow2,cache=unsafe,discard=unmap",
        "-nic", f"user,model=virtio-net-pci,hostfwd=tcp:127.0.0.1:{port}-:22",
        "-qmp", f"unix:{qmp_path(work)},server=on,wait=off", "-serial", f"file:{work / 'serial.log'}",
        "-monitor", "none",
        "-object", "rng-random,filename=/dev/urandom,id=rng0", "-device", "virtio-rng-pci,rng=rng0",
    ]
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=open(work / "qemu.log", "w"))


EMERGENCY = "You are in emergency mode"


def in_emergency(serial: Path) -> bool:
    """The guest gave up booting (on a loaded host, usually device units that
    timed out); it never comes back from there."""
    try:
        return EMERGENCY in serial.read_text(errors="replace")
    except OSError:
        return False


def wait_ssh(guest: "Guest", process, timeout: int, serial: Path | None = None) -> bool:
    started = time.time()
    while time.time() - started < timeout:
        if process.poll() is not None:
            return False
        if serial is not None and in_emergency(serial):
            return False
        if guest.run("true", timeout=30).returncode == 0:
            return True
        time.sleep(5)
    return False


def wait_session(guest: "Guest", timeout: int) -> str:
    started = time.time()
    while time.time() - started < timeout:
        probe = guest.run(f"id -u {USER} && pgrep -u {USER} -x gnome-shell >/dev/null && "
                          f"test -S /run/user/$(id -u {USER})/wayland-0 && "
                          f"loginctl show-user {USER} -p State --value", timeout=20)
        lines = probe.stdout.split()
        if probe.returncode == 0 and len(lines) >= 2 and lines[-1] == "active":
            return lines[0]
        time.sleep(5)
    return ""


PREPARED_STAMP = "/etc/luma-depot-review/prepared"


def prepare_refusal(stamped: bool, holders: str, force: bool) -> str:
    """Why prepare-base must not touch this base, or "". Its boots run first-boot
    jobs inside the base (Android's setup was one), so a prepared base is never
    prepared again: build a new one. A base other disks back onto can't change."""
    if holders:
        return f"the base is open by other processes ({holders}); disks backed by it would break"
    if stamped and not force:
        return "the base is already prepared; re-preparing runs first-boot jobs inside it. Build a new base"
    return ""


def disk_holders(disk: Path) -> str:
    result = subprocess.run(["fuser", str(disk)], capture_output=True, text=True)
    return result.stdout.strip()


def prepare_base(disk: Path, arch: str = "x86_64", force: bool = False) -> None:
    """Make an installed base boot straight to a stable logged-in desktop.

    Works on an overlay and commits it into the base only after the verify boot
    passed, so a refusal or a failure leaves the base as it was."""
    key = disk.parent / "review-key"
    why = prepare_refusal(False, disk_holders(disk), force)
    if why:
        raise RuntimeError(f"prepare-base: {why}")
    work = Path(tempfile.mkdtemp(prefix="prepare-", dir=disk.parent))
    overlay = work / "prepare.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(disk.resolve()), "-F", "qcow2",
                    str(overlay)], check=True)
    try:
        for attempt in ("configure", "verify"):
            port = free_port()
            process = boot(overlay, arch, work, port, memory="6144", cpus="4")
            guest = Guest(port, key)
            started = time.time()
            try:
                if not wait_ssh(guest, process, 1500):
                    raise RuntimeError(f"{attempt}: no SSH after {int(time.time() - started)} s")
                log(f"{attempt}: SSH after {int(time.time() - started)} s")
                if attempt == "configure":
                    stamped = guest.run(f"test -e {PREPARED_STAMP}", timeout=30).returncode == 0
                    why = prepare_refusal(stamped, "", force)
                    if why:
                        raise RuntimeError(f"prepare-base: {why}")
                    guest.run("systemctl mask " + " ".join(REVIEW_MASKS), timeout=60, check=True)
                    guest.run("systemctl --global mask " + " ".join(REVIEW_USER_MASKS), timeout=60, check=True)
                    guest.run(write_review_dconf(), timeout=60, check=True)
                    guest.run("rpm-ostree kargs --append-if-missing=plymouth.enable=0 "
                              "--append-if-missing=consoleblank=0 "
                              "--append-if-missing=systemd.default_device_timeout_sec=600 "
                              "--delete-if-present=rhgb --delete-if-present=quiet", timeout=600, check=True)
                    guest.run(f"mkdir -p {Path(PREPARED_STAMP).parent} && date -u +%FT%TZ > {PREPARED_STAMP}",
                              timeout=30, check=True)
                    jobs = guest.run("systemctl list-jobs --no-legend | head -20", timeout=30).stdout
                    log("pending jobs at configure:\n" + jobs)
                else:
                    uid = wait_session(guest, 900)
                    if not uid:
                        jobs = guest.run("systemctl list-jobs --no-legend | head; loginctl list-sessions", timeout=30)
                        raise RuntimeError("verify: no logged-in desktop:\n" + jobs.stdout)
                    log(f"verify: desktop session after {int(time.time() - started)} s")
                    time.sleep(45)
                    again = wait_session(guest, 10)
                    if again != uid:
                        raise RuntimeError("verify: the desktop session did not stay logged in")
                    qmp = QMP(qmp_path(work))
                    qmp.command("screendump", filename=str(work / "desktop.ppm"))
                    width, height, rgb = read_ppm(work / "desktop.ppm")
                    write_png(disk.parent / f"base-{arch}-desktop.png", width, height, rgb)
                    log(f"verify: desktop stayed up; boot to desktop took {int(time.time() - started)} s")
                guest.run("systemctl poweroff", timeout=30)
                process.wait(timeout=300)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                shutil.copy(work / "serial.log", disk.parent / f"prepare-{attempt}-serial.log")
                qmp_path(work).unlink(missing_ok=True)
        why = prepare_refusal(False, disk_holders(disk), force)
        if why:
            raise RuntimeError(f"prepare-base: {why}")
        subprocess.run(["qemu-img", "commit", "-q", str(overlay)], check=True)
        log(f"prepare-base: committed into {disk}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# -- QMP and screenshots -----------------------------------------------------------

class QMP:
    def __init__(self, path: Path, timeout: float = 30):
        deadline = time.time() + timeout
        while True:
            try:
                self.sock = socket.socket(socket.AF_UNIX)
                self.sock.connect(str(path))
                break
            except OSError:
                if time.time() > deadline:
                    raise
                time.sleep(0.5)
        self.file = self.sock.makefile("rw")
        json.loads(self.file.readline())
        self.command("qmp_capabilities")

    def command(self, name: str, **arguments):
        self.file.write(json.dumps({"execute": name, "arguments": arguments}) + "\n")
        self.file.flush()
        while True:
            reply = json.loads(self.file.readline())
            if "return" in reply:
                return reply["return"]
            if "error" in reply:
                raise RuntimeError(f"QMP {name}: {reply['error']}")


def read_ppm(path: Path) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    parts = data.split(maxsplit=4)
    if parts[0] != b"P6":
        raise ValueError("unexpected screendump format")
    width, height = int(parts[1]), int(parts[2])
    pixels = parts[4]
    return width, height, pixels[: width * height * 3]


def write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    stride = width * 3
    raw = b"".join(b"\0" + rgb[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def changed_fraction(a: bytes, b: bytes, width: int = 0, height: int = 0) -> float:
    """The share of sampled pixels that differ. With the frame size, the shell's
    own bands (top bar and dock) are left out, so only the area where an app's
    window lands counts."""
    if len(a) != len(b) or not a:
        return 1.0
    top, bottom = 0, len(a)
    if width and height and len(a) == width * height * 3:
        stride = width * 3
        top, bottom = int(height * APP_AREA[0]) * stride, int(height * APP_AREA[1]) * stride
    step = 3 * 7  # sample every 7th pixel
    total = changed = 0
    for i in range(top, bottom - 2, step):
        total += 1
        if abs(a[i] - b[i]) + abs(a[i + 1] - b[i + 1]) + abs(a[i + 2] - b[i + 2]) > 24:
            changed += 1
    return changed / max(total, 1)


# The rows between the top bar and the dock, as fractions of the frame height.
APP_AREA = (0.06, 0.85)
WINDOW_FRACTION = 0.02


def is_blank(rgb: bytes) -> bool:
    """A frame that is almost all one dark colour: the display is off."""
    sample = rgb[::3 * 97]
    return bool(sample) and sum(1 for v in sample if v > 40) < len(sample) * 0.01


def window_verdict(shown: float, after_stop: float) -> bool:
    """Whether a change on screen was the app's window: it was there while the
    app ran, and it went away when the app was stopped. A change that stays (the
    desktop's own windows, notifications, wallpaper) was not the app."""
    return shown > WINDOW_FRACTION and after_stop < max(WINDOW_FRACTION / 2, shown * 0.25)


# -- A run ---------------------------------------------------------------------------

class Guest:
    def __init__(self, port: int, key: Path):
        self.port, self.key = port, key

    def ssh_args(self) -> list[str]:
        return ["-i", str(self.key), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "IdentitiesOnly=yes",
                "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null", "-o", "LogLevel=ERROR"]

    def run(self, command: str, timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(["ssh", "-p", str(self.port), *self.ssh_args(), "root@127.0.0.1", command],
                                    capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as expired:
            # While the guest boots, user networking accepts the connection
            # before sshd answers; that is "not yet", not an error.
            result = subprocess.CompletedProcess(expired.cmd, 124, expired.stdout or "", "timed out")
            if isinstance(result.stdout, bytes):
                result.stdout = result.stdout.decode(errors="replace")
        if check and result.returncode != 0:
            raise RuntimeError(f"guest command failed ({result.returncode}): {command}\n{result.stderr[-800:]}")
        return result

    def copy(self, local: Path, remote: str, timeout: int = 600) -> None:
        subprocess.run(["scp", "-q", "-P", str(self.port), *self.ssh_args(), str(local), f"root@127.0.0.1:{remote}"],
                       check=True, timeout=timeout, stdin=subprocess.DEVNULL)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def session_env(uid: str) -> str:
    return (f"XDG_RUNTIME_DIR=/run/user/{uid} DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus "
            f"WAYLAND_DISPLAY=wayland-0")


def keep_awake(guest: "Guest", uid: str) -> None:
    """Keep the session's screen on for the whole run: no idle, dim, lock or
    DPMS-off, and an idle inhibitor held until the VM stops."""
    env = session_env(uid)
    guest.run(f"runuser -u {USER} -- env {env} sh -c '"
              "gsettings set org.gnome.desktop.session idle-delay 0; "
              "gsettings set org.gnome.desktop.screensaver lock-enabled false; "
              "gsettings set org.gnome.desktop.screensaver idle-activation-enabled false; "
              "gsettings set org.gnome.settings-daemon.plugins.power idle-dim false; "
              "gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing' 2>/dev/null; "
              f"runuser -u {USER} -- env {env} systemd-run --user --quiet --collect --unit=depot-review-awake "
              "gnome-session-inhibit --inhibit idle --inhibit-only --reason 'Depot review' >/dev/null 2>&1; "
              "loginctl unlock-sessions 2>/dev/null; true", timeout=60)
    wake(guest, uid)


def wake(guest: "Guest", uid: str) -> None:
    """Turn the display back on if it went into power saving."""
    guest.run(f"runuser -u {USER} -- env {session_env(uid)} gdbus call --session --dest org.gnome.Mutter.DisplayConfig "
              "--object-path /org/gnome/Mutter/DisplayConfig --method org.freedesktop.DBus.Properties.Set "
              "org.gnome.Mutter.DisplayConfig PowerSaveMode '<int32 0>' >/dev/null 2>&1; true", timeout=30)


SCREEN_TRIES = 4
LAUNCH_SECONDS = 180
UNIT_STATES = ("active", "activating", "deactivating", "inactive", "failed")


def parse_unit_state(returncode: int, stdout: str) -> str:
    """systemctl is-active's answer, or "unknown" when SSH or the user manager
    didn't answer (a probe timeout is not the app closing)."""
    lines = stdout.strip().splitlines()
    state = lines[-1].strip() if lines else ""
    if returncode in (124, 255) or state not in UNIT_STATES:
        return "unknown"
    return state


def stuck_in_ldconfig(comms: str) -> bool:
    """The app's unit is still only Flatpak building its library cache: flatpak,
    bwrap and ldconfig, and nothing of the app itself."""
    names = set(comms.split())
    return "ldconfig" in names and names <= {"flatpak", "bwrap", "ldconfig"}


def judge_launch(control: bool | None) -> str:
    """Who a failed launch is about. A known-good control app that also fails on
    the same VM means the VM couldn't run apps: "infrastructure". Otherwise (it
    opened a window, or there was no control to run) the finding stands: "app"."""
    return "infrastructure" if control is False else "app"


def run_app(base: Path, key: Path, package: Path | None, app_id: str, fmt: str, out: Path, arch: str = "x86_64",
            boot_timeout: int = 420, remote_ref: str | None = None, memory: str = "6144", cpus: str = "4",
            control_bundle: Path | None = None, control_app_id: str = "") -> dict:
    """With remote_ref (e.g. luma app/ID/ARCH/stable), install from the VM's own
    configured remote instead of a bundle: the published-release check.

    Returns {"steps": [{step, ok, message}], "screenshot": path|None, "ok": bool}.
    A step Depot itself couldn't run carries infrastructure=True and a reason;
    it says nothing about the app. The boot timeout counts from qemu's start, so
    take host capacity (capacity.slot) before calling this.

    When the app doesn't launch, control_bundle (a known-good app on the same
    runtime) is launched on the same VM. If it fails too, the launch step is
    infrastructure (control_failed), not a finding against the app."""
    out.mkdir(parents=True, exist_ok=True)
    steps: list[dict] = []
    result = {"steps": steps, "screenshot": None, "ok": False, "arch": arch}

    def step(name: str, ok: bool, message: str, **extra) -> bool:
        steps.append({"step": name, "ok": ok, "message": message, **extra})
        log(f"{name}: {'ok' if ok else 'FAIL'} {message}")
        return ok

    def couldnt_run(name: str, reason: str, message: str, detail: str = "") -> None:
        step(name, False, message, internal=True, infrastructure=True, reason=reason, detail=detail)

    if fmt != "flatpak":
        step("install", False, "The VM run covers Flatpak bundles tonight; Luma checks universal packages by hand.",
             internal=True)
        return result
    work = Path(tempfile.mkdtemp(prefix="run-", dir=out))
    overlay = work / "disk.qcow2"
    subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(overlay)],
                   check=True)
    port = free_port()
    started = time.time()
    process = boot(overlay, arch, work, port, memory=memory, cpus=cpus)
    guest = Guest(port, key)
    try:
        qmp = QMP(qmp_path(work))
        if not wait_ssh(guest, process, boot_timeout, work / "serial.log"):
            if in_emergency(work / "serial.log"):
                couldnt_run("boot", "guest_emergency", "The test VM didn't start in time.",
                            detail=f"guest in emergency mode after {int(time.time() - started)} s")
            elif process.poll() is not None:
                couldnt_run("boot", "vm_exited", "The test VM stopped while it was starting.",
                            detail=f"qemu exited with {process.returncode}")
            else:
                couldnt_run("boot", "boot_timeout", "The test VM didn't start in time.",
                            detail=f"no SSH after {int(time.time() - started)} s")
            return result
        # The policy and the Android masks are in the base; this also covers
        # bases prepared before they were.
        guest.run(write_review_dconf() + android_off(), timeout=90)
        uid = wait_session(guest, max(60, boot_timeout - int(time.time() - started)))
        if not uid:
            couldnt_run("boot", "desktop_timeout", "The test VM's desktop didn't start.",
                        detail=f"no desktop session after {int(time.time() - started)} s")
            return result
        step("boot", True, f"Luma started ({int(time.time() - started)} s).")
        # Android starts in the background after sign-in. Its window and memory
        # are not the app's, so it stays off while an app is checked.
        guest.run(f"runuser -u {USER} -- env {session_env(uid)} systemctl --user mask --runtime --now "
                  "luma-android-prewarm.service >/dev/null 2>&1; "
                  f"runuser -u {USER} -- env {session_env(uid)} waydroid session stop >/dev/null 2>&1; true",
                  timeout=60)
        # A slow boot can outlast the idle delay: the screen blanks, and a blank
        # screen waking up looks like a window. Keep it on for the whole run and
        # wake it; a busy host can also take a while to draw the first frame.
        keep_awake(guest, uid)
        before = work / "before.ppm"
        for attempt in range(SCREEN_TRIES):
            qmp.command("send-key", keys=[{"type": "qcode", "data": "shift"}])
            time.sleep(8 if attempt == 0 else 10)  # let the shell settle
            qmp.command("screendump", filename=str(before))
            if not is_blank(read_ppm(before)[2]):
                break
            log(f"screen: blank after wake {attempt + 1}/{SCREEN_TRIES}")
            wake(guest, uid)
        else:
            couldnt_run("launch", "screen_off", "The test VM's screen was off, so the launch couldn't be watched.",
                        detail=f"blank after {SCREEN_TRIES} wakes")
            return result
        if remote_ref:
            install = guest.run(f"flatpak install --system --noninteractive -y {remote_ref} 2>&1", timeout=1200)
        else:
            guest.copy(package, "/var/tmp/app.flatpak")
            install = guest.run("flatpak install --system --noninteractive -y /var/tmp/app.flatpak 2>&1",
                                timeout=1200)
        (out / "install.log").write_text(install.stdout + install.stderr)
        if install.returncode != 0:
            step("install", False, "Your app didn't install. " + plain_install_error(install.stdout),
                 detail=install.stdout[-1500:])
            return result
        installed = guest.run(f"flatpak info --system {app_id}", timeout=30)
        (out / "flatpak-info.txt").write_text(installed.stdout)
        step("install", installed.returncode == 0, "Installed." if installed.returncode == 0 else
             "Your app didn't install.")
        if installed.returncode != 0:
            return result
        env = session_env(uid)
        shot = work / "after.ppm"

        def start(unit: str, ident: str) -> subprocess.CompletedProcess:
            return guest.run(f"runuser -u {USER} -- env {env} systemd-run --user --collect "
                             f"--unit={unit} --property=TimeoutStopSec=10 flatpak run {ident}", timeout=60)

        def watch(unit: str, base_rgb: bytes) -> dict:
            """Sample the unit and the screen until a window shows twice in a row,
            the unit stops, or three minutes pass (a first start builds Flatpak's
            library cache for the runtime, which on the VM's cold disk can take
            over a minute)."""
            alive, seen, unknown, probes = False, 0, 0, 0
            launched = time.time()
            while time.time() - launched < LAUNCH_SECONDS:
                time.sleep(5)
                probe = guest.run(f"runuser -u {USER} -- env {env} systemctl --user is-active {unit}", timeout=20)
                state = parse_unit_state(probe.returncode, probe.stdout)
                probes += 1
                if state == "unknown":
                    unknown += 1
                else:
                    alive = state in ("active", "activating")
                qmp.command("screendump", filename=str(shot))
                fraction = changed_fraction(base_rgb, read_ppm(shot)[2], WIDTH, HEIGHT)
                seen = seen + 1 if fraction > WINDOW_FRACTION else 0
                if (state != "unknown" and not alive) or seen >= 2:
                    break
            seconds = int(time.time() - launched)
            time.sleep(3)
            qmp.command("screendump", filename=str(shot))
            width, height, rgb = read_ppm(shot)
            return {"alive": alive, "seen": seen, "seconds": seconds, "rgb": rgb, "width": width, "height": height,
                    "shown": changed_fraction(base_rgb, rgb, width, height), "blind": unknown == probes}

        def stop(unit: str, ident: str) -> None:
            guest.run(f"runuser -u {USER} -- env {env} systemctl --user stop {unit}", timeout=30)
            guest.run(f"runuser -u {USER} -- env {env} flatpak kill {ident}", timeout=20)

        def control() -> tuple[bool | None, str]:
            """Launch the known-good control app on this VM: did it open a window?"""
            if not control_bundle or not control_app_id or control_app_id == app_id:
                return None, "no control app"
            if not Path(control_bundle).is_file():
                log(f"control: {control_bundle} is missing")
                return None, f"control bundle {control_bundle} is missing"
            stop("depot-review-app", app_id)
            guest.copy(Path(control_bundle), "/var/tmp/control.flatpak")
            added = guest.run("flatpak install --system --noninteractive -y /var/tmp/control.flatpak 2>&1",
                              timeout=900)
            if added.returncode != 0:
                return False, "control app didn't install: " + added.stdout[-600:]
            time.sleep(3)
            qmp.command("screendump", filename=str(work / "control-before.ppm"))
            control_rgb = read_ppm(work / "control-before.ppm")[2]
            if start("depot-review-control", control_app_id).returncode != 0:
                return False, "control app couldn't be started"
            seen = watch("depot-review-control", control_rgb)
            stop("depot-review-control", control_app_id)
            time.sleep(3)
            qmp.command("screendump", filename=str(shot))
            gone = changed_fraction(control_rgb, read_ppm(shot)[2], WIDTH, HEIGHT)
            ok = seen["seen"] >= 2 and window_verdict(seen["shown"], gone)
            log(f"control: {control_app_id} {'opened a window' if ok else 'failed'} ({seen['seconds']} s)")
            return ok, f"control app {control_app_id} {'opened a window' if ok else 'did not open a window'} " \
                       f"on the same VM ({seen['seconds']} s, alive={seen['alive']})"

        def failed_launch(message: str, detail: str) -> dict:
            # What the session was doing: a session that ended under the run is
            # Depot's problem, and this is the evidence Nick reads.
            session = guest.run("loginctl list-sessions --no-legend; loginctl show-user " + USER + " -p State; "
                                "ls -la /run/user/; journalctl -b --no-pager -o short-monotonic "
                                "-u gdm -u systemd-logind _COMM=gnome-shell _COMM=gnome-session-service | tail -n 80",
                                timeout=60)
            (out / "session.log").write_text(session.stdout + session.stderr)
            verdict, note = control()
            if judge_launch(verdict) == "infrastructure":
                couldnt_run("launch", "control_failed", "The test VM couldn't run apps, so your app's launch "
                            "couldn't be judged.", detail=f"{note}\napp: {message}\n{detail}"[-1500:])
            else:
                step("launch", False, message, detail=f"{detail}\n({note})"[-1500:])
            return result

        launch = start("depot-review-app", app_id)
        if launch.returncode != 0:
            return failed_launch("Your app couldn't be started.", launch.stderr[-800:])
        base_rgb = read_ppm(before)[2]
        watched = watch("depot-review-app", base_rgb)
        alive, seen, seconds, shown = watched["alive"], watched["seen"], watched["seconds"], watched["shown"]
        png = out / f"launch-{arch}.png"
        write_png(png, watched["width"], watched["height"], watched["rgb"])
        result["screenshot"] = str(png)
        journal = guest.run(f"journalctl -q --no-pager -o cat _UID={uid} --since=-3min | tail -n 200", timeout=30)
        status = guest.run(f"runuser -u {USER} -- env {env} systemctl --user status --no-pager -l -n 30 "
                           "depot-review-app 2>&1", timeout=30)
        (out / "app-journal.log").write_text(journal.stdout + "\n--- unit\n" + status.stdout)
        if watched["blind"]:
            couldnt_run("launch", "probe_failed", "The test VM stopped answering, so the launch couldn't be watched.",
                        detail="systemctl --user is-active never answered")
            return result
        if not alive:
            return failed_launch("Your app closed right after it started. Its log is attached for you.",
                                 (journal.stdout[-1000:] + "\n" + status.stdout[-500:]))
        if seen < 2:
            procs = guest.run(f"cat /sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/app.slice/"
                              "depot-review-app.service/cgroup.procs 2>/dev/null | xargs -r ps -o comm= -p",
                              timeout=30).stdout
            if stuck_in_ldconfig(procs):
                couldnt_run("launch", "flatpak_ldconfig_timeout", "Flatpak didn't finish preparing the runtime "
                            "in time, so your app's launch couldn't be watched.",
                            detail=f"still in Flatpak's first-run ldconfig after {LAUNCH_SECONDS} s: {procs.split()}")
                return result
            return failed_launch("Your app started but no window appeared within three minutes.",
                                 journal.stdout[-1500:])
        # Stop the app and look again: the change must leave with it.
        stop("depot-review-app", app_id)
        after_stop = 1.0
        for _ in range(4):
            time.sleep(3)
            qmp.command("screendump", filename=str(shot))
            after_stop = changed_fraction(base_rgb, read_ppm(shot)[2], WIDTH, HEIGHT)
            if window_verdict(shown, after_stop):
                break
        log(f"launch: window area {shown:.3f} shown, {after_stop:.3f} after stop")
        if not window_verdict(shown, after_stop):
            step("launch", False, "The screen changed while your app ran, but not because of a window of "
                 "your app. Luma looks at this by hand.", internal=True,
                 detail=f"changed {shown:.3f} while running, {after_stop:.3f} after stopping it")
            return result
        step("launch", True, f"Your app opened a window ({seconds} s).")
        remove = guest.run(f"flatpak uninstall --system --noninteractive -y --delete-data {app_id} 2>&1", timeout=300)
        gone = guest.run(f"! flatpak info --system {app_id} >/dev/null 2>&1 && "
                         f"! test -e /var/lib/flatpak/exports/share/applications/{app_id}.desktop", timeout=30)
        ok = remove.returncode == 0 and gone.returncode == 0
        step("uninstall", ok, "Uninstalled cleanly." if ok else "Your app didn't uninstall cleanly.",
             detail=remove.stdout[-800:])
        result["ok"] = all(s["ok"] for s in steps)
        return result
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        couldnt_run("vm", "vm_error", "The test VM had a problem; Luma will look at it.", detail=str(error))
        return result
    finally:
        try:
            process.terminate()
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
        for name in ("serial.log", "qemu.log"):
            if (work / name).exists():
                shutil.copy(work / name, out / name)
        qmp_path(work).unlink(missing_ok=True)
        shutil.rmtree(work, ignore_errors=True)


def plain_install_error(text: str) -> str:
    lowered = text.lower()
    if "runtime" in lowered and ("not found" in lowered or "not installed" in lowered or "no remote" in lowered):
        return "Its runtime couldn't be found. Build it on a runtime Depot accepts."
    if "no space" in lowered:
        return "It's too big to install."
    return "Flatpak's message is attached."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build-base")
    b.add_argument("--iso", type=Path, required=True)
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--arch", default="x86_64")
    pb = sub.add_parser("prepare-base")
    pb.add_argument("--disk", type=Path, required=True)
    pb.add_argument("--arch", default="x86_64")
    pb.add_argument("--force", action="store_true", help="prepare a base that is already prepared")
    r = sub.add_parser("run")
    r.add_argument("--base", type=Path, required=True)
    r.add_argument("--key", type=Path, required=True)
    r.add_argument("--package", type=Path)
    r.add_argument("--remote-ref", help="install from the VM's remote, e.g. 'luma app/ID/x86_64/stable'")
    r.add_argument("--app-id", required=True)
    r.add_argument("--format", default="flatpak")
    r.add_argument("--arch", default="x86_64")
    r.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare-base":
        prepare_base(args.disk, args.arch, force=args.force)
        return 0
    if args.command == "build-base":
        build_base(args.iso, args.out, args.arch)
        return 0
    result = run_app(args.base, args.key, args.package, args.app_id, args.format, args.out, args.arch,
                     remote_ref=args.remote_ref)
    print(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
