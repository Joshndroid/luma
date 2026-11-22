#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""A resize (or a Look change) is composited on the canvas's next frame: the redrawn picture replaces the
older one still waiting at the mixer pad, so the mixer never composites a frame of the old revision
after the new one arrived.

    GST_PLUGIN_PATH=<lumaframe> python3 tests/engine_resize_frame.py
"""
import os
import statistics
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstBase", "1.0")
from gi.repository import GLib, Gst, GstBase  # noqa: E402,F401

from luma_streamy.engine import Engine  # noqa: E402
from luma_streamy.model import Project  # noqa: E402

STEPS = int(os.environ.get("N_STEPS", "30"))


def run(seconds: float) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


def main() -> int:
    Gst.init(None)
    project = Project.starter()
    scene = project.active_scene
    for w in list(scene.widgets):
        project.remove_widget(w.id)
    widget = project.add_widget(scene.id, "camera", source={"fake-caps": "video/x-raw,format=NV12,width=640,height=360,"
                                                                          "framerate=30/1", "mode": [640, 360, 30]})
    project.set_rect(widget.id, 100, 100, 640, 360)
    engine = Engine(fake=True, preview=False)
    engine.view_of = lambda w, fmt: project.view_on(w, fmt)
    engine.set_canvases(("landscape",))
    engine.start(project)
    run(2.0)
    branch = engine.branches[widget.id]
    assert branch.shown, "no first picture"
    layer = branch.layers["landscape"]
    pad = layer.frame.get_static_pad("src").get_peer()
    mixer = engine.pipeline.get_by_name("mixer-landscape")
    lock = threading.Lock()
    state = {"target": None, "arrived": None, "frames": None, "late": []}

    def arrived(_pad, info, _data):
        buffer = info.get_buffer()
        with lock:
            if state.get("armed"):
                # A source picture just reached the pad: the resize commits while it waits there.
                state["armed"] = False
                GLib.idle_add(commit)
            if state["target"] is not None and state["arrived"] is None and buffer.offset >= state["target"]:
                state["arrived"] = state["frames"]
        return Gst.PadProbeReturn.OK

    def selected(agg, *_args):
        sample = agg.peek_next_sample(pad)
        buffer = sample.get_buffer() if sample is not None else None
        with lock:

            if state["target"] is None or buffer is None:
                return
            state["frames"] += 1
            if state["arrived"] is not None and buffer.offset >= state["target"]:
                # Mixer frames composited after the new revision reached the pad, before it was used.
                state["late"].append(state["frames"] - state["arrived"] - 1)
                state["target"] = None

    pad.add_probe(Gst.PadProbeType.BUFFER, arrived, None)
    mixer.connect("samples-selected", selected)
    def commit() -> bool:
        width = 640 + (state["step"] % 2) * 40
        project.set_rect(widget.id, 100, 100, width, width * 9 / 16)
        with lock:  # the revision this resize commits (Engine._revision counts them all)
            state.update(target=engine._revision + 1, arrived=None, frames=0)
        engine.update_widget(widget)
        return False

    for step in range(STEPS):
        with lock:
            state["step"], state["armed"] = step, True
        run(0.2)
    assert engine.stop(wait=10), "the program did not stop"
    late = state["late"]
    print(f"{len(late)} of {STEPS} resizes composited; frames late after arriving: "
          f"p50 {statistics.median(late) if late else '-'}, max {max(late) if late else '-'}, "
          f"{sum(1 for n in late if n > 0)} late", flush=True)
    assert len(late) >= STEPS * 0.8, "resizes never composited"
    assert sum(1 for n in late if n > 0) <= 1, f"an older picture went first: {late}"
    return 0


if __name__ == "__main__":
    sys.exit(main())
