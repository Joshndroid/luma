# SPDX-License-Identifier: Apache-2.0
"""The main loop's watchdog: when the UI thread stops answering for more than a second, the journal gets
where it is stuck (its Python stack) and how long it was gone, so a freeze leaves a trace."""

from __future__ import annotations

import sys
import threading
import time
import traceback

from gi.repository import GLib

#: The main loop answers every BEAT_S; a gap past STUCK_S is a freeze.
BEAT_S = 0.25
STUCK_S = 1.0


def log(text: str) -> None:
    print(f"streamy-watchdog: {text}", file=sys.stderr, flush=True)


class Watchdog:
    def __init__(self) -> None:
        self.main = threading.main_thread().ident
        self.last = time.monotonic()
        self.stuck = False
        GLib.timeout_add(int(BEAT_S * 1000), self._beat)
        threading.Thread(target=self._watch, name="streamy-watchdog", daemon=True).start()

    def _beat(self) -> bool:
        now = time.monotonic()
        if self.stuck:
            self.stuck = False
            log(f"the main loop answered again after {now - self.last:.1f} s")
        self.last = now
        return True

    def _watch(self) -> None:
        next_dump = 0.0
        while True:
            time.sleep(BEAT_S)
            now = time.monotonic()
            gap = now - self.last
            if gap < STUCK_S:
                continue
            if not self.stuck:
                self.stuck, next_dump = True, now
            if now >= next_dump:  # its stack at 1 s, then every 5 s while it stays stuck
                next_dump = now + 5
                frame = sys._current_frames().get(self.main)
                stack = "".join(traceback.format_stack(frame)[-12:]) if frame else "(no frame)"
                log(f"the main loop has not answered for {gap:.1f} s; it is in:\n{stack}")


def start() -> Watchdog:
    return Watchdog()
