# SPDX-License-Identifier: Apache-2.0
"""Depot's own failures (VM, screen, host capacity) are never findings against an app."""
import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from depot_review import capacity, checks, service  # noqa: E402
from depot_review.hub import Hub, HubError  # noqa: E402


def couldnt_run(reason="boot_timeout"):
    item = checks.infrastructure("vm:boot (x86_64)", reason)
    return {"status": checks.outcome([item]), "checks": [item]}


class Classify(unittest.TestCase):
    def test_infrastructure_failure_is_couldnt_run(self):
        self.assertEqual(couldnt_run()["status"], checks.COULDNT_RUN)

    def test_developer_finding_wins(self):
        out = [checks.infrastructure("vm:boot (x86_64)", "boot_timeout"),
               checks.check("runtime", False, "Use a supported runtime.")]
        self.assertEqual(checks.outcome(out), "needs_changes")

    def test_internal_problem_alone_is_attention(self):
        out = [checks.check("vm:launch (x86_64)", False, "Luma looks at this by hand.", internal=True)]
        self.assertEqual(checks.outcome(out), "attention")

    def test_infrastructure_beside_internal_is_couldnt_run(self):
        out = [checks.check("malware", False, "x", internal=True), checks.infrastructure("vm:launch", "screen_off")]
        self.assertEqual(checks.outcome(out), checks.COULDNT_RUN)

    def test_passed(self):
        self.assertEqual(checks.outcome([checks.check("runtime", True, "ok")]), "passed")

    def test_vm_steps_keep_the_infrastructure_flag(self):
        run = {"steps": [{"step": "boot", "ok": True, "message": "Luma started."},
                         {"step": "launch", "ok": False, "message": "The screen was off.", "internal": True,
                          "infrastructure": True, "reason": "screen_off"}]}
        out = service.vm_checks(run, "x86_64")
        self.assertNotIn("infrastructure", out[0])
        self.assertEqual((out[1]["key"], out[1]["reason"]), ("vm:launch (x86_64)", "screen_off"))
        self.assertEqual(checks.outcome(out), checks.COULDNT_RUN)

    def test_unreachable_hub_download_is_a_hub_error(self):
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "token"
            token.write_text("t")
            hub = Hub("https://hub.invalid", str(token), {})
            with self.assertRaises(HubError):
                hub.download("https://hub.invalid/x", Path(folder) / "x", limit=10)
            self.assertFalse((Path(folder) / "x.part").exists())

    def test_plumbing_errors(self):
        self.assertEqual(service.failure_report(HubError("GET x: timed out"))["status"], checks.COULDNT_RUN)
        self.assertEqual(service.failure_report(subprocess.TimeoutExpired("podman", 5400))["status"],
                         checks.COULDNT_RUN)
        no_room = service.failure_report(capacity.NoCapacity("memory", "no room"))
        self.assertEqual((no_room["status"], no_room["checks"][0]["reason"]), (checks.COULDNT_RUN, "memory"))
        # Anything else is a real problem for Luma to look at, not a retry.
        self.assertEqual(service.failure_report(KeyError("app_id"))["status"], "attention")


class Retries(unittest.TestCase):
    def run_retries(self, reports, delays=(300, 900, 2700)):
        slept, notes = [], []
        queue = list(reports)
        report, tries = service.with_retries(lambda: queue.pop(0), list(delays), sleep=slept.append,
                                             note=notes.append)
        return report, tries, slept, notes

    def test_recovers_after_backoff(self):
        report, tries, slept, notes = self.run_retries([couldnt_run(), couldnt_run("screen_off"),
                                                        {"status": "passed", "checks": []}])
        self.assertEqual((report["status"], tries, slept), ("passed", 3, [300, 900]))
        self.assertIn("screen_off", notes[1])

    def test_gives_up_after_the_last_delay(self):
        report, tries, slept, _ = self.run_retries([couldnt_run()] * 4)
        self.assertEqual((report["status"], tries, slept), (checks.COULDNT_RUN, 4, [300, 900, 2700]))

    def test_findings_are_not_retried(self):
        for status in ("passed", "needs_changes", "attention"):
            report, tries, slept, _ = self.run_retries([{"status": status, "checks": []}])
            self.assertEqual((report["status"], tries, slept), (status, 1, []))


class Slot(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.lock = os.path.join(self.dir.name, "review.lock")
        self.now = [0.0]

    def tearDown(self):
        self.dir.cleanup()

    def tick(self, seconds):
        self.now[0] += seconds

    def slot(self, available=lambda: 20000, busy=lambda: "", wait=60):
        return capacity.slot(self.lock, 10240, wait, poll=15, clock=lambda: self.now[0], sleep=self.tick,
                             available=available, busy=busy)

    def test_room_now(self):
        with self.slot():
            pass
        self.assertEqual(self.now[0], 0)

    def test_waits_for_memory(self):
        free = iter([2000, 2000, 12000])
        with self.slot(available=lambda: next(free)):
            pass
        self.assertEqual(self.now[0], 30)

    def test_no_memory_is_no_capacity(self):
        with self.assertRaises(capacity.NoCapacity) as raised:
            with self.slot(available=lambda: 2000):
                pass
        self.assertEqual(raised.exception.reason, "memory")

    def test_frozen_host_is_no_capacity(self):
        with self.assertRaises(capacity.NoCapacity) as raised:
            with self.slot(busy=lambda: "luma-heavy.slice is frozen by the thermal guard"):
                pass
        self.assertEqual(raised.exception.reason, "busy")

    def test_lane_held_by_another_vm(self):
        holder = os.open(self.lock, os.O_RDWR | os.O_CREAT)
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            with self.assertRaises(capacity.NoCapacity) as raised:
                with self.slot():
                    pass
            self.assertEqual(raised.exception.reason, "lock")
        finally:
            os.close(holder)
        with self.slot():  # free again once the other VM is done
            pass

    def test_loaded_host_is_busy(self):
        self.assertIn("load", capacity.host_busy(loadavg=lambda: (999.0, 0, 0)))

    def test_meminfo(self):
        path = os.path.join(self.dir.name, "meminfo")
        Path(path).write_text("MemTotal: 65000000 kB\nMemAvailable: 30408704 kB\n")
        self.assertEqual(capacity.mem_available_mib(path), 29696)


if __name__ == "__main__":
    unittest.main()
