# SPDX-License-Identifier: Apache-2.0
import hashlib
from pathlib import Path
import re
import tomllib
import unittest

DEVICE = Path(__file__).resolve().parents[1]
ROOT = DEVICE.parents[2]

IDENTITY = {"efs", "efs_backup", "modem_userdata", "persist", "devinfo"}


def load(name):
    return tomllib.loads((DEVICE / name).read_text())


class HuskyProfile(unittest.TestCase):
    def test_experimental_tier_and_soc(self):
        device = load("device.toml")
        soc = tomllib.loads((ROOT / "socs/google/zuma/soc.toml").read_text())
        self.assertEqual(device["tier"], "experimental")
        self.assertEqual(device["soc"], "google/zuma")
        self.assertEqual(device["fastboot_product"], "husky")
        self.assertEqual(soc["boot"]["format"], "android-boot-v4")
        self.assertIn("google,husky", device["kit"]["compatible"])

    def test_no_published_release_is_invented(self):
        device = load("device.toml")
        self.assertNotIn("releases", device)
        self.assertNotIn("device_repo", device)
        self.assertEqual(device["updates"]["ostree_ref"], "luma/1/aarch64/nightly")

    def test_identity_partitions_are_preserved(self):
        preserve = set(load("device.toml")["install"]["preserve"])
        self.assertTrue(IDENTITY <= preserve)

    def test_only_userdata_is_written(self):
        device = load("device.toml")
        flashed = [step.split()[1] for step in device["kit"]["flash"] if step.startswith("flash ")]
        self.assertEqual(flashed, ["userdata"])
        guide = (DEVICE / "INSTALL.md").read_text()
        written = re.findall(r"fastboot\s+(?:flash|erase|format)(?::\S+)?\s+(\S+)", guide)
        self.assertEqual(written, ["userdata"])
        self.assertNotIn("--set-active", guide)
        self.assertNotIn("flashing lock", guide)

    def test_avb_footer_matches_husky_abl(self):
        boot = load("boot.toml")
        self.assertEqual(boot["partition_size"], 64 * 1024 * 1024)
        self.assertEqual(boot["avb"]["footer"], "hash")
        self.assertEqual(boot["avb"]["vbmeta"], "untouched")
        self.assertEqual(boot["dtbo"]["policy"], "stock")
        self.assertIn("arm64.nomte", boot["cmdline"]["args"])

    def test_firmware_manifest_is_exact(self):
        device = load("device.toml")
        manifest = (DEVICE / "firmware.manifest").read_bytes()
        self.assertEqual(hashlib.sha256(manifest).hexdigest(),
                         device["firmware"]["manifest_sha256"])


if __name__ == "__main__":
    unittest.main()
