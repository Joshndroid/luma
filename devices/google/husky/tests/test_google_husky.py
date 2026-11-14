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

    def test_command_line_names_the_phone_image(self):
        # Every phone boot image names the generic userdata image's
        # filesystems; the initramfs reads the root's UUID from there.
        cmdline = load("boot.toml")["cmdline"]
        self.assertEqual(cmdline["generated"], ["pmos_root_uuid", "pmos_boot_uuid", "ostree"])
        for arg in cmdline["args"]:
            self.assertFalse(arg.startswith(("pmos_", "ostree=")), arg)
        init = (ROOT / "scripts/mobile/husky/initramfs/init").read_text()
        self.assertIn("pmos_root_uuid=", init)
        self.assertNotIn("03797c05-cf87-4eda-adc4-6ce111baf2d3", init)

    def test_login_screen_gets_the_shared_handheld_defaults(self):
        init = (ROOT / "scripts/mobile/husky/initramfs/init").read_text()
        build = (ROOT / "scripts/mobile/husky/build-boot.sh").read_text()
        self.assertIn("config/mobile/luma-greeter-handheld.dconf", build)
        self.assertIn("/luma/handheld/luma-greeter-handheld.dconf", init)
        self.assertIn("/etc/dconf/db/gdm.d/10-luma-handheld", init)
        self.assertNotIn("power-button-action=", init)

    def test_unused_vendor_module_requests_are_masked(self):
        init = (ROOT / "scripts/mobile/husky/initramfs/init").read_text()
        self.assertIn("for name in fuse-overlayfs open-vm-tools fwupd-i2c i2c-tools", init)
        self.assertIn('ln -sf /dev/null "/run/modules-load.d/$name.conf"', init)

    def test_firmware_manifest_is_exact(self):
        device = load("device.toml")
        manifest = (DEVICE / "firmware.manifest").read_bytes()
        self.assertEqual(hashlib.sha256(manifest).hexdigest(),
                         device["firmware"]["manifest_sha256"])

    def test_modem_data_stays_read_only_and_root_only(self):
        init = (ROOT / "scripts/mobile/husky/initramfs/init").read_text()
        self.assertIn(
            "for name in persist efs efs_backup modem_userdata modem_a modem_b; do",
            init,
        )
        self.assertIn('blockdev --setro "$dev"', init)
        self.assertIn(
            'mount -t ramfs -o mode=0700,nodev,nosuid,noexec ramfs "$modem_fw"',
            init,
        )
        self.assertIn('mount -t f2fs -o ro,norecovery,nodev,nosuid,noexec', init)
        self.assertIn('mount -t ext4 -o ro,noload,nodev,nosuid,noexec', init)
        self.assertIn('chmod 0400 "$modem_fw/$name"', init)
        self.assertIn("PrivateMounts=yes", init)
        self.assertIn('MODE:="0600"', init)


if __name__ == "__main__":
    unittest.main()
