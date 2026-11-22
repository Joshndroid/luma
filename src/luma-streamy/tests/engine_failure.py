#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""A widget whose source fails, or never answers, comes out of the program with a reason,
and the main loop never waits on it.

    GST_PLUGIN_PATH=<lumaframe> python3 tests/engine_failure.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from luma_streamy import engine as engine_module  # noqa: E402
from luma_streamy.engine import Engine  # noqa: E402
from luma_streamy.model import Project  # noqa: E402


def main() -> int:
    engine_module.FIRST_PICTURE_S = 2
    folder = tempfile.mkdtemp(prefix="streamy-failure-")
    stuck = os.path.join(folder, "stuck")
    os.mkfifo(stuck)  # opening it blocks until someone writes: a source that never answers

    project = Project.starter()
    scene = project.active_scene
    good = project.add_widget(scene.id, "display")
    missing = project.add_widget(scene.id, "camera", source={"fake-image": os.path.join(folder, "gone.png")})
    hung = project.add_widget(scene.id, "window", source={"fake-image": stuck})

    engine = Engine(fake=True, preview=False)
    failed: dict[str, str] = {}
    waiting: set[str] = set()
    engine.connect("widget-failed", lambda _e, wid, reason: failed.setdefault(wid, reason))
    engine.connect("widget-waiting", lambda _e, wid: waiting.add(wid))
    gaps = []
    last = [time.monotonic()]

    def tick() -> bool:
        now = time.monotonic()
        gaps.append(now - last[0])
        last[0] = now
        return True

    GLib.timeout_add(50, tick)
    engine.start(project)
    loop = GLib.MainLoop()
    GLib.timeout_add(4500, loop.quit)
    loop.run()
    print("failed", failed, "branches", sorted(engine.branches), "longest stall", round(max(gaps), 3))
    assert missing.id in failed and failed[missing.id] == "missing", failed
    # No picture yet is not a failure: the stream stays open, the window hears it is waiting.
    assert hung.id not in failed and hung.id in waiting and hung.id in engine.branches, (failed, waiting)
    assert good.id in engine.branches and good.id not in failed
    # 1 s: shared two-core builders pause a process for half a second on their own; a source
    # waited on by the main loop (the fault this catches) holds it for seconds.
    assert max(gaps) < 1.0, f"the main loop stalled for {max(gaps):.2f} s"
    # Stop everything without waiting on the stuck source, then let it go: each time a reader has the pipe
    # open (or is blocked opening it), a writer opens and closes at once, so its open returns and its read
    # ends. The whole program is stopped before the next one starts.
    t = time.monotonic()
    engine.stop()
    assert time.monotonic() - t < 1.0
    while not engine.wait_stopped(0.1):
        try:
            os.close(os.open(stuck, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:  # ENXIO: no reader yet
            pass
        assert time.monotonic() - t < 10, "the program did not stop"

    # Nothing to show and nothing sent: no pipeline at all. A widget added later starts a fresh
    # program, and its pictures keep reaching the mixer (the timing of a program opened late is right:
    # wrong running times make the mixer drop every picture as late, or hold them, after the first).
    empty = Project.starter()
    idle = Engine(fake=True, preview=False)
    idle.start(empty)
    assert idle.pipeline is None, "an empty scene has no pipeline"
    late = empty.add_widget(empty.active_scene.id, "display")
    pictures: list[float] = []  # when each picture left the layer for the mixer (any thread)

    def add() -> bool:
        idle.show_scene(empty.active_scene)
        frame = idle.branches[late.id].frame("landscape")
        frame.get_static_pad("src").add_probe(
            Gst.PadProbeType.BUFFER, lambda *_a: (pictures.append(time.monotonic()), Gst.PadProbeReturn.OK)[1])
        return False

    # The source sends 30 a second. Proven here: the first picture comes (software GL on a loaded
    # builder: within FIRST_S), then pictures keep coming for WINDOW_S at a rate no late-timed
    # program reaches (MIN_RATE, a tenth of the source's) and still come at its end (no gap over 1 s).
    FIRST_S, WINDOW_S, MIN_RATE = 10.0, 3.0, 3.0
    GLib.timeout_add(1000, add)
    started = time.monotonic()
    while not pictures and time.monotonic() - started < 1.0 + FIRST_S:
        run(0.1)
    assert pictures, f"no picture reached the mixer within {FIRST_S:g} s"
    run(WINDOW_S)
    first = pictures[0]
    window = [t for t in pictures if t - first <= WINDOW_S]
    rate = (len(window) - 1) / WINDOW_S
    gap = time.monotonic() - pictures[-1]
    print(f"late widget: first picture after {first - started - 1.0:.2f} s, then {rate:.1f}/s, last {gap:.2f} s ago")
    assert rate >= MIN_RATE, f"only {len(window)} pictures reached the mixer in {WINDOW_S:g} s"
    assert gap < 1.0, f"pictures stopped reaching the mixer {gap:.2f} s ago"
    empty.remove_widget(late.id)
    idle.show_scene(empty.active_scene)
    run(0.5)
    assert idle.pipeline is None, "the last widget gone, the pipeline goes"
    assert idle.stop(wait=10), "the program did not stop"
    return 0


def run(seconds: float) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


if __name__ == "__main__":
    Gst.init(None)
    sys.exit(main())
