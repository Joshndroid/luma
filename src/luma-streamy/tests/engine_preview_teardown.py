#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""A preview leaving the program (a canvas no longer shown, the program closing) lets GTK's paintable
sink go on the main thread. Dropped on another one (the close thread, a pool stop) it aborts the
process: "Value dropped on a different thread than where it was created".

    xvfb-run env GDK_BACKEND=x11 GST_PLUGIN_PATH=<lumaframe> python3 tests/engine_preview_teardown.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gst, Gtk  # noqa: E402

from luma_streamy.engine import Engine  # noqa: E402
from luma_streamy.model import Project  # noqa: E402


def run(seconds: float) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


def main() -> int:
    project = Project.starter()
    project.add_widget(project.active_scene.id, "display")
    engine = Engine(fake=True, preview=True)
    engine.set_canvases(("landscape", "vertical"))
    engine.start(project)
    run(2)
    assert set(engine.programs) == {"landscape", "vertical"}, engine.programs
    gone: dict[str, str] = {}
    for fmt, program in engine.programs.items():
        sink = program.preview[-1].get_property("sink")
        sink.weak_ref(lambda fmt=fmt: gone.setdefault(fmt, threading.current_thread().name))
        del sink
    engine.set_canvases(("landscape",))  # Vertical's program goes on the pool
    run(1)
    assert engine.stop(wait=10), "the program did not stop"  # Landscape's goes with the close thread
    for _ in range(40):
        if len(gone) == 2:
            break
        run(0.25)
    print("preview sinks finalized on", gone)
    assert gone == {"landscape": "MainThread", "vertical": "MainThread"}, gone
    return 0


if __name__ == "__main__":
    Gst.init(None)
    Gtk.init()
    sys.exit(main())
