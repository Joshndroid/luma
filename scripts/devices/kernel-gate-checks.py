#!/usr/bin/python3 -I
# SPDX-License-Identifier: Apache-2.0
"""Checks a phone kernel must pass on the phone before its package is published.

Runs ON the phone (stdlib only), as root:

  cat kernel-gate-checks.py | ssh root@phone python3 - [--notes SHA256] [--samples N]

  notes     /sys/kernel/notes hashes to the kernel the package declares
  power     the system battery's power_now agrees with voltage_now x current_now:
            sampled while at least 100 mA flows, the mean power_now must be
            nonzero and within 2x of the mean |V x I|. FP6 selinux1.9 shipped
            power_now = 0 (milos is the SM8550 battmgr variant); selinux1.8
            reported 10x V x I.
  upower    UPower reports an energy rate for that battery

Prints one JSON object; exit status 0 only when every check passed. With no
current flowing the power check cannot judge and fails as "inconclusive":
charge or load the phone and run it again.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

SUPPLY = Path("/sys/class/power_supply")
MIN_CURRENT_UA = 100_000


def read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def battery() -> Path | None:
    for entry in sorted(SUPPLY.iterdir()) if SUPPLY.is_dir() else []:
        if read(entry / "type") == "Battery" and read(entry / "scope") != "Device" and read(entry / "present") != "0":
            return entry
    return None


def check_notes(expected: str | None) -> dict:
    actual = hashlib.sha256(Path("/sys/kernel/notes").read_bytes()).hexdigest()
    if not expected:
        return {"ok": True, "notes": actual, "note": "no expected value given"}
    return {"ok": actual == expected, "notes": actual, "expected": expected}


def check_power(samples: int) -> dict:
    bat = battery()
    if bat is None:
        return {"ok": True, "note": "no system battery"}
    if not (bat / "power_now").exists():
        return {"ok": True, "battery": bat.name, "note": "no power_now"}
    powers, products, currents = [], [], []
    for _ in range(samples):
        try:
            v, i, p = (int(read(bat / name)) for name in ("voltage_now", "current_now", "power_now"))
        except ValueError:
            return {"ok": False, "battery": bat.name, "error": "unreadable voltage_now/current_now/power_now"}
        powers.append(abs(p))
        products.append(abs(v * i) / 1e6)
        currents.append(abs(i))
        time.sleep(1)
    mean_p, mean_vi, mean_i = (sum(x) / len(x) for x in (powers, products, currents))
    row = {"battery": bat.name, "power_now_uw": round(mean_p), "v_x_i_uw": round(mean_vi),
           "current_ua": round(mean_i), "samples": samples}
    if mean_i < MIN_CURRENT_UA:
        return dict(row, ok=False, result="inconclusive", note="under 100 mA flowing; charge or load the phone")
    ratio = mean_p / mean_vi if mean_vi else 0.0
    row["ratio"] = round(ratio, 3)
    row["ok"] = mean_p > 0 and 0.5 <= ratio <= 2.0
    return row


def check_upower() -> dict:
    bat = battery()
    if bat is None:
        return {"ok": True, "note": "no system battery"}
    path = f"/org/freedesktop/UPower/devices/battery_{bat.name.replace('-', '_')}"
    try:
        out = subprocess.run(["upower", "-i", path], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "error": str(error)}
    rate = next((line.split(":", 1)[1].strip() for line in out.splitlines() if "energy-rate" in line), "")
    try:
        value = float(rate.split()[0])
    except (IndexError, ValueError):
        return {"ok": False, "energy_rate": rate}
    return {"ok": value > 0, "energy_rate": rate}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--notes")
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    result = {"notes": check_notes(args.notes), "power": check_power(args.samples), "upower": check_upower()}
    result["pass"] = all(row["ok"] for row in result.values())
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
