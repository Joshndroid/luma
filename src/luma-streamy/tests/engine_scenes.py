#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Scene changes: every scene's sources stay open, so a change opens nothing and shows no card.

- A switch between scenes that share a camera keeps its one capture (no reopen, no "Connecting").
- A cut to a scene whose display is warm shows its picture on the first frame the outputs get.
- A fade brings the incoming scene's picture to full alpha in the set duration, on the output tap.
- A camera in both scenes glides from its old box to its new one: its two layers move together.

    GST_PLUGIN_PATH=<lumaframe> STREAMY_FAKE_SOURCES=1 python3 tests/engine_scenes.py
"""
import os
import sys
import time

os.environ.setdefault("STREAMY_FAKE_SOURCES", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo  # noqa: E402

from luma_streamy.engine import Engine  # noqa: E402
from luma_streamy.model import Project  # noqa: E402

FRAME = Gst.SECOND // 30  # the output tap's rate
BG = 16  # NV12 luma of the empty (black) canvas


def run(seconds: float) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


def until(check, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        run(0.05)
    return check()


class Fanout:
    """Stands in for the outputs: takes the landscape tap's pictures (NV12, what the encoders get) and
    keeps each one's time and the luma at a few points."""

    def __init__(self, points: dict) -> None:
        self.points, self.frames = points, []

    def wants(self):
        return ["landscape"]

    def push_sound(self, _sample) -> None:
        pass

    def push_video(self, _fmt, sample) -> None:
        buffer = sample.get_buffer()
        info = GstVideo.VideoInfo.new_from_caps(sample.get_caps())
        stride = info.stride[0]
        ok, mapped = buffer.map(Gst.MapFlags.READ)
        if not ok:
            return
        try:
            seen = {name: mapped.data[y * stride + x] for name, (x, y) in self.points.items()}
        finally:
            buffer.unmap(mapped)
        self.frames.append((buffer.pts, seen))

    def after(self, start: int) -> list:
        return [(pts, seen) for pts, seen in list(self.frames) if pts >= start]

    def before(self, start: int) -> list:
        return [(pts, seen) for pts, seen in list(self.frames) if pts < start]


def dump(engine) -> str:
    """Each layer's state, for a failure's message."""
    rows = []
    for wid, b in engine.branches.items():
        for fmt, layer in b.layers.items():
            queue = layer.elements[1].get_static_pad("sink")
            rows.append(f"{wid}/{fmt}: scene {b.scene_id} fade {b.fade} linked {layer.linked} valve-drop "
                        f"{layer.valve.get_property('drop')} queue-caps {queue.get_current_caps() is not None} "
                        f"frame-caps {layer.frame.get_static_pad('src').get_current_caps() is not None} alpha "
                        f"{layer.pad.get_property('alpha'):.2f} state {layer.frame.get_state(0)[1].value_nick} "
                        f"feed-shown {b.feed.shown} parked {b.feed.parked} tee-peer {layer.tee_pad.get_peer() is not None}")
    return "\n".join(rows)


def main() -> int:
    project = Project.starter()
    one = project.active_scene
    two = project.add_scene("Two")
    camera = project.add_widget(one.id, "camera", source={
        "camera": "fake-cam", "fake-caps": "video/x-raw,format=YUY2,width=640,height=360,framerate=30/1"})
    camera.x, camera.y, camera.width, camera.height = 1400, 700, 480, 270
    camera.effects.shadow_blur = 0  # no margin: its pad is its box
    camera.effects.stroke_width = 0
    display = project.add_widget(two.id, "display")
    display.x, display.y, display.width, display.height = 0, 0, 960, 540
    side = project.add_widget(two.id, "camera", source={
        "camera": "side-cam", "fake-caps": "image/jpeg,width=640,height=360,framerate=30/1"})
    side.x, side.y, side.width, side.height = 0, 600, 640, 360  # in Two only: parked while One shows
    twin = project.paste_widget(two.id, camera)  # linked: the same camera, its own place
    twin.x, twin.y, twin.width, twin.height = 960, 540, 960, 540
    assert twin.link == camera.link

    points = {"display": (480, 270), "camera-small": (1640, 835), "camera-big": (1100, 650), "side": (320, 780)}
    fanout = Fanout(points)
    engine = Engine(fake=True, preview=False)
    engine.scenes_of = lambda: project.scenes
    engine.transition = ("cut", 300)
    opened = []
    open_feed = engine._open_feed

    def counted(widget, key):
        opened.append(key)
        return open_feed(widget, key)

    engine._open_feed = counted
    engine.start(project)
    engine.fanout = fanout
    engine.sync_taps()
    assert until(lambda: all(f.shown for f in engine.feeds.values()) and len(engine.feeds) == 3, 15), \
        f"sources not warm: {[(k, f.shown) for k, f in engine.feeds.items()]}"
    print("warm:", sorted(engine.feeds), "opened", len(opened))
    assert len(opened) == 3, opened  # the camera once for both scenes, the display and the side camera once
    parked = engine.branches[side.id].feed
    assert until(lambda: parked.parked and parked.park.get_property("drop"), 3), "a camera no shown scene uses kept decoding"
    feed = engine.branches[camera.id].feed
    head = feed.elements[0]
    assert engine.branches[twin.id].feed is feed, "the linked camera has a capture of its own"
    run(1.5)

    # 1. A cut to the scene with the warm display: it shows on the first frame the outputs get.
    engine.switched_at = {}
    asked = time.monotonic()
    project.activate(two.id)
    engine.show_scene(two)
    assert until(lambda: "landscape" in engine.switched_at, 1), "the cut never started"
    print(f"cut started {(time.monotonic() - asked) * 1000:.0f} ms after it was asked (a parked camera's picture)")
    start = engine.switched_at["landscape"]
    assert until(lambda: len(fanout.after(start + 6 * FRAME)) > 0, 5), "no output pictures after the cut"
    first_pts, first = fanout.after(start)[0]
    last_pts, last = fanout.before(start)[-1]
    print(f"cut: last before {last} at -{(start - last_pts) / 1e6:.0f} ms; first after {first} at "
          f"+{(first_pts - start) / 1e6:.0f} ms")
    assert last["display"] < BG + 20 and last["camera-small"] > BG + 40, f"{last}\n{dump(engine)}"
    assert first["display"] > BG + 40, f"the display wasn't on the first frame after the cut: {first}"
    assert first["camera-big"] > BG + 40, first  # the big box (the small one lies inside it)
    assert first_pts - start < 2 * FRAME, "the first frame after the cut came late"
    assert first["side"] > BG + 40, f"the parked camera wasn't on the first frame after the cut: {first}"
    assert not parked.park.get_property("drop")
    assert not engine.placeholders, f"a card showed on the switch: {list(engine.placeholders)}"
    assert len(opened) == 3 and engine.branches[camera.id].feed is feed and feed.elements[0] is head, \
        "the camera was opened again"
    assert head.get_state(0)[1] == Gst.State.PLAYING

    # 2. A fade back: the display (in Two only) fades out; then a fade to Two again brings it to full
    #    alpha in the set duration. Meanwhile the camera glides between its two boxes.
    assert until(lambda: len(fanout.after(start + 20 * FRAME)) > 0, 15), "the outputs stopped"
    full = sorted(seen["display"] for _pts, seen in fanout.after(start + 4 * FRAME))[len(fanout.after(start + 4 * FRAME)) // 2]
    engine.transition = ("fade", 300)
    pads = {}
    mixer = engine.programs["landscape"].mixer
    track = []

    def geometry(_pad, info):
        buffer = info.get_buffer()
        if buffer is not None and pads:
            track.append((buffer.pts, {k: (p.get_property("xpos"), p.get_property("ypos"), p.get_property("width"),
                                           p.get_property("alpha")) for k, p in pads.items()}))
        return Gst.PadProbeReturn.OK

    pads.update(small=engine.branches[camera.id].layers["landscape"].pad, big=engine.branches[twin.id].layers["landscape"].pad)
    mixer.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, geometry)
    engine.switched_at = {}
    project.activate(one.id)
    engine.show_scene(one)
    assert until(lambda: "landscape" in engine.switched_at, 1)
    back = engine.switched_at["landscape"]
    # Waits are in output time: on a loaded builder the mixer can composite at half the clock's pace.
    assert until(lambda: len(fanout.after(back + 600 * Gst.MSECOND)) > 0, 15), "the outputs stopped"
    engine.switched_at = {}
    project.activate(two.id)
    engine.show_scene(two)
    assert until(lambda: "landscape" in engine.switched_at, 1)
    fade = engine.switched_at["landscape"]
    assert until(lambda: len(fanout.after(fade + 400 * Gst.MSECOND)) > 3, 5), "no output pictures after the fade"
    print("fade out (ms, luma):", [(round((pts - back) / 1e6), s["display"]) for pts, s in fanout.after(back - 2 * FRAME)
                                    if pts < fade][:16])
    seen = [(pts - fade, s["display"]) for pts, s in fanout.after(fade - 2 * FRAME)]
    print("fade in (ms, luma):", [(round(t / 1e6), y) for t, y in seen[:14]], "full", full)
    ramp = [y for t, y in seen if 0 < t < 300 * Gst.MSECOND]
    assert all(y < BG + 20 for t, y in seen if t < 0), "the display showed before the fade started"
    assert any(BG + 15 < y < full - 15 for y in ramp), f"no step between: {ramp}"
    assert all(b >= a - 6 for a, b in zip(ramp, ramp[1:])), f"the fade went back: {ramp}"
    done = next((t for t, y in seen if y >= full - 8), None)
    assert done is not None and done <= 300 * Gst.MSECOND + FRAME, f"full alpha at {done} ns, set 300 ms"

    # The glide (Two -> One): both camera layers on the same box every frame, from the big box to the small.
    glide = [(t - back, g) for t, g in track if back <= t <= back + 300 * Gst.MSECOND]
    print("glide (ms, small xpos/width, big xpos/width):",
          [(round(t / 1e6), g["small"][0], g["small"][2], g["big"][0], g["big"][2]) for t, g in glide[::3]])
    assert len(glide) >= 8, f"too few frames during the glide: {len(glide)}"
    parted = [(round(t / 1e6), g["small"][:3], g["big"][:3]) for t, g in glide if g["small"][:3] != g["big"][:3]]
    assert not parted, f"the two layers parted during the glide: {parted}"
    xs = [g["small"][0] for _t, g in glide]
    assert xs[0] <= 970 and all(b >= a for a, b in zip(xs, xs[1:])) and xs[-1] >= 1390, xs
    settled = [g for t, g in track if t > back + 340 * Gst.MSECOND and t < fade]
    assert settled and settled[-1]["small"][:3] == (1400, 700, 480) and settled[-1]["big"][3] == 0.0, settled[-1:]
    assert len(opened) == 3 and engine.branches[camera.id].feed is feed, "a scene change opened a source"
    engine.stop(wait=5.0)
    print("ok: switches open nothing; the cut shows on the first frame; the fade reaches full alpha in 300 ms; "
          "the camera glides")
    return 0


if __name__ == "__main__":
    sys.exit(main())
