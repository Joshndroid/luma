# SPDX-License-Identifier: Apache-2.0
"""Host headroom for the review VM on the shared build server.

A review VM takes its own lane lock (the luma-heavy conform-lane pattern: one
review VM beside one build), then waits until the host has the VM's memory
plus a reserve free, the 1-minute load is under 0.75 per CPU, and
luma-heavy.slice is not frozen by the thermal guard.
Only then does it boot, so boot timeouts count from a VM that can run.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import time
from contextlib import contextmanager


class NoCapacity(RuntimeError):
    """The host had no room for the VM within the wait: an infrastructure outcome."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def mem_available_mib(path: str = "/proc/meminfo") -> int:
    with open(path) as meminfo:
        for line in meminfo:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def host_busy(cool_celsius: int = 71, load_per_cpu: float = 0.75, loadavg=os.getloadavg) -> str:
    """Why the host is too loaded, hot or frozen to start a VM now, or ""."""
    load, cpus = loadavg()[0], os.cpu_count() or 1
    if load > cpus * load_per_cpu:
        return f"load {load:.1f} on {cpus} CPUs"
    try:
        frozen = subprocess.run(["systemctl", "show", "luma-heavy.slice", "-p", "FreezerState", "--value"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        frozen = ""
    if frozen == "frozen":
        return "luma-heavy.slice is frozen by the thermal guard"
    if shutil.which("luma-nvme-temp"):
        try:
            celsius = int(subprocess.run(["luma-nvme-temp"], capture_output=True, text=True, timeout=10).stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return ""
        if celsius >= cool_celsius:
            return f"NVMe at {celsius} C"
    return ""


@contextmanager
def slot(lock_path: str, need_mib: int, wait_seconds: float, *, poll: float = 15,
         clock=time.monotonic, sleep=time.sleep, available=mem_available_mib, busy=host_busy, note=None):
    """Hold the review lane with headroom for one VM, or raise NoCapacity."""
    deadline = clock() + wait_seconds
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        waited = False
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if clock() >= deadline:
                    raise NoCapacity("lock", f"another review VM held {lock_path} for {int(wait_seconds)} s")
                if note and not waited:
                    note(f"capacity: waiting for {lock_path}")
                waited = True
                sleep(poll)
        waited = False
        while True:
            free, why = available(), busy()
            if free >= need_mib and not why:
                break
            if clock() >= deadline:
                if why:
                    raise NoCapacity("busy", f"host stayed busy for {int(wait_seconds)} s: {why}")
                raise NoCapacity("memory", f"host had {free} MiB available, the VM needs {need_mib} MiB, "
                                           f"for {int(wait_seconds)} s")
            if note and not waited:
                note(f"capacity: waiting for headroom ({free} MiB available, need {need_mib} MiB"
                     + (f", {why})" if why else ")"))
            waited = True
            sleep(poll)
        yield
    finally:
        os.close(fd)
