# SPDX-License-Identifier: Apache-2.0
"""scripts/devices/device-kernels.py: a device kernel change must move its package."""
import importlib.machinery
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.machinery.SourceFileLoader("device_kernels", str(ROOT / "scripts/devices/device-kernels.py"))
spec = importlib.util.spec_from_loader("device_kernels", loader)
dk = importlib.util.module_from_spec(spec)
loader.exec_module(dk)

DEVICE = '''schema = 1
vendor = "acme"
codename = "phone"
[kernel]
tag = "luma-kernel-1"
release = "7.2.1"
config = "in/kernel/tree"
patches = ["patches/linux-acme/0001-fix.patch"]
package = "kernel-modules-acme"
dtb = "acme.dtb"
[device_repo]
url = "https://example.invalid/acme/"
install = ["kernel-modules-acme"]
'''
SPEC = "Name: kernel-modules-acme\nVersion: 7.2.1\nRelease: 1.luma.%s%%{?dist}\n"


class DeviceKernels(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.git("init", "-q")
        self.put("devices/acme/phone/device.toml", DEVICE)
        self.put("patches/linux-acme/0001-fix.patch", "diff --git a/x b/x\n")
        self.put("packaging/rpm/kernel-modules-acme.spec", SPEC % 1)
        self.put("config/desktop/packages.txt", "luma-vitals-0.1.0-1.luma.17.fc44.noarch\n")
        self.commit("base")

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       check=True, capture_output=True)

    def put(self, path, text):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def check(self):
        return dk.check(self.root, "HEAD~1", "HEAD")

    def test_a_patch_change_without_a_release_bump_fails(self):
        self.put("patches/linux-acme/0001-fix.patch", "diff --git a/x b/x\n+more\n")
        self.commit("patch")
        errors = self.check()
        self.assertEqual(len(errors), 1)
        self.assertIn("bump its Release in packaging/rpm/kernel-modules-acme.spec", errors[0])

    def test_a_patch_change_with_a_release_bump_passes(self):
        self.put("patches/linux-acme/0001-fix.patch", "diff --git a/x b/x\n+more\n")
        self.put("packaging/rpm/kernel-modules-acme.spec", SPEC % 2)
        self.commit("patch and bump")
        self.assertEqual(self.check(), [])

    def test_a_new_patch_in_the_kernel_table_counts(self):
        self.put("patches/linux-acme/0002-more.patch", "diff --git a/y b/y\n")
        self.put("devices/acme/phone/device.toml", DEVICE.replace(
            '"patches/linux-acme/0001-fix.patch"]', '"patches/linux-acme/0001-fix.patch", "patches/linux-acme/0002-more.patch"]'))
        self.commit("add patch")
        self.assertEqual(len(self.check()), 1)

    def test_delivery_fields_and_unreferenced_patches_are_not_inputs(self):
        self.put("devices/acme/phone/device.toml", DEVICE.replace(
            'package = "kernel-modules-acme"', 'package = "kernel-modules-acme"\nbuild = "scripts/b.sh"'))
        self.put("patches/linux-acme/0009-unused.patch", "diff --git a/z b/z\n")
        self.commit("metadata")
        self.assertEqual(self.check(), [])

    def test_plan_builds_what_the_device_repository_lacks(self):
        served = {"kernel-modules-acme": ["7.2.1-1.luma.1.fc44"]}
        rows = dk.plan(self.root, fetch=lambda url, package: served[package])
        self.assertEqual(rows[0]["action"], "current")
        self.put("packaging/rpm/kernel-modules-acme.spec", SPEC % 2)
        rows = dk.plan(self.root, fetch=lambda url, package: served[package])
        self.assertEqual((rows[0]["ships"], rows[0]["action"]), ("device-repo", "build"))

    def test_plan_leaves_image_pinned_kernels_to_the_image(self):
        self.put("config/desktop/packages.txt", "kernel-modules-acme-7.2.1-1.luma.1.fc44.aarch64\n")
        self.assertEqual(dk.plan(self.root, fetch=lambda *_: [])[0]["ships"], "image")

    def test_every_device_in_this_tree_names_its_kernel_package(self):
        for path in dk.tree_devices(ROOT):
            device = dk.load(dk.tree_reader(ROOT), path)
            if "kernel" in device:
                package = device["kernel"].get("package")
                self.assertTrue(package, f"{path}: [kernel] package")
                self.assertTrue((ROOT / f"packaging/rpm/{package}.spec").is_file(), f"{path}: {package}.spec")
                if device["kernel"].get("build"):
                    self.assertTrue((ROOT / device["kernel"]["build"]).is_file(), f"{path}: build script")


if __name__ == "__main__":
    unittest.main()
