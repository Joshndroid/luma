# SPDX-License-Identifier: MPL-2.0
"""luma-charger and its generator against a fake sysfs and a fake clock."""
import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


def locate(name):
    # The package's %check has tests/ beside charger/; the repository keeps it in src/.
    for path in (HERE.parent / "charger" / name, HERE.parents[1] / "src" / "luma-vitals" / "charger" / name):
        if path.is_file():
            return path
    raise FileNotFoundError(name)


CHARGER = locate("luma-charger")
GENERATOR = locate("luma-charger-generator")


def load():
    loader = importlib.machinery.SourceFileLoader("luma_charger", str(CHARGER))
    spec = importlib.util.spec_from_loader("luma_charger", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


charger = load()
# Journal lines on stdout would land between a test's name and its "ok".
charger.log = lambda *args, **kwargs: None


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Sysfs:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="charger-sysfs-"))

    def put(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value + "\n")

    def get(self, relative):
        return (self.root / relative).read_text().strip()

    def supply(self, name, **values):
        for key, value in values.items():
            self.put(f"class/power_supply/{name}/{key}", str(value))


def phone():
    """A Fairphone 6 as the charger boot finds it."""
    fs = Sysfs()
    fs.supply("qcom-battmgr-bat", type="Battery", present=1, capacity=3, status="Charging")
    fs.supply("qcom-battmgr-usb", type="USB", online=1)
    fs.supply("qcom-battmgr-wls", type="Wireless", online=0)
    fs.supply("pen", type="Battery", scope="Device", present=1, capacity=90, status="Discharging")
    for index, name in enumerate(("adsp", "modem", "wpss", "cdsp")):
        fs.put(f"class/remoteproc/remoteproc{index}/name", name)
        fs.put(f"class/remoteproc/remoteproc{index}/state", "running")
    fs.put("class/graphics/fb0/blank", "0")
    return fs


class GeneratorTest(unittest.TestCase):
    def run_generator(self, cmdline):
        dirs = [tempfile.mkdtemp(prefix="gen-") for _ in range(3)]
        subprocess.run(["sh", str(GENERATOR), *dirs], check=True,
                       env=dict(os.environ, SYSTEMD_PROC_CMDLINE=cmdline))
        link = Path(dirs[1]) / "default.target"
        return os.readlink(link) if link.is_symlink() else None

    def test_charger_boot_defaults_to_the_charger_target(self):
        self.assertEqual(self.run_generator("quiet splash androidboot.mode=charger androidboot.slot_suffix=_a"),
                         "/usr/lib/systemd/system/luma-charger.target")

    def test_other_boots_are_untouched(self):
        for cmdline in ("quiet splash androidboot.mode=normal", "quiet",
                        "androidboot.mode=charger luma.charger=0",
                        "androidboot.mode=charger systemd.unit=rescue.target",
                        "androidboot.mode=charger emergency"):
            with self.subTest(cmdline=cmdline):
                self.assertIsNone(self.run_generator(cmdline))


class SuppliesTest(unittest.TestCase):
    def test_reads_the_system_battery_and_the_charger(self):
        fs = phone()
        supplies = charger.Supplies(fs.root)
        self.assertEqual(supplies.battery(), (3, "Charging"))
        self.assertTrue(supplies.external())
        fs.supply("qcom-battmgr-usb", online=0)
        self.assertFalse(supplies.external())

    def test_no_battery(self):
        fs = Sysfs()
        fs.supply("AC", type="Mains", online=1)
        self.assertIsNone(charger.Supplies(fs.root).battery())

    def test_messages(self):
        self.assertEqual(charger.message((37, "Charging")), "Charging · 37%")
        self.assertEqual(charger.message((100, "Full")), "Charged · 100%")
        self.assertEqual(charger.message((80, "Not charging")), "80%")
        self.assertEqual(charger.message((1, "Charging"), too_low=True), "1% · Charge a little longer to start")
        self.assertEqual(charger.message(None), "Charging")

    def test_power_key_bitmap(self):
        # pmic_pwrkey on the FP6: KEY_POWER (116) only, one 64-bit word.
        self.assertTrue(charger.has_key("10000000000000 0", 116, 64))
        self.assertFalse(charger.has_key("10000000000000 0", 115, 64))
        self.assertFalse(charger.has_key("0", 116, 64))
        self.assertTrue(charger.has_key("100000 0 0 0", 116, 32))


class ChargerTest(unittest.TestCase):
    def setUp(self):
        self.fs = phone()
        self.clock = Clock()
        self.calls = []
        self.real_systemctl = charger.systemctl
        charger.systemctl = lambda *args: self.calls.append(args)
        self.charger = charger.Charger(self.fs.root, clock=self.clock, record=self.fs.root / "stopped")
        self.charger.screen.plymouth = None

    def tearDown(self):
        charger.systemctl = self.real_systemctl

    def advance(self, seconds):
        self.clock.now += seconds
        return self.charger.tick()

    def test_stops_idle_processors_but_never_the_charger_dsp(self):
        self.charger.processors.stop_idle()
        states = {self.fs.get(f"class/remoteproc/remoteproc{i}/name"): self.fs.get(f"class/remoteproc/remoteproc{i}/state")
                  for i in range(4)}
        self.assertEqual(states, {"adsp": "running", "modem": "stop", "wpss": "stop", "cdsp": "stop"})
        self.charger.processors.restart()
        self.assertEqual(self.fs.get("class/remoteproc/remoteproc1/state"), "start")

    def test_a_restarted_service_still_restarts_what_was_stopped(self):
        self.charger.processors.stop_idle()
        again = charger.Charger(self.fs.root, clock=self.clock, record=self.fs.root / "stopped")
        again.processors.stop_idle()
        again.processors.restart()
        for index in (1, 2, 3):
            self.assertEqual(self.fs.get(f"class/remoteproc/remoteproc{index}/state"), "start")

    def test_a_processor_that_comes_up_later_is_stopped_too(self):
        self.fs.put("class/remoteproc/remoteproc1/state", "offline")  # still booting
        self.charger.tick()
        self.assertEqual(self.fs.get("class/remoteproc/remoteproc1/state"), "offline")
        self.fs.put("class/remoteproc/remoteproc1/state", "running")
        self.advance(1)
        self.assertEqual(self.fs.get("class/remoteproc/remoteproc1/state"), "stop")
        self.charger.processors.restart()
        self.assertEqual(self.fs.get("class/remoteproc/remoteproc1/state"), "start")

    def test_level_then_display_off(self):
        self.charger.show()
        self.assertIsNone(self.advance(charger.SHOW_SECONDS - 0.1))
        self.assertEqual(self.fs.get("class/graphics/fb0/blank"), "0")
        self.assertIsNone(self.advance(0.2))
        self.assertEqual(self.fs.get("class/graphics/fb0/blank"), "4")
        self.charger.key(True)  # a press lights it again
        self.assertEqual(self.fs.get("class/graphics/fb0/blank"), "0")
        self.charger.key(False)
        self.assertIsNone(self.advance(charger.SHOW_SECONDS + 0.1))
        self.assertEqual(self.fs.get("class/graphics/fb0/blank"), "4")

    def test_holding_the_key_starts_the_phone(self):
        self.fs.supply("qcom-battmgr-bat", capacity=charger.START_MIN_PERCENT)
        self.charger.key(True)
        self.assertIsNone(self.advance(charger.LONG_PRESS_SECONDS - 0.1))
        self.assertEqual(self.advance(0.2), "start")

    def test_a_short_press_does_not(self):
        self.fs.supply("qcom-battmgr-bat", capacity=50)
        self.charger.key(True)
        self.advance(0.5)
        self.charger.key(False)
        self.assertIsNone(self.advance(charger.LONG_PRESS_SECONDS))

    def test_too_low_to_start(self):
        self.fs.supply("qcom-battmgr-bat", capacity=charger.START_MIN_PERCENT - 1)
        self.charger.key(True)
        self.assertIsNone(self.advance(charger.LONG_PRESS_SECONDS + 0.1))
        self.assertIsNone(self.advance(charger.LONG_PRESS_SECONDS + 0.1))

    def test_charger_removed_powers_off_after_ten_seconds(self):
        self.fs.supply("qcom-battmgr-usb", online=0)
        self.assertIsNone(self.charger.tick())
        self.assertIsNone(self.advance(charger.UNPLUGGED_SECONDS - 1))
        self.fs.supply("qcom-battmgr-usb", online=1)  # a renegotiation, not a removal
        self.assertIsNone(self.advance(0.5))
        self.fs.supply("qcom-battmgr-usb", online=0)
        self.assertIsNone(self.advance(0.5))
        self.assertIsNone(self.advance(charger.UNPLUGGED_SECONDS - 1))
        self.assertEqual(self.advance(1.1), "off")

    def test_flat_on_a_charger_that_cannot_keep_up_powers_off(self):
        self.fs.supply("qcom-battmgr-bat", capacity=2, status="Discharging")
        self.assertIsNone(self.charger.tick())
        self.assertIsNone(self.advance(charger.FLOOR_SECONDS - 1))
        self.fs.supply("qcom-battmgr-bat", status="Charging")  # it caught up
        self.assertIsNone(self.advance(2))
        self.fs.supply("qcom-battmgr-bat", status="Discharging")
        self.assertIsNone(self.advance(1))
        self.assertEqual(self.advance(charger.FLOOR_SECONDS), "empty")

    def test_start_restarts_processors_and_isolates_the_default(self):
        self.charger.processors.stop_idle()
        self.charger.start_phone("test")
        self.assertEqual(self.fs.get("class/remoteproc/remoteproc1/state"), "start")
        self.assertEqual(self.calls[-1][0], "isolate")
        self.assertNotEqual(self.calls[-1][1], "luma-charger.target")


if __name__ == "__main__":
    unittest.main()
