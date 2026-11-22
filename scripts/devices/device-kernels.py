#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Device kernels reach devices through their package, so the package moves with the kernel.

Every devices/<vendor>/<codename>/device.toml with a [kernel] names the RPM
that ships that kernel to the device ([kernel] package): a phone's
kernel-modules package, which carries its boot image for luma-device-boot, or a
board kernel the OS image pins. A kernel change that leaves that package's
release alone never reaches a device: nothing rebuilds it and nothing installs
it (the FP6's 0041/0042 sat on main for two weeks).

  device-kernels.py check --base REV --head REV
      CI: fail when a device's kernel inputs changed between the revisions
      and its package's Version-Release in packaging/rpm/<package>.spec did
      not. Inputs are the [kernel] table (without package, build and the
      android_gpu record) and every repository file it names: patches,
      config fragments, config, the source lock and the lock's repository
      patches.

  device-kernels.py plan [--root DIR] [--json]
      Nightly: for each device, where its kernel package ships and whether
      the tree's release is newer than what devices get. "image": pinned in
      config/desktop/packages.txt, the OS image build ships it. "build": the
      device repository ([device_repo] url) serves an older release, so the
      ARM nightly builds it ([kernel] build) and stages it for that
      repository. "current": the repository already serves it.

  device-kernels.py build --device VENDOR/CODENAME --output DIR [--root DIR]
      Run the device's [kernel] build script with --output DIR.

  device-kernels.py gate --device VENDOR/CODENAME --package-dir DIR --shell CMD --gates DIR
      The automated part of the phone gate, on a phone already running the
      kernel under test (RAM boot, or installed through luma-device-boot):
      scripts/devices/kernel-gate-checks.py through CMD (for example
      "ssh root@phone"), with the kernel notes the package in DIR declares.
      All checks passing writes GATES/<vendor>-<codename>/<package>-<evr>.pass
      with their result; the person running the gate adds what only a person
      can see (touch after resume) to that record.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import lzma
import re
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).resolve().parents[2]
DIST = ".fc44"
#: [kernel] keys that describe delivery or records, not what is built.
NOT_INPUTS = ("package", "build", "android_gpu")

Reader = Callable[[str], Optional[bytes]]


def tree_reader(root: Path) -> Reader:
    def read(path: str) -> Optional[bytes]:
        try:
            return (root / path).read_bytes()
        except OSError:
            return None
    return read


def git_reader(root: Path, rev: str) -> Reader:
    def read(path: str) -> Optional[bytes]:
        result = subprocess.run(["git", "-C", str(root), "show", f"{rev}:{path}"], capture_output=True)
        return result.stdout if result.returncode == 0 else None
    return read


def git_devices(root: Path, rev: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "--name-only", rev, "devices/"],
                         capture_output=True, text=True, check=True).stdout
    return sorted(p for p in out.splitlines() if re.fullmatch(r"devices/[^/]+/[^/]+/device\.toml", p))


def tree_devices(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.glob("devices/*/*/device.toml"))


def load(read: Reader, path: str) -> Optional[dict]:
    data = read(path)
    return tomllib.loads(data.decode()) if data is not None else None


def kernel_inputs(read: Reader, device: dict) -> dict[str, str]:
    """What the kernel is built from: the [kernel] table and the repository files it names."""
    kernel = device.get("kernel", {})
    inputs = {"[kernel]": json.dumps({k: v for k, v in kernel.items() if k not in NOT_INPUTS}, sort_keys=True)}
    paths = list(kernel.get("patches", [])) + list(kernel.get("config_fragments", []))
    for key in ("config", "source_lock"):
        if kernel.get(key):
            paths.append(kernel[key])
    lock = read(kernel["source_lock"]) if kernel.get("source_lock") else None
    if lock is not None:
        try:
            paths += [row["path"] for row in json.loads(lock).get("patches", []) if row.get("source") == "repository"]
        except (ValueError, TypeError, KeyError):
            pass
    for path in paths:
        data = read(path)
        # A config that lives in the kernel tree (FP6) is not a repository file;
        # its name is in the [kernel] table already.
        if data is not None:
            inputs[path] = hashlib.sha256(data).hexdigest()
    return inputs


def spec_evr(read: Reader, package: str) -> Optional[str]:
    data = read(f"packaging/rpm/{package}.spec")
    if data is None:
        return None
    text = data.decode()
    fields = {}
    for key in ("Version", "Release"):
        match = re.search(rf"^{key}:\s*(\S+)", text, re.MULTILINE)
        if not match:
            return None
        fields[key] = match.group(1).replace("%{?dist}", "")
    return f"{fields['Version']}-{fields['Release']}"


def check(root: Path, base: str, head: str) -> list[str]:
    errors = []
    before_read, after_read = git_reader(root, base), git_reader(root, head)
    for path in git_devices(root, head):
        after = load(after_read, path)
        before = load(before_read, path)
        if not after or "kernel" not in after or not before or "kernel" not in before:
            continue  # a new device, or one without a kernel of its own
        old, new = kernel_inputs(before_read, before), kernel_inputs(after_read, after)
        if old == new:
            continue
        changed = sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))
        package = after["kernel"].get("package")
        if not package:
            errors.append(f"{path}: kernel inputs changed ({', '.join(changed)}) and [kernel] names no "
                          "package that ships this kernel")
            continue
        old_evr, new_evr = spec_evr(before_read, before["kernel"].get("package", package)), spec_evr(after_read, package)
        if new_evr is None:
            errors.append(f"{path}: packaging/rpm/{package}.spec is missing or has no Version/Release")
        elif old_evr == new_evr:
            errors.append(f"{path}: kernel inputs changed ({', '.join(changed)}) but {package} is still "
                          f"{new_evr}: bump its Release in packaging/rpm/{package}.spec so devices get the kernel")
    return errors


def pinned(root: Path) -> set[str]:
    names = set()
    for line in (root / "config/desktop/packages.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if re.search(r"\.(x86_64|aarch64|noarch)$", line):
            names.add(line.rsplit(".", 1)[0].rsplit("-", 2)[0])
    return names


def repo_versions(url: str, package: str) -> list[str]:
    """Every Version-Release of PACKAGE an rpm-md repository serves."""
    base = url.rstrip("/") + "/"
    with urllib.request.urlopen(base + "repodata/repomd.xml", timeout=30) as response:
        repomd = response.read().decode()
    href = re.search(r'<data type="primary">.*?<location href="([^"]+)"', repomd, re.S)
    if not href:
        raise ValueError("repomd.xml has no primary data")
    with urllib.request.urlopen(base + href.group(1), timeout=60) as response:
        raw = response.read()
    if href.group(1).endswith(".gz"):
        raw = gzip.decompress(raw)
    elif href.group(1).endswith(".xz"):
        raw = lzma.decompress(raw)
    elif href.group(1).endswith(".zst"):
        raw = subprocess.run(["zstd", "-dc"], input=raw, capture_output=True, check=True).stdout
    found = []
    for match in re.finditer(r"<name>([^<]+)</name>.*?<version [^>]*ver=\"([^\"]+)\" rel=\"([^\"]+)\"",
                             raw.decode(), re.S):
        if match.group(1) == package:
            found.append(f"{match.group(2)}-{match.group(3)}")
    return found


def newer(a: str, b: str) -> bool:
    """rpm's ordering where rpm is installed; otherwise any difference counts."""
    try:
        import rpm  # noqa: PLC0415
        split = lambda evr: ("0",) + tuple(evr.rsplit("-", 1))  # noqa: E731
        return rpm.labelCompare(split(a), split(b)) > 0
    except ImportError:
        return a != b


def plan(root: Path, fetch=repo_versions) -> list[dict]:
    read = tree_reader(root)
    pins = pinned(root)
    rows = []
    for path in tree_devices(root):
        device = load(read, path) or {}
        kernel = device.get("kernel", {})
        package = kernel.get("package")
        if not package:
            continue
        slug = f"{device.get('vendor')}/{device.get('codename')}"
        evr = (spec_evr(read, package) or "") + DIST
        row = {"device": slug, "package": package, "tree": evr}
        repo = device.get("device_repo", {})
        if package in pins:
            row["ships"] = "image"
        elif package in list(repo.get("install", [])) + list(repo.get("replace", [])):
            served = fetch(repo["url"], package)
            row.update(ships="device-repo", repo=repo["url"], served=served,
                       build=kernel.get("build"))
            row["action"] = "current" if any(not newer(evr, s) for s in served) else "build"
        else:
            row["ships"] = "nowhere"
        rows.append(row)
    return rows


def build(root: Path, slug: str, output: Path) -> int:
    path = f"devices/{slug}/device.toml"
    device = load(tree_reader(root), path)
    if not device or not device.get("kernel", {}).get("build"):
        print(f"{path}: [kernel] has no build script", file=sys.stderr)
        return 2
    return subprocess.run(["bash", str(root / device["kernel"]["build"]), "--output", str(output)], cwd=root).returncode


def declared_notes(package_dir: Path) -> tuple[str, str, str]:
    """(package, evr, kernel_notes_sha256) of the device kernel package in DIR."""
    rpms = sorted(p for p in package_dir.glob("*.rpm") if not p.name.endswith(".src.rpm"))
    for rpm_file in rpms:
        name, evr = subprocess.run(["rpm", "-qp", "--nosignature", "--qf", "%{NAME} %{VERSION}-%{RELEASE}", str(rpm_file)],
                                   capture_output=True, text=True, check=True).stdout.split()
        listing = subprocess.run(["rpm", "-qlp", "--nosignature", str(rpm_file)], capture_output=True, text=True,
                                 check=True).stdout.split()
        manifest = next((p for p in listing if p.endswith("/device-boot.json")), None)
        if not manifest:
            continue
        data = subprocess.run(f"rpm2cpio '{rpm_file}' | cpio -i --quiet --to-stdout '.{manifest}'", shell=True,
                              capture_output=True, check=True).stdout
        return name, evr, json.loads(data).get("kernel_notes_sha256", "")
    raise ValueError(f"{package_dir} holds no package with a device-boot declaration")


def gate(root: Path, slug: str, package_dir: Path, shell: str, gates: Path) -> int:
    package, evr, notes = declared_notes(package_dir)
    script = (root / "scripts/devices/kernel-gate-checks.py").read_bytes()
    command = f"{shell} python3 - --notes {notes}" if notes else f"{shell} python3 -"
    result = subprocess.run(command, shell=True, input=script, capture_output=True)
    sys.stdout.write(result.stdout.decode())
    if result.returncode != 0:
        print(f"FAIL  {slug} {package}-{evr}: kernel gate checks failed; nothing recorded", file=sys.stderr)
        return 1
    record = gates / slug.replace("/", "-") / f"{package}-{evr}.pass"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"device": slug, "package": package, "evr": evr,
                                  "checks": json.loads(result.stdout), "time": time.time()}, indent=2) + "\n")
    print(f"PASS  {slug} {package}-{evr}: {record}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--base", required=True)
    c.add_argument("--head", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--json", action="store_true")
    b = sub.add_parser("build")
    b.add_argument("--device", required=True)
    b.add_argument("--output", type=Path, required=True)
    g = sub.add_parser("gate")
    g.add_argument("--device", required=True)
    g.add_argument("--package-dir", type=Path, required=True)
    g.add_argument("--shell", required=True)
    g.add_argument("--gates", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "gate":
        return gate(args.root, args.device, args.package_dir, args.shell, args.gates)
    if args.command == "check":
        errors = check(args.root, args.base, args.head)
        for error in errors:
            print(f"FAIL  {error}")
        if not errors:
            print("PASS  device kernel packages move with their kernels")
        return 1 if errors else 0
    if args.command == "plan":
        rows = plan(args.root)
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            for row in rows:
                print(f"{row['device']:24} {row['package']:28} {row['tree']:44} {row['ships']}"
                      + (f" {row['action']} (serves {', '.join(row['served']) or 'nothing'})" if "action" in row else ""))
        return 0
    return build(args.root, args.device, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
