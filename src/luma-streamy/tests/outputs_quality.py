#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Picture size and frame rate per encode: a 60 fps feed recorded twice at once, scaled to 360p at
60 fps and at its own size at 30 fps; a bitrate change while recording reaches the encoder. Probes the
files with ffprobe: their sizes, declared rates, and twice the pictures in the 60 fps one (a slow
builder may not encode every picture in real time; the ratio is the encode's own pacing).

    python3 tests/outputs_quality.py OUT_DIR
"""
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from luma_streamy.outputs import Outputs  # noqa: E402
from luma_streamy.output_graph import Preset  # noqa: E402

out = sys.argv[1]
os.makedirs(out, exist_ok=True)
Gst.init(None)
loop = GLib.MainLoop()
outputs = Outputs()
if shutil.which("ffprobe") is None or not {"openh264enc", "vah264enc", "vah264lpenc"} & outputs.working or \
        not {"fdkaacenc", "avenc_aac", "voaacenc"} & outputs.working:
    print("skipped: needs an H.264 and an AAC encoder, and ffprobe")
    sys.exit(0)
feed = Gst.parse_launch(
    "videotestsrc is-live=true pattern=ball ! video/x-raw,format=NV12,width=960,height=540,framerate=60/1 ! "
    "appsink name=v emit-signals=true sync=true max-buffers=4 drop=true "
    "audiotestsrc is-live=true wave=ticks ! audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2 ! "
    "appsink name=a emit-signals=true sync=true max-buffers=16 drop=true")


def pulled(sink, media):
    sample = sink.emit("pull-sample")
    if sample is not None:
        outputs.push_video("landscape", sample) if media == "v" else outputs.push_sound(sample)
    return Gst.FlowReturn.OK


feed.get_by_name("v").connect("new-sample", pulled, "v")
feed.get_by_name("a").connect("new-sample", pulled, "a")
errors: list[str] = []
seen: dict = {}


def start() -> bool:
    outputs.start("small", Preset("landscape", "h264", 2000, 128, size=360, fps=60), f"{out}/small.mkv", record=True)
    outputs.start("own", Preset("landscape", "h264", 3000, 128), f"{out}/own.mkv", record=True)
    seen["tap fps"] = outputs.fps("landscape")
    return False


def change() -> bool:
    encode = outputs.rows["own"].group
    seen["live bitrate"] = outputs.set_video_kbps("own", 1500)
    encoder = encode._encoder
    seen["encoder bitrate"] = encoder.get_property("bitrate") if encoder is not None else None
    return False


def stop() -> bool:
    outputs.stop_all()

    def gone() -> bool:
        if outputs.rows:
            return True
        loop.quit()
        return False

    GLib.timeout_add(200, gone)
    return False


feed.set_state(Gst.State.PLAYING)
GLib.timeout_add(300, start)
GLib.timeout_add(3000, change)
GLib.timeout_add(7000, stop)
GLib.timeout_add_seconds(60, lambda: (errors.append("timed out"), loop.quit()))
loop.run()
feed.set_state(Gst.State.NULL)
seen["tap fps after"] = outputs.fps("landscape")


def probe(path: str) -> dict:
    text = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                           "stream=width,height,nb_read_frames,r_frame_rate:format=duration", "-of", "json", path],
                          capture_output=True, text=True).stdout
    data = json.loads(text or "{}")
    stream = (data.get("streams") or [{}])[0]
    duration = float(data.get("format", {}).get("duration") or 0)
    frames = int(stream.get("nb_read_frames") or 0)
    return {"size": (stream.get("width"), stream.get("height")), "fps": frames / duration if duration else 0,
            "frames": frames, "duration": duration, "rate": stream.get("r_frame_rate")}


small, own = probe(f"{out}/small.mkv"), probe(f"{out}/own.mkv")
seen.update(small=small, own=own)
print(json.dumps(seen, indent=1, default=str))
if small["size"] != (640, 360):
    errors.append(f"360p came out {small['size']}")
if own["size"] != (960, 540):
    errors.append(f"its own size came out {own['size']}")
if small["rate"] != "60/1" or own["rate"] != "30/1":
    errors.append(f"declared rates {small['rate']} and {own['rate']}")
if not own["frames"] or not 1.8 <= small["frames"] / own["frames"] <= 2.2:
    errors.append(f"{small['frames']} pictures at 60 fps beside {own['frames']} at 30 fps")
if not 26 <= own["fps"] <= 32:
    errors.append(f"the 30 fps encode made {own['fps']:.1f} fps from a 60 fps feed")
if seen["tap fps"] != 60 or seen["tap fps after"] != 30:
    errors.append(f"the tap's rate didn't follow the fastest encode: {seen['tap fps']} then {seen['tap fps after']}")
if not seen.get("live bitrate"):
    errors.append("a bitrate change while recording didn't reach the encoder")
for error in errors:
    print(f"FAIL: {error}")
print("ok" if not errors else "failed")
sys.exit(1 if errors else 0)
