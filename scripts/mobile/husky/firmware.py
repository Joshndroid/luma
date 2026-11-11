#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Pixel 8 Pro (husky) firmware: extract it from Google's factory image and
check it against devices/google/husky/firmware.manifest.

  firmware.py extract --factory husky-<build>-factory-<id>.zip \\
                      --sha256 <checksum Google publishes> --out DIR [--work DIR]
  firmware.py manifest DIR        print the manifest for a firmware tree
  firmware.py check DIR [MANIFEST] the tree must match the manifest exactly

The firmware is Google's and not redistributable: it is never committed, and
DIR stays on the build host. Download the factory image yourself from
https://developers.google.com/android/images#husky (the page asks you to accept
Google's terms). Extraction reads vendor.img (or vendor_a from super.img) and
needs unzip, simg2img, lpunpack and debugfs (vendor is ext4).

The tree is laid out as the kernel requests the files (the boot image puts it
in the initramfs as /lib/firmware). The speakers' factory calibration is not
here: it is per phone and is read on the phone, read-only, from persist.
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = ROOT / "devices/google/husky/firmware.manifest"

# (vendor path or directory, store path, required). A directory copies its tree.
FILES = [
    # CS40L26 haptics (the driver asks for these names at probe).
    ("firmware/cs40l26.wmfw", "cs40l26.wmfw", True),
    ("firmware/cs40l26.bin", "cs40l26.bin", True),
    ("firmware/cs40l26-svc.bin", "cs40l26-svc.bin", True),
    ("firmware/cs40l26-dvl.bin", "cs40l26-dvl.bin", True),
    ("firmware/cs40l26-dbc.bin", "cs40l26-dbc.bin", True),
    ("firmware/cs40l26-calib.wmfw", "cs40l26-calib.wmfw", True),
    # Bluetooth patch RAM; hci_aoc's Broadcom setup asks for brcm/BCM.hcd.
    ("firmware/brcm/BCM.hcd", "brcm/BCM.hcd", True),
    # AoC image and the sensor registry the AoC loads through the kernel.
    ("firmware/aoc.bin", "google/zuma/aoc.bin", True),
    ("etc/sensors/registry", "google/usf/husky", True),
    # CS35L41 speaker protection (Cirrus CSPL), under the names the mainline
    # driver asks for with subsystem ID husky. All three or the speakers stay
    # muted.
    ("firmware/cs35l41-dsp1-spk-prot.wmfw", "cirrus/cs35l41-dsp1-spk-prot-husky.wmfw", True),
    ("firmware/cs35l41-dsp1-spk-prot.bin", "cirrus/cs35l41-dsp1-spk-prot-husky.bin", True),
    ("firmware/R-cs35l41-dsp1-spk-prot.bin", "cirrus/cs35l41-dsp1-spk-prot-husky-r.bin", True),
]
# BCM4398 Wi-Fi: firmware, NVRAM, CLM and TX-cap blobs for every chip revision
# and SKU on the image; bcmdhd appends the revision or SKU itself. The symbol
# maps (fw_bcmdhd.map_*) are for crash dumps only and are left out.
WIFI_PREFIXES = ("fw_bcmdhd.bin_", "bcmdhd.cal_", "bcmdhd_clm.blob_", "bcmdhd_txcap.blob_")
WIFI_REQUIRED = ("fw_bcmdhd.bin_4398_d0", "bcmdhd_clm.blob_4398_d0", "bcmdhd_txcap.blob_4398_d0")


def run(*cmd, **kw):
    subprocess.run(cmd, check=True, **kw)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def debugfs(image, request):
    return subprocess.run(["debugfs", "-R", request, str(image)], check=True,
                          capture_output=True, text=True).stdout


def dump(image, src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    run("debugfs", "-R", f"dump -p {src} {dest}", str(image), capture_output=True)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise SystemExit(f"error: {src} not found in vendor")
    os.chmod(dest, 0o644)


def listing(image, directory):
    """Names in a vendor directory (debugfs ls -p: /inode/mode/uid/gid/name/size/)."""
    out = []
    for line in debugfs(image, f"ls -p {directory}").splitlines():
        parts = line.strip().split("/")
        if len(parts) >= 7 and parts[5] not in (".", ".."):
            out.append((parts[5], parts[2].startswith("04")))
    return out


def dump_tree(image, src, dest):
    entries = listing(image, src)
    if not entries:
        raise SystemExit(f"error: {src} is empty or missing in vendor")
    for name, is_dir in entries:
        if is_dir:
            dump_tree(image, f"{src}/{name}", dest / name)
        else:
            dump(image, f"{src}/{name}", dest / name)


def unsparse(path):
    """A raw copy of an Android sparse image (or the image itself if raw)."""
    with open(path, "rb") as f:
        magic = f.read(4)
    if magic != b"\x3a\xff\x26\xed":
        return path
    raw = path.with_suffix(".raw")
    run("simg2img", str(path), str(raw))
    path.unlink()
    return raw


def extract(args):
    factory = Path(args.factory).resolve()
    out = Path(args.out).resolve()
    if sha256(factory) != args.sha256.lower():
        raise SystemExit(f"error: {factory.name} does not match the published SHA-256")
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"error: {out} is not empty")
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.work) as tmp:
        tmp = Path(tmp)
        inner = [n for n in subprocess.run(["unzip", "-Z1", str(factory)], check=True,
                                           capture_output=True, text=True).stdout.split()
                 if PurePosixPath(n).name.startswith("image-husky-") and n.endswith(".zip")]
        if len(inner) != 1:
            raise SystemExit("error: expected one image-husky-*.zip in the factory image")
        run("unzip", "-q", "-o", str(factory), inner[0], "-d", str(tmp))
        image = tmp / inner[0]
        members = subprocess.run(["unzip", "-Z1", str(image)], check=True,
                                 capture_output=True, text=True).stdout.split()
        # Factory images carry vendor.img on its own (fastboot builds super on
        # the phone); some builds ship a prebuilt super.img instead.
        member = "vendor.img" if "vendor.img" in members else "super.img"
        if member not in members:
            raise SystemExit("error: neither vendor.img nor super.img in the image zip")
        run("unzip", "-q", "-o", str(image), member, "-d", str(tmp))
        image.unlink()
        vendor = unsparse(tmp / member)
        if member == "super.img":
            parts = tmp / "parts"
            parts.mkdir()
            for name in ("vendor_a", "vendor"):
                if subprocess.run(["lpunpack", f"--partition={name}", str(vendor), str(parts)],
                                  capture_output=True).returncode == 0 and (parts / f"{name}.img").exists():
                    found = parts / f"{name}.img"
                    break
            else:
                raise SystemExit("error: no vendor partition in super.img")
            vendor.unlink()
            vendor = found
        with open(vendor, "rb") as f:
            f.seek(0x438)
            if f.read(2) != b"\x53\xef":
                raise SystemExit("error: vendor is not ext4 (an EROFS vendor needs another extractor)")
        for src, dest, required in FILES:
            names = [n for n, _ in listing(vendor, str(PurePosixPath(src).parent))]
            if PurePosixPath(src).name not in names:
                if required:
                    raise SystemExit(f"error: {src} not in vendor")
                continue
            if src == "etc/sensors/registry":
                dump_tree(vendor, src, out / dest)
            else:
                dump(vendor, src, out / dest)
        wifi = [n for n, d in listing(vendor, "firmware") if not d and n.startswith(WIFI_PREFIXES)]
        for name in WIFI_REQUIRED:
            if name not in wifi:
                raise SystemExit(f"error: firmware/{name} not in vendor")
        for name in sorted(wifi):
            dump(vendor, f"firmware/{name}", out / "bcmdhd" / name)
        build = debugfs(vendor, "cat build.prop")
    for line in build.splitlines():
        if line.startswith("ro.vendor.build.fingerprint="):
            print(line.split("=", 1)[1])
    print(f"{sum(1 for p in out.rglob('*') if p.is_file())} files in {out}")


def manifest_rows(tree):
    tree = Path(tree)
    rows = []
    for path in sorted(tree.rglob("*"), key=lambda p: p.relative_to(tree).as_posix()):
        rel = path.relative_to(tree).as_posix()
        if path.is_symlink():
            rows.append(("L", "777", "-", rel, os.readlink(path)))
        elif path.is_dir():
            rows.append(("D", "755", "-", rel, "-"))
        else:
            rows.append(("F", "644", sha256(path), rel, "-"))
    files = sum(1 for r in rows if r[0] == "F")
    return "\n".join("\t".join(r) for r in [("H", str(files), "-", "-", "-")] + rows) + "\n"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--factory", required=True)
    e.add_argument("--sha256", required=True)
    e.add_argument("--out", required=True)
    e.add_argument("--work", default=None, help="scratch directory (needs ~20 GB)")
    m = sub.add_parser("manifest")
    m.add_argument("tree")
    c = sub.add_parser("check")
    c.add_argument("tree")
    c.add_argument("manifest", nargs="?", default=str(MANIFEST))
    args = p.parse_args()
    if args.cmd == "extract":
        extract(args)
    elif args.cmd == "manifest":
        sys.stdout.write(manifest_rows(args.tree))
    else:
        want = Path(args.manifest).read_text()
        if manifest_rows(args.tree) != want:
            raise SystemExit(f"error: {args.tree} does not match {args.manifest}")
        print("firmware matches the manifest")


if __name__ == "__main__":
    main()
