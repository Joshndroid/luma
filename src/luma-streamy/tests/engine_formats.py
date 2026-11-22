#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Every capture kind reaches a first picture on the program, in every format its source may send:
a display (XR24/AR24 = BGRx/BGRA), a window (AR24/XR24), a camera (MJPEG, YUY2, NV12). Each runs
past the point a negotiation failure would stop it, and again after its branch reopens.

    GST_PLUGIN_PATH=<lumaframe> python3 tests/engine_formats.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from luma_streamy.engine import FADE_MS, Engine  # noqa: E402
from luma_streamy.model import Project  # noqa: E402

FAST = bool(os.environ.get("STREAMY_FORMATS_FAST") or os.environ.get("STREAMY_FORMATS_BESIDE"))
BIG = "width=3440,height=1440" if not FAST else "width=1720,height=720"
CASES = [
    ("display", f"video/x-raw,format=BGRx,{BIG},framerate=30/1"),
    ("display", f"video/x-raw,format=BGRA,{BIG},framerate=30/1"),
    ("window", "video/x-raw,format=BGRA,width=1280,height=800,framerate=30/1"),
    ("window", "video/x-raw,format=BGRx,width=1280,height=800,framerate=30/1"),
    ("camera", "image/jpeg,width=1280,height=720,framerate=30/1"),
    ("camera", "video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1"),
    ("camera", "video/x-raw,format=NV12,width=1920,height=1080,framerate=30/1"),
]
RUN_S = 3.5  # past the Dell's 2.5 s failure after the first picture
FIRST_S = 15.0  # the first picture of a big cast on software GL on a loaded builder


def run(seconds: float) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


def composited(engine: Engine, widget_id: str) -> int:
    """How many program frames composited the widget's own picture within 5 s (its layer's mixer pad
    had a buffer when the mixer chose its inputs)."""
    layer = engine.branches[widget_id].layers["landscape"]
    pad = layer.frame.get_static_pad("src").get_peer()
    mixer = engine.pipeline.get_by_name("mixer-landscape")
    count = [0]

    def selected(agg, *_args):
        if agg.peek_next_sample(pad) is not None:
            count[0] += 1

    handler = mixer.connect("samples-selected", selected)
    for _ in range(50):
        mixer.set_property("emit-signals", True)  # the engine turns it on only while a change settles
        run(0.1)
        if count[0] >= 5:
            break
    mixer.disconnect(handler)
    mixer.set_property("emit-signals", False)
    return count[0]


def check(kind: str, caps: str) -> list[str]:
    project = Project.starter()
    scene = project.active_scene
    for w in list(scene.widgets):
        project.remove_widget(w.id)
    source = {"fake-caps": caps}
    if kind == "camera" and "width=" in caps:
        size = dict(p.split("=") for p in caps.split(",")[1:] if "=" in p)
        source["mode"] = [int(size["width"]), int(size["height"]), int(size["framerate"].split("/")[0])]
    if os.environ.get("STREAMY_FORMATS_BESIDE"):
        # An MJPEG camera already in the program (its decoder completes its own state change).
        cam = project.add_widget(scene.id, "camera", source={"fake-caps": "image/jpeg,width=320,height=180,framerate=30/1",
                                                            "mode": [320, 180, 30]})
        project.set_rect(cam.id, 0, 0, 320, 180)
    widget = project.add_widget(scene.id, kind, source=source)
    project.set_rect(widget.id, 0, 0, 1920, 1080)
    engine = Engine(fake=True, preview=False)
    engine.view_of = lambda w, fmt: project.view_on(w, fmt)
    failed: list[str] = []
    engine.connect("widget-failed", lambda _e, wid, reason: failed.append(reason))
    errors = []
    engine.start(project)
    for attempt in ("first", "reopened"):
        for _ in range(int(FIRST_S * 10)):  # its first picture, however slow the GL
            branch = engine.branches.get(widget.id)
            if failed or (branch is not None and branch.shown):
                break
            run(0.1)
        run(RUN_S)  # then on past the point a negotiation failure would stop it
        branch = engine.branches.get(widget.id)
        if failed:
            errors.append(f"{attempt}: failed ({failed})")
            break
        if branch is None or not branch.shown:
            errors.append(f"{attempt}: no first picture")
            break
        for _ in range(50):  # faded in (its opacity steps on the main loop)
            if branch.fade >= FADE_MS:
                break
            run(0.1)
        frames = composited(engine, widget.id)
        if frames < 5:
            errors.append(f"{attempt}: {frames} program frames composited its picture")
        if attempt == "first":
            # The branch closes and opens again (a scene switch back, a share resumed).
            engine.show_scene(scene, skip={widget.id})
            run(0.5)
            engine.show_scene(scene)
    assert engine.stop(wait=10), "the program did not stop"
    return errors


def main() -> int:
    Gst.init(None)
    only = os.environ.get("STREAMY_FORMATS_ONLY")
    bad = 0
    for kind, caps in CASES:
        if only and only not in f"{kind} {caps}":
            continue
        errors = check(kind, caps)
        label = f"{kind:8} {caps.split(',')[0] if caps.startswith('image') else caps.split(',')[1]}"
        print(("ok   " if not errors else "FAIL ") + label + ("" if not errors else ": " + "; ".join(errors)), flush=True)
        bad += bool(errors)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
