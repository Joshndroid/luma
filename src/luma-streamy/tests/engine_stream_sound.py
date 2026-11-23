#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""What a destination row sends carries the program's sound: the engine with its outputs fan-out
attached (as the window does), a vertical-canvas row recorded for a few seconds, decoded, and its
audio track loud for most of its length.

Variants: the sound widgets there from the start; a microphone added while the program runs; the
microphone's branch rebuilt mid-recording; a stream row (FLV, the RTMP path) written to a file, its
first streamheader (what rtmp2sink sends) carrying the AAC config and its AAC frames at 47 a second;
the same with encoders that move their output timestamps (vah264enc adds 1000 hours). "rtmp" (not
run by default) streams to STREAMY_SOUND_RTMP and decodes the newest file in STREAMY_SOUND_RTMP_REC.
Skipped (exit 0) where no H.264 and AAC encoder works.

    GST_PLUGIN_PATH=<lumaframe> python3 tests/engine_stream_sound.py [OUT_DIR] [VARIANT ...]
"""
import math
import os
import struct
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from luma_streamy import output_graph as og  # noqa: E402
from luma_streamy.engine import Engine  # noqa: E402
from luma_streamy.graph import Element  # noqa: E402
from luma_streamy.model import Project  # noqa: E402
from luma_streamy.output_graph import Preset  # noqa: E402
from luma_streamy import outputs as outputs_module  # noqa: E402
from luma_streamy.outputs import Outputs  # noqa: E402

RECORD_S = 6.0
#: The fake microphone is pink noise and desktop audio a sine, both at a quarter of full scale
#: (about -15 dBFS); a window below this is silence.
LOUD_DBFS = -40.0
#: Windows (100 ms) that must be loud: the first ones can be the mixer starting.
LOUD_SHARE = 0.8

#: Stream rows go to a file: the FLV muxer is the RTMP row's, rtmp2sink replaced by a filesink.
_flv_paths: dict[str, str] = {}
_row_sink = og.row_sink


def _local_row_sink(row_id, kind, target, *, codec="h264", container="mkv"):
    if kind == "rtmp" and row_id in _flv_paths:
        return [Element("flvmux", f"row-{row_id}-mux", {"streamable": True}),
                Element("filesink", f"row-{row_id}-sink", {"location": _flv_paths[row_id], "async": False,
                                                           "sync": False})]
    return _row_sink(row_id, kind, target, codec=codec, container=container)


og.row_sink = _local_row_sink

#: Every caps event an FLV row's sink receives, as (seconds since the row was built, caps string): rtmp2sink
#: sends the first one's streamheader when it connects, and skips the header buffers that follow.
_flv_caps: dict[str, list[tuple[float, str]]] = {}
_row_build = outputs_module.Row._build


def _probed_build(row, video_caps):
    _row_build(row, video_caps)
    sink = row.pipeline.get_by_name(f"row-{row.id}-sink") if row.id in _flv_paths else None
    if sink is None:
        return
    started = time.monotonic()
    seen = _flv_caps.setdefault(row.id, [])

    def caps(_pad, info):
        event = info.get_event()
        if event is not None and event.type == Gst.EventType.CAPS:
            seen.append((time.monotonic() - started, event.parse_caps().to_string()))
        return Gst.PadProbeReturn.OK

    sink.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, caps)


outputs_module.Row._build = _probed_build

#: STREAMY_SOUND_AAC_DELAY_S: encoded sound reaches the rows this much later than it comes out of the
#: encoder (in order, times unchanged), the way a fast hardware video encoder's pictures beat the AAC
#: encoder's frames to the mux.
AAC_DELAY_S = float(os.environ.get("STREAMY_SOUND_AAC_DELAY_S", "0"))
if AAC_DELAY_S > 0:
    import queue
    import threading

    _late: "queue.Queue" = queue.Queue()
    _push_sound = outputs_module.Row.push_sound

    def _deliver() -> None:
        while True:
            due, row, sample = _late.get()
            wait = due - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _push_sound(row, sample)

    threading.Thread(target=_deliver, daemon=True).start()
    outputs_module.Row.push_sound = lambda row, sample: _late.put((time.monotonic() + AAC_DELAY_S, row, sample))


#: The "offset" variant: the encoders move their output to bases of their own, as vah264enc does (1000 hours
#: later), here the pictures by 1000 hours and the sound by a minute; every row must still mux both.
ENCODER_OFFSETS = {"vout": 1000 * 3600 * Gst.SECOND, "aout": 60 * Gst.SECOND}
_offset_encoders = False
_encode_build = outputs_module.Encode._build


def _offset_build(encode, caps):
    _encode_build(encode, caps)
    if not _offset_encoders:
        return
    for suffix, offset in ENCODER_OFFSETS.items():
        sink = encode.pipeline.get_by_name(f"enc-{encode.preset.key}-{suffix}")
        if sink is None:
            continue

        def moved(_pad, info, offset=offset):
            buffer = info.get_buffer()
            if buffer.pts != Gst.CLOCK_TIME_NONE:
                buffer.pts += offset
            if buffer.dts != Gst.CLOCK_TIME_NONE:
                buffer.dts += offset
            return Gst.PadProbeReturn.OK

        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, moved)


outputs_module.Encode._build = _offset_build


def header_tags(caps: str) -> list[str]:
    """The streamheader buffers of FLV caps, as hex: the FLV header, onMetaData, then codec tags."""
    start = caps.find("streamheader=(buffer)<")
    if start < 0:
        return []
    body = caps[start + len("streamheader=(buffer)<"):caps.find(">", start)]
    return [part.strip() for part in body.split(",")]


def has_aac_config(tags: list[str]) -> bool:
    """An audio tag (type 8) whose data is AAC (sound format 10) packet type 0: the sequence header."""
    return any(t[:2] == "08" and len(t) >= 26 and t[22] == "a" and t[24:26] == "00" for t in tags)

def flv_tags(path: str) -> tuple[list[int], list[int]]:
    """The video tags' and the AAC raw-data tags' timestamps (ms) in an FLV file."""
    data = open(path, "rb").read()
    video, sound = [], []
    pos = 13  # the FLV header and the first previous-tag size
    while pos + 11 <= len(data):
        kind, size = data[pos], int.from_bytes(data[pos + 1:pos + 4], "big")
        ts = int.from_bytes(data[pos + 4:pos + 7], "big") | data[pos + 7] << 24
        body = data[pos + 11:pos + 11 + size]
        if kind == 9:
            video.append(ts)
        elif kind == 8 and len(body) > 1 and body[0] >> 4 == 10 and body[1] == 1:
            sound.append(ts)
        pos += 11 + size + 4
    return video, sound


def run(seconds: float) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


def audio_of(path: str) -> tuple[list[float], float]:
    """The file's sound as 100 ms RMS levels (dBFS), and its length in seconds; ([], 0) without one."""
    pipeline = Gst.Pipeline.new("decode")
    src = Gst.ElementFactory.make("filesrc")
    src.set_property("location", path)
    decode = Gst.ElementFactory.make("decodebin")
    # Pictures stay encoded: only the sound is decoded.
    decode.set_property("caps", Gst.Caps.from_string("audio/x-raw; video/x-h264; video/x-av1; video/x-raw"))
    for element in (src, decode):
        pipeline.add(element)
    src.link(decode)
    data = bytearray()

    def pulled(sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is not None:
            ok, info = sample.get_buffer().map(Gst.MapFlags.READ)
            if ok:
                data.extend(info.data)
                sample.get_buffer().unmap(info)
        return Gst.FlowReturn.OK

    def pad_added(_decode, pad) -> None:
        caps = pad.get_current_caps() or pad.query_caps(None)
        if caps.to_string().startswith("audio/"):
            chain = [Gst.ElementFactory.make(f) for f in ("queue", "audioconvert", "audioresample", "capsfilter",
                                                          "appsink")]
            chain[3].set_property("caps", Gst.Caps.from_string("audio/x-raw,format=S16LE,rate=48000,channels=1"))
            chain[4].set_property("emit-signals", True)
            chain[4].set_property("sync", False)
            chain[4].connect("new-sample", pulled)
        else:
            chain = [Gst.ElementFactory.make("fakesink")]
        for element in chain:
            pipeline.add(element)
        for a, b in zip(chain, chain[1:]):
            a.link(b)
        for element in chain:
            element.sync_state_with_parent()
        pad.link(chain[0].get_static_pad("sink"))

    decode.connect("pad-added", pad_added)
    pipeline.set_state(Gst.State.PLAYING)
    message = pipeline.get_bus().timed_pop_filtered(60 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
    if message is not None and message.type == Gst.MessageType.ERROR:
        print(f"  decode: {message.parse_error()[0].message}")
    pipeline.set_state(Gst.State.NULL)
    count = len(data) // 2
    samples = struct.unpack(f"<{count}h", bytes(data[:count * 2]))
    window = 4800
    levels = []
    for i in range(0, count - window + 1, window):
        chunk = samples[i:i + window]
        rms = math.sqrt(sum(s * s for s in chunk) / window) / 32768
        levels.append(20 * math.log10(rms) if rms > 0 else -200.0)
    return levels, count / 48000


def scene_project(sound: bool) -> tuple[Project, object]:
    project = Project.starter()
    scene = project.active_scene
    for w in list(scene.widgets):
        project.remove_widget(w.id)
    project.add_widget(scene.id, "display")
    if sound:
        project.add_widget(scene.id, "microphone")
        project.add_widget(scene.id, "desktop-audio")
    return project, scene


def check(variant: str, out_dir: str) -> None:
    global _offset_encoders
    _offset_encoders = variant == "offset"
    project, scene = scene_project(sound=variant != "added")
    engine = Engine(fake=True, preview=False)
    outputs = Outputs(working=WORKING)
    engine.fanout = outputs
    states: list[tuple[str, str]] = []

    def row_state(_o, row_id, state, _words) -> None:
        states.append((row_id, state))
        engine.sync_taps()  # as the window does

    outputs.connect("row-state", row_state)
    engine.set_canvases(("vertical",))  # shown, as the window's vertical preview does
    engine.start(project)
    run(1.5)
    if variant == "added":
        project.add_widget(scene.id, "microphone")
        engine.show_scene(scene)
        run(1.0)
    stream = variant in ("flv", "rtmp", "offset")
    row_id = f"row-{variant}"
    path = os.path.join(out_dir, f"sound-{variant}.{'flv' if stream else 'mkv'}")
    if variant == "rtmp":
        # A real RTMP server (STREAMY_SOUND_RTMP, e.g. mediamtx) records what it received into
        # STREAMY_SOUND_RTMP_REC; the newest recording there is what is decoded.
        outputs.start(row_id, Preset("vertical", "h264", 4000, 160), f"{os.environ['STREAMY_SOUND_RTMP']}/live/sound")
    elif stream:
        _flv_paths[row_id] = path
        outputs.start(row_id, Preset("vertical", "h264", 4000, 160), "rtmp://127.0.0.1/live/sound")
    else:
        outputs.start(row_id, Preset("vertical", "h264", 4000, 160), path, record=True, container="mkv")
    if variant == "rebuilt":
        run(RECORD_S / 3)
        mic = next(w for w in scene.widgets if w.kind == "microphone")
        engine._remove_branch(mic.id)  # as the window's _restart_widget does
        engine.show_scene(scene)
        run(RECORD_S * 2 / 3)
    else:
        run(RECORD_S)
    outputs.stop(row_id)
    t = time.monotonic()
    while (row_id, "off") not in states and time.monotonic() - t < 40:
        run(0.2)
    assert (row_id, "off") in states, f"{variant}: the row never finished ({states})"
    assert os.path.exists(path) or variant == "rtmp", f"{variant}: nothing was written (row states {states})"
    assert engine.stop(wait=30), f"{variant}: the program did not stop"
    if variant == "rtmp":
        run(2.0)
        rec = os.environ["STREAMY_SOUND_RTMP_REC"]
        files = [os.path.join(d, f) for d, _s, fs in os.walk(rec) for f in fs]
        assert files, f"rtmp: nothing recorded in {rec}"
        path = max(files, key=os.path.getmtime)
    levels, seconds = audio_of(path)
    loud = sum(1 for level in levels if level > LOUD_DBFS)
    share = loud / len(levels) if levels else 0.0
    shown = " ".join(f"{level:.0f}" for level in levels[::5])
    print(f"{variant}: {os.path.getsize(path)} bytes, sound {seconds:.2f} s, {loud}/{len(levels)} windows "
          f"above {LOUD_DBFS:.0f} dBFS; every 0.5 s: {shown}", flush=True)
    if row_id in _flv_caps:
        seen = _flv_caps[row_id]
        assert seen, f"{variant}: the FLV sink never got caps"
        first = header_tags(seen[0][1])
        print(f"  {len(seen)} caps event(s); the first streamheader has {len(first)} buffers "
              f"{[len(t) // 2 for t in first]}", flush=True)
        assert first and first[0].startswith("464c5601") and int(first[0][8:10], 16) & 4, \
            f"{variant}: the FLV header doesn't say there is audio"
        assert has_aac_config(first), f"{variant}: the first streamheader (what RTMP sends) has no AAC config"
        video, sound = flv_tags(path)
        span = (video[-1] - video[0]) / 1000 if len(video) > 1 else 0.0
        heard = (sound[-1] - sound[0]) / 1000 if len(sound) > 1 else 0.0
        rate = (len(sound) - 1) / heard if heard else 0.0
        print(f"  {len(video)} video tags over {span:.2f} s, {len(sound)} AAC frames over {heard:.2f} s "
              f"({rate:.1f}/s)", flush=True)
        # AAC is 1024 samples a frame: 46.9 frames a second at 48 kHz, with no gaps, over most of the pictures.
        assert heard >= span * 0.6, f"{variant}: AAC over {heard:.2f} s of {span:.2f} s of pictures"
        assert 44 <= rate <= 50, f"{variant}: {rate:.1f} AAC frames a second"
    assert seconds >= RECORD_S / 2, f"{variant}: {seconds:.2f} s of sound in a {RECORD_S:.0f} s recording"
    assert share >= LOUD_SHARE, f"{variant}: only {share:.0%} of the sound is above {LOUD_DBFS:.0f} dBFS"


VARIANTS = ("start", "added", "rebuilt", "flv", "offset", "rtmp")


WORKING: set[str] = set()


def main(out_dir: str, variants: list[str]) -> int:
    WORKING.update(outputs_module.working_encoders())
    if og.choose(WORKING, "h264") is None or og.choose(WORKING, "h264").audio is None:
        print("skipped: no working H.264 and AAC encoder here", flush=True)
        return 0
    failed = []
    for variant in variants or ["start", "added", "rebuilt", "flv", "offset"]:
        try:
            check(variant, out_dir)
        except AssertionError as error:
            print(f"FAIL {error}", flush=True)
            failed.append(variant)
    return 1 if failed else 0


if __name__ == "__main__":
    Gst.init(None)
    args = sys.argv[1:]
    out = args.pop(0) if args and args[0] not in VARIANTS else tempfile.mkdtemp()
    sys.exit(main(out, args))
