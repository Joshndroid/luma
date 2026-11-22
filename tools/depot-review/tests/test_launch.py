# SPDX-License-Identifier: Apache-2.0
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from depot_review import vm  # noqa: E402

W, H = 160, 100


def frame(boxes=()):
    """A dark frame with white boxes (x0, y0, x1, y1) in fractions of the frame."""
    px = bytearray(W * H * 3)
    for x0, y0, x1, y1 in boxes:
        for y in range(int(y0 * H), int(y1 * H)):
            for x in range(int(x0 * W), int(x1 * W)):
                i = (y * W + x) * 3
                px[i:i + 3] = b"\xff\xff\xff"
    return bytes(px)


class Launch(unittest.TestCase):
    def test_dock_and_top_bar_changes_are_not_a_window(self):
        base = frame()
        shell = frame([(0.1, 0.9, 0.9, 1.0), (0.4, 0.0, 0.6, 0.04)])
        self.assertLess(vm.changed_fraction(base, shell, W, H), vm.WINDOW_FRACTION)
        self.assertGreater(vm.changed_fraction(base, shell), vm.WINDOW_FRACTION)

    def test_window_in_the_app_area_counts(self):
        self.assertGreater(vm.changed_fraction(frame(), frame([(0.2, 0.15, 0.8, 0.75)]), W, H), 0.2)

    def test_blank_screen_is_detected(self):
        self.assertTrue(vm.is_blank(frame()))
        self.assertFalse(vm.is_blank(frame([(0.0, 0.0, 1.0, 0.5)])))

    def test_window_must_leave_when_the_app_stops(self):
        self.assertTrue(vm.window_verdict(0.35, 0.0))
        self.assertTrue(vm.window_verdict(0.35, 0.005))
        self.assertFalse(vm.window_verdict(0.35, 0.34))  # something else changed the screen
        self.assertFalse(vm.window_verdict(0.01, 0.0))   # nothing really appeared

    def test_unit_state_probe(self):
        self.assertEqual(vm.parse_unit_state(0, "active\n"), "active")
        self.assertEqual(vm.parse_unit_state(3, "inactive\n"), "inactive")
        self.assertEqual(vm.parse_unit_state(3, "failed"), "failed")
        # A probe that timed out or lost SSH is not the app closing.
        self.assertEqual(vm.parse_unit_state(124, ""), "unknown")
        self.assertEqual(vm.parse_unit_state(255, ""), "unknown")
        self.assertEqual(vm.parse_unit_state(1, "Failed to connect to bus: No medium found"), "unknown")

    def test_control_app_decides_who_a_failed_launch_is_about(self):
        self.assertEqual(vm.judge_launch(False), "infrastructure")  # the control failed too: the VM
        self.assertEqual(vm.judge_launch(True), "app")              # the control opened a window
        self.assertEqual(vm.judge_launch(None), "app")              # no control: the finding stands

    def test_android_stays_off_in_the_review_vm(self):
        self.assertIn("waydroid-container.service", vm.REVIEW_MASKS)
        self.assertIn("luma-android-initialize.service", vm.REVIEW_MASKS)
        self.assertIn("luma-android-prewarm.timer", vm.REVIEW_USER_MASKS)
        run = vm.android_off()
        self.assertIn("systemctl mask --runtime --now", run)
        self.assertIn("systemctl --global mask --runtime luma-android-prewarm", run)
        self.assertNotIn("--runtime", vm.android_off(runtime=False))

    def test_prepare_base_refuses(self):
        self.assertEqual(vm.prepare_refusal(False, "", False), "")
        self.assertIn("already prepared", vm.prepare_refusal(True, "", False))
        self.assertEqual(vm.prepare_refusal(True, "", True), "")
        self.assertIn("open by other processes", vm.prepare_refusal(False, "794438", True))

    def test_fresh_base_masks_android_from_the_first_boot(self):
        ks = vm.kickstart("ssh-ed25519 AAAA test", "ttyS0")
        self.assertIn("systemctl mask luma-android-initialize.service waydroid-container.service", ks)
        self.assertIn("systemctl --global mask luma-android-prewarm.timer", ks)

    def test_hung_first_run_ldconfig(self):
        self.assertTrue(vm.stuck_in_ldconfig("flatpak\nbwrap\nbwrap\nldconfig\n"))
        self.assertFalse(vm.stuck_in_ldconfig("flatpak\nbwrap\nearshot\n"))
        self.assertFalse(vm.stuck_in_ldconfig("flatpak\nbwrap\nldconfig\nearshot\n"))
        self.assertFalse(vm.stuck_in_ldconfig(""))

    def test_emergency_mode_is_seen(self):
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            serial = Path(folder) / "serial.log"
            self.assertFalse(vm.in_emergency(serial))
            serial.write_text("[ TIME ] Timed out waiting for device\nYou are in emergency mode. After")
            self.assertTrue(vm.in_emergency(serial))

    def test_qmp_socket_fits_af_unix(self):
        short = Path("/srv/jobs/run-x")
        self.assertEqual(vm.qmp_path(short), short / "qmp.sock")
        deep = Path("/srv/dev-storage/depot-review/work/jobs/recheck-sub_va4DOdlqJIxg-20261122T123356/attempt-1/"
                    "vm-x86_64/run-_zghsieq")
        self.assertLess(len(str(vm.qmp_path(deep))), 108)
        self.assertNotEqual(vm.qmp_path(deep), vm.qmp_path(deep.with_name("run-other")))


if __name__ == "__main__":
    unittest.main()
