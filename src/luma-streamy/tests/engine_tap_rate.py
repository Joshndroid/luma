#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""A canvas's tap runs at its fastest encode's rate, changed while the program runs: the engine with its
outputs fan-out attached (as the window does), a 30 fps recording of the vertical canvas, then a 60 fps
one beside it, then the 60 fps one ends. The tap goes 30, 60, 30 fps without stopping the program, the
30 fps recording keeps going through each change, and the 60 fps one (scaled to 480p) gets at least
as many pictures as the 30 fps one (more where the canvas composites faster than 45 fps: llvmpipe
builders manage about 30). Skipped (exit 0) where no H.264 and AAC encoder works.

    GST_PLUGIN_PATH=<lumaframe> python3 tests/engine_tap_rate.py [OUT_DIR]
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from luma_streamy import output_graph as og  # noqa: E402
from luma_streamy import outputs as outputs_module  # noqa: E402
from luma_streamy.engine import Engine  # noqa: E402
from luma_streamy.model import Project  # noqa: E402
from luma_streamy.output_graph import Preset  # noqa: E402
from luma_streamy.outputs import Outputs  # noqa: E402


def run(seconds: float) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


def tap_fps(engine: Engine, fmt: str) -> int:
    caps = engine.pipeline.get_by_name(f"tap-fps-{fmt}").get_property("caps")
    ok, num, den = caps.get_structure(0).get_fraction("framerate")
    return num // den if ok and den else 0


def main(out: str) -> int:
    working = outputs_module.working_encoders()
    if og.choose(working, "h264") is None or og.choose(working, "h264").audio is None:
        print("skipped: no working H.264 and AAC encoder here", flush=True)
        return 0
    project = Project.starter()
    scene = project.active_scene
    for w in list(scene.widgets):
        project.remove_widget(w.id)
    project.add_widget(scene.id, "display")
    engine = Engine(fake=True, preview=False)
    outputs = Outputs(working=working)
    engine.fanout = outputs
    taps = {"n": 0}
    push = outputs.push_video
    outputs.push_video = lambda fmt, sample: (taps.__setitem__("n", taps["n"] + 1), push(fmt, sample))[1]
    errors: list[str] = []
    outputs.connect("row-state", lambda *_a: engine.sync_taps())  # as the window does
    outputs.connect("problem", lambda _o, words: errors.append(words))
    engine.set_canvases(("vertical",))
    engine.start(project)
    run(1.5)
    outputs.start("thirty", Preset("vertical", "h264", 3000, 128), f"{out}/thirty.mkv", record=True)
    run(3.0)
    rates = [tap_fps(engine, "vertical")]
    frames = {"thirty": outputs.stats("thirty").frames}
    outputs.start("sixty", Preset("vertical", "h264", 3000, 128, size=480, fps=60), f"{out}/sixty.mkv", record=True)
    run(1.0)
    rates.append(tap_fps(engine, "vertical"))
    start = {k: outputs.stats(k).frames for k in ("thirty", "sixty")}
    tapped = taps["n"]
    began = time.monotonic()
    run(6.0)
    span = time.monotonic() - began
    rate = {k: (outputs.stats(k).frames - start[k]) / span for k in start}
    tap_rate = (taps["n"] - tapped) / span
    paced = outputs.rows["thirty"].group.paced
    outputs.stop("sixty")
    run(0.5)
    rates.append(tap_fps(engine, "vertical"))
    t = time.monotonic()
    while "sixty" in outputs.rows and time.monotonic() - t < 35:
        run(0.2)
    after = outputs.stats("thirty").frames
    run(2.0)
    still = outputs.stats("thirty").frames - after
    outputs.stop("thirty")
    t = time.monotonic()
    while outputs.rows and time.monotonic() - t < 40:
        run(0.2)
    assert engine.stop(wait=30), "the program did not stop"
    print(f"tap {rates} ({tap_rate:.1f} pictures/s handed over), pictures a second {rate}, the 30 fps encode paced {paced} away, "
          f"{still} pictures in the last 2 s, first {frames['thirty']}", flush=True)
    if rates != [30, 60, 30]:
        errors.append(f"the tap went {rates}, not 30, 60, 30")
    if rate["thirty"] < 3 or still < 3:
        errors.append(f"the 30 fps recording stalled across a tap change ({rate['thirty']:.1f}/s, then {still})")
    if rate["thirty"] > 33:
        errors.append(f"the 30 fps encode took {rate['thirty']:.1f} pictures a second")
    if rate["sixty"] < rate["thirty"] * 0.9 or (tap_rate > 45 and rate["sixty"] <= rate["thirty"] * 1.2):
        errors.append(f"the 60 fps encode got {rate['sixty']:.1f} pictures a second beside {rate['thirty']:.1f}")
    for error in errors:
        print(f"FAIL {error}", flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    Gst.init(None)
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp()))
