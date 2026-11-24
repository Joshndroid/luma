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

    def test_kernel_uses_the_luma_zuma_branch_layout(self):
        kernel = load("device.toml")["kernel"]
        self.assertEqual(kernel["tag"], "luma-kernel-7.2.15-zuma")
        self.assertEqual(kernel["release"], "7.2.15-zuma")
        self.assertEqual(kernel["config"], "luma/config/zuma.fragment")
        build = (ROOT / "scripts/mobile/husky/build-boot.sh").read_text()
        self.assertIn("luma/config/luma.fragment", build)
        self.assertIn("scripts/kconfig/merge_config.sh", build)

    def test_kernel_package_carries_this_kernels_modules_only(self):
        kernel = load("device.toml")["kernel"]
        self.assertEqual(kernel["package"], "kernel-modules-luma-husky")
        self.assertNotIn("build", kernel)
        spec = (ROOT / "packaging/rpm/kernel-modules-luma-husky.spec").read_text()
        self.assertIn(f"%global kver {kernel['release']}\n", spec)
        self.assertIn(f"Version:        {kernel['release'].split('-')[0]}\n", spec)
        # No boot image (Google's firmware) and nothing for luma-device-boot.
        self.assertNotIn("device-boot", spec.split("%files", 1)[1])
        self.assertNotIn("Requires:", spec.replace("BuildRequires:", ""))
        # The spec checks every module the boot image loads.
        build = (ROOT / "scripts/mobile/husky/build-boot.sh").read_text()
        loaded = re.search(r'^modules="(.*?)"', build, re.S | re.M).group(1).split()
        checked = re.search(r"for k in (.*?); do", spec, re.S).group(1).replace("\\", " ").split()
        self.assertEqual(checked, loaded)
        self.assertIn('INSTALL_MOD_PATH="$out/modules" INSTALL_MOD_STRIP=1 modules_install', build)
        # The initramfs copies come from that signed tree; stripping after
        # signing would drop the signature and taint the kernel.
        self.assertNotIn("llvm-strip", build)
        self.assertIn("Module signature appended", build)

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

    def test_suspend_is_s2idle_only(self):
        init = (ROOT / "scripts/mobile/husky/initramfs/init").read_text()
        conf = init.split("/run/systemd/sleep.conf.d/50-luma-husky-sleep.conf <<'EOF'")[1]
        conf = conf.split("\nEOF")[0].split()
        self.assertEqual(conf, ["[Sleep]", "AllowSuspend=yes", "MemorySleepMode=s2idle",
                                "AllowHibernation=no", "AllowSuspendThenHibernate=no",
                                "AllowHybridSleep=no"])
        self.assertNotIn("no-sleep", init)

    def test_idle_suspend_on_battery_only(self):
        init = (ROOT / "scripts/mobile/husky/initramfs/init").read_text()
        build = (ROOT / "scripts/mobile/husky/build-boot.sh").read_text()
        self.assertIn('"$here/initramfs/luma-husky-power.dconf"', build)
        self.assertIn("for db in luma gdm; do", init)
        self.assertIn("/sysroot/etc/dconf/db/$db.d/20-luma-husky-power", init)
        dconf = (ROOT / "scripts/mobile/husky/initramfs/luma-husky-power.dconf").read_text()
        keys = [line for line in dconf.splitlines() if line and not line.startswith("#")]
        self.assertEqual(keys, ["[org/gnome/settings-daemon/plugins/power]",
                                "sleep-inactive-battery-type='suspend'",
                                "sleep-inactive-battery-timeout=120",
                                "sleep-inactive-ac-type='nothing'"])

    def test_nfc_driver_loads_in_dependency_order(self):
        init = (ROOT / "scripts/mobile/husky/initramfs/init").read_text()
        build = (ROOT / "scripts/mobile/husky/build-boot.sh").read_text()
        loaded = re.search(r'^modules="(.*?)"', build, re.S | re.M).group(1).split()
        self.assertEqual(loaded[-4:], ["net/nfc/nfc", "net/nfc/nci/nci",
                                       "drivers/nfc/st-nci/st-nci",
                                       "drivers/nfc/st-nci/st-nci_i2c"])
        self.assertIn("for m in nfc nci st-nci st-nci_i2c; do", init)
        self.assertIn("ConditionPathExists=!/var/lib/luma-husky/nfc-off", init)
        self.assertIn("ln -sf ../luma-husky-nfc.service", init)

    def test_unused_vendor_module_requests_are_masked(self):
        init = (ROOT / "scripts/mobile/husky/initramfs/init").read_text()
        self.assertIn("for name in fuse-overlayfs open-vm-tools fwupd-i2c i2c-tools", init)
        self.assertIn('ln -sf /dev/null "/run/modules-load.d/$name.conf"', init)

    def test_firmware_manifest_is_exact(self):
        device = load("device.toml")
        manifest = (DEVICE / "firmware.manifest").read_bytes()
        self.assertEqual(hashlib.sha256(manifest).hexdigest(),
                         device["firmware"]["manifest_sha256"])

    def test_speakers_play_at_the_phones_factory_volume(self):
        audio = ROOT / "scripts/mobile/husky/initramfs/audio"
        hifi = (audio / "HiFi.conf").read_text()
        speaker = hifi.split('SectionDevice."Speaker"')[1]
        enable = speaker.split("EnableSequence [")[1].split("]")[0]
        # The route only runs the gate, which applies the calibrated gains;
        # no fixed cap below the factory level is left in the profile.
        self.assertEqual(enable.split(), ["exec", '"/run/luma-husky-audio/speaker-ready"'])
        self.assertNotIn("673", hifi)
        calibrate = (audio / "speaker-calibrate").read_text()
        self.assertIn("ambient <= 85 and analog <= 31 and digital <= 913", calibrate)
        self.assertIn("GAINS.write_text(' '.join('%d %d' % (rec[4], rec[5]) for rec in records)",
                      calibrate)
        self.assertLess(calibrate.index("GAINS.write_text"), calibrate.index("REPORT.write_text"))
        ready = (audio / "speaker-ready").read_text()
        self.assertIn('[ "$(stat -c %u "$f")" = 0 ]', ready)
        self.assertIn('[ "$la" -le 31 ] && [ "$ra" -le 31 ] && [ "$ld" -le 913 ] && [ "$rd" -le 913 ]',
                      ready)
        self.assertIn("set_amp '' \"$la\" \"$ld\" && set_amp 'R ' \"$ra\" \"$rd\"", ready)

    def test_microphones_record_with_gain(self):
        hifi = (ROOT / "scripts/mobile/husky/initramfs/audio/HiFi.conf").read_text()
        mic = hifi.split('SectionDevice."Mic"')[1].split("SectionDevice")[0]
        enable = mic.split("EnableSequence [")[1].split("]")[0]
        self.assertIn("cset \"name='Mic HW Gain (cB)' 130\"", enable)
        self.assertIn("cset \"name='Mic Record Soft Gain (dB)' 12\"", enable)

    def test_wireplumber_keeps_playback_ahead_of_the_aoc(self):
        audio = ROOT / "scripts/mobile/husky/initramfs/audio"
        rule = (audio / "wireplumber-aoc.conf").read_text()
        self.assertIn('node.name = "~alsa_output.platform-aoc-sound.*"', rule)
        self.assertIn("api.alsa.headroom = 2048", rule)
        build = (ROOT / "scripts/mobile/husky/build-boot.sh").read_text()
        self.assertIn('"$here/initramfs/audio/wireplumber-aoc.conf" "$work/root/luma/audio/wireplumber-aoc.conf"',
                      build)
        init = (ROOT / "scripts/mobile/husky/initramfs/init").read_text()
        self.assertIn("/run/luma-husky-xdg/wireplumber/wireplumber.conf.d/51-luma-husky-aoc.conf", init)
        # Only WirePlumber searches the /run directory, and still finds /etc/xdg.
        self.assertIn("Environment=XDG_CONFIG_DIRS=/run/luma-husky-xdg:/etc/xdg", init)
        self.assertIn("/run/systemd/user/wireplumber.service.d/51-luma-husky-audio.conf", init)

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
