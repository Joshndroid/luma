# SPDX-License-Identifier: Apache-2.0
"""Outputs: every destination row live at once, one encode per canvas and preset.

The engine hands this module the program's raw pictures (NV12, per canvas) and sound (S16LE
48 kHz stereo) from its taps; ``Outputs`` encodes each (canvas, preset) once and tees the encoded
samples to every row that shares it. Each row is its own pipeline (output_graph.py has the shapes),
so a row that loses its connection reconnects alone (2 s, ×1.5, at most 15 min) and joins again on
the next key frame, which it asks the encoder for. A live stream whose bytes stop going out for
og.STALL_S is reconnected by Streamy rather than waiting for the server to drop it, and an encode with
automatic bitrate (og.RateControl) steps its bitrate down while its rows fall behind and back up once
they keep up. A recording finishes cleanly: EOS drains its
encoder, then its muxer writes the file's index.

Samples arrive on GStreamer threads; every state change (build, teardown, reconnect) runs on the
GLib main loop.
"""
from __future__ import annotations

import collections
import os
import re
import threading
import time
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, GObject, Gst, GstVideo  # noqa: E402

from . import output_graph as og  # noqa: E402
from .graph import Element, EncoderChoice, backoff  # noqa: E402
from .output_graph import Preset, RowStats  # noqa: E402

#: A stream whose connection is gone never sees its EOS: it is torn down after this. A recording
#: waits longer, so a busy machine still drains its encoder and finishes the file.
STREAM_FINISH_S = 5
RECORD_FINISH_S = 30


def log(text: str) -> None:
    """One line for the journal: every row and encode change is logged, so a dropped stream leaves a trace."""
    import sys

    print(f"streamy-outputs: {text}", file=sys.stderr, flush=True)


#: A live row writes its numbers to the journal this often (seconds).
STATS_LOG_S = 5


#: An encoder whose first output is this far (ns) from its first input has moved its timestamps to a base of
#: its own (vah264enc puts its output 1000 hours later); smaller differences are the encoder's own timing (an AAC
#: encoder's priming) and stay.
ENCODER_REBASE_MIN = 500_000_000

#: An encode keeps this much of its encoded sound (ns): a row starts on a key frame that a slow encoder
#: put out seconds after it went in, and takes the sound from that picture on, not from when it starts.
SOUND_BACKLOG_NS = 5 * 1_000_000_000

def min_frame_gap(fps: int) -> int:
    """Pictures closer together than this (ns) are over an encode's rate: 3/4 of a frame at ``fps``, so a
    jittery source at that rate keeps every picture and one at twice the rate keeps every other."""
    return 3 * 10**9 // (4 * fps)


MIN_FRAME_GAP = min_frame_gap(og.FPS)


def at_rate(caps: Gst.Caps, fps: int) -> Gst.Caps:
    """``caps`` declaring ``fps``: an encoder spends its CBR budget per declared frame, so the encode's
    input says the rate it is paced to, whatever the tap handed over."""
    text = caps.to_string()
    if "framerate=" in text:
        text = re.sub(r"framerate=\(fraction\)[0-9]+/[0-9]+", f"framerate=(fraction){fps}/1", text)
    else:
        text += f", framerate=(fraction){fps}/1"
    return Gst.Caps.from_string(text)


def counted(counts: dict, key: str, text: str) -> None:
    """Log a dropped sample at the 1st, 10th, 100th and every 1000th time it happens for ``key``, so a
    stream that loses its sound leaves a trace without flooding the journal."""
    n = counts[key] = counts.get(key, 0) + 1
    if n in (1, 10, 100) or n % 1000 == 0:
        log(f"{text} ({n} so far)")


def build(spec: Element) -> Gst.Element:
    element = Gst.ElementFactory.make(spec.factory, spec.name)
    if element is None:
        raise OutputError(f"{spec.factory} is not installed")
    for name, value in spec.props.items():
        if name == "caps":
            value = Gst.Caps.from_string(value)
        if "::" in name:  # a child's property (whipclientsink's signaller)
            element.set_property(name, value) if hasattr(element, "set_property") else None
            continue
        element.set_property(name, value)
    return element


def fresh(buffer: Gst.Buffer) -> Gst.Buffer:
    """A new buffer on the same memory whose times can be set. Buffer.copy() in Python is the boxed
    copy, a ref: setting its pts would change what every other row or encode receives."""
    return buffer.copy_region(Gst.BufferCopyFlags.FLAGS | Gst.BufferCopyFlags.TIMESTAMPS |
                              Gst.BufferCopyFlags.META | Gst.BufferCopyFlags.MEMORY, 0, buffer.get_size())


def link_all(elements: list[Gst.Element]) -> None:
    for a, b in zip(elements, elements[1:]):
        if not a.link(b):
            raise OutputError(f"{a.get_name()} does not link to {b.get_name()}")


class OutputError(RuntimeError):
    pass


def link_to_mux(element: Gst.Element, mux: Gst.Element, caps: Gst.Caps) -> bool:
    """Link ``element`` to the mux's request pad whose template takes ``caps`` (Element.link picks
    by the queue's still-open caps and can take the wrong one)."""
    for template in mux.get_pad_template_list():
        if template.direction == Gst.PadDirection.SINK and template.get_caps().can_intersect(caps):
            pad = mux.request_pad_simple(template.name_template) if template.presence == Gst.PadPresence.REQUEST \
                else mux.get_static_pad(template.name_template)
            if pad is not None and element.get_static_pad("src").link(pad) == Gst.PadLinkReturn.OK:
                return True
    return False


def working_encoders() -> set[str]:
    """The encoders installed here that really encode (a stub library or a missing driver fails)."""
    from .engine import available_factories, encoder_works
    from .graph import AV1, H264

    return {f for f in available_factories() if f not in H264 + AV1 or encoder_works(f)}


def _on_main(fn: Callable, *args) -> None:
    GLib.idle_add(lambda: fn(*args) and False)


class Row:
    """One destination's mux and sink: built on the first key frame, rebuilt after a dropped connection."""

    def __init__(self, outputs: "Outputs", group: "Encode", row_id: str, kind: str, target: str,
                 *, container: str = "mkv") -> None:
        self.outputs, self.group, self.id, self.kind, self.target = outputs, group, row_id, kind, target
        self.container = container
        self.stats = RowStats(state="connecting", started=time.monotonic())
        self.pipeline: Gst.Pipeline | None = None
        self.video_src = self.sound_src = None
        self.t0: int | None = None
        self.need_key = True
        self.stopping = False
        self.timer = 0
        self.attempt = 0
        self.finished = False
        self._last_bytes = 0
        self._last_tick = time.monotonic()
        self._lock = threading.Lock()
        self._bus_id = 0
        self._drops: dict[str, int] = {}
        self._sound_seen = False
        self._sound_last: int | None = None  # the newest sound pts pushed in
        #: Stall watch: the network counter last seen, when it last moved, and the pictures in by then.
        self._net_last: int | None = None
        self._net_moved = time.monotonic()
        self._frames_moved = 0
        self._dropped_seen = 0
        self.stats.target_kbps = self.stats.encode_kbps = group.current_kbps
        self.stats.auto = group.rate is not None

    @property
    def is_stream(self) -> bool:
        return self.kind != "file"

    # ── Samples (GStreamer threads) ──

    def push_video(self, sample: Gst.Sample) -> None:
        if self.stopping:
            return
        buffer = sample.get_buffer()
        key = not buffer.has_flags(Gst.BufferFlags.DELTA_UNIT)
        with self._lock:
            if self.pipeline is None:
                if self.group.sound_src is not None and self.group.sound_caps is None:
                    return  # the sound's first frame isn't out yet: start with both, on a later key frame
                if not key or self.timer:
                    if not key and self.need_key:
                        self.need_key = False
                        self.group.request_key()
                    return
                # A key frame: the row starts here. Built on this thread, so not one frame is lost.
                try:
                    self._build(sample.get_caps())
                except (OutputError, GLib.Error) as error:
                    _on_main(self._failed, str(error))
                    return
                self.t0 = buffer.pts
                # The sound the encode put out since this picture went in: on a slow encoder (seconds behind
                # on a busy machine) the row would otherwise start that much later on sound than on pictures.
                for recent in self.group.recent_sound(self.t0):
                    self._push_sound(recent)
            src = self.video_src
            if src.get_property("current-level-buffers") >= src.get_property("max-buffers"):
                self.stats.dropped_network += 1
                self.need_key = True  # what follows a dropped picture is useless until the next key frame
                return
            if self.need_key and self.t0 is not None and buffer.pts != self.t0:
                if not key:
                    self.stats.dropped_network += 1
                    return
            self.need_key = False
            self.stats.frames += 1
            src.emit("push-buffer", self._shift(buffer))

    def push_sound(self, sample: Gst.Sample) -> None:
        with self._lock:
            self._push_sound(sample)

    def _push_sound(self, sample: Gst.Sample) -> None:
        """One encoded sound sample into the row (its lock held)."""
        if self.stopping:
            return
        if self.pipeline is None or self.t0 is None:
            return  # not built yet (or reconnecting): it starts on a key frame, and takes the sound after it then
        buffer = sample.get_buffer()
        if self._sound_last is not None and buffer.pts != Gst.CLOCK_TIME_NONE and buffer.pts <= self._sound_last:
            return  # already in (pushed as the row started, from the encode's recent sound)
        if self.sound_src is None:
            counted(self._drops, "nopad", f"row {self.id}: sound dropped: the row was built without sound")
            return
        if buffer.pts == Gst.CLOCK_TIME_NONE:
            counted(self._drops, "nopts", f"row {self.id}: sound dropped: no timestamp")
            return
        if buffer.pts < self.t0:
            counted(self._drops, "early", f"row {self.id}: sound dropped: {(self.t0 - buffer.pts) / Gst.SECOND:.3f} s "
                                          f"before the row's first picture")
            return
        src = self.sound_src
        if src.get_property("current-level-buffers") >= src.get_property("max-buffers"):
            counted(self._drops, "full", f"row {self.id}: sound dropped: the mux isn't taking it "
                                         f"({src.get_property('current-level-buffers')} waiting)")
            return
        if not self._sound_seen:
            self._sound_seen = True
            log(f"row {self.id}: first sound {(buffer.pts - self.t0) / Gst.SECOND:+.3f} s from its first picture")
        self._sound_last = buffer.pts
        src.emit("push-buffer", self._shift(buffer))

    def _shift(self, buffer: Gst.Buffer) -> Gst.Buffer:
        out = fresh(buffer)
        out.pts = buffer.pts - self.t0
        if buffer.dts != Gst.CLOCK_TIME_NONE:
            out.dts = max(0, buffer.dts - self.t0) if buffer.dts >= self.t0 else Gst.CLOCK_TIME_NONE
        return out

    # ── Pipeline ──

    def _build(self, video_caps: Gst.Caps) -> None:
        codec = self.group.choice.codec
        pipeline = Gst.Pipeline.new(f"streamy-row-{self.id}")
        tail = [build(e) for e in og.row_sink(self.id, self.kind, self.target, codec=codec,
                                              container=self.container)]
        head = [build(e) for e in og.row_head(self.id, "video", self.group.preset.fps)]
        adapter = [build(e) for e in og.video_adapter(self.id, self.kind, codec)]
        for element in head + adapter + tail:
            pipeline.add(element)
        head[0].set_property("caps", video_caps)
        link_all(head + adapter)
        mux = tail[0]
        if not link_to_mux((head + adapter)[-1], mux, video_caps if not adapter else
                           Gst.Caps.from_string(og.video_adapter(self.id, self.kind, codec)[-1].props["caps"])):
            raise OutputError(f"{codec} doesn't go to {mux.get_factory().get_name()}")
        dump = os.environ.get("STREAMY_ROW_DUMP")
        if dump and self.is_stream and len(tail) >= 2:
            self._dump_branch(pipeline, tail, dump)
        else:
            link_all(tail)
        sound = None
        if self.group.sound_caps is not None:
            sound = [build(e) for e in og.row_head(self.id, "sound")]
            for element in sound:
                pipeline.add(element)
            sound[0].set_property("caps", self.group.sound_caps)
            link_all(sound)
            if not link_to_mux(sound[-1], mux, self.group.sound_caps):
                raise OutputError(f"AAC doesn't go to {mux.get_factory().get_name()}")
        counted = tail[-1].get_static_pad("sink")
        counted.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST, self._count)
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_id = bus.connect("message", self._message)
        self.pipeline, self.video_src = pipeline, head[0]
        self.sound_src = sound[0] if sound else None
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise OutputError("the destination could not start")
        log(f"row {self.id}: built {self.kind} {self.target.split('/')[2] if '://' in self.target else self.target} "
            f"on {video_caps.to_string()[:200]}")

    def _dump_branch(self, pipeline: Gst.Pipeline, tail: list[Gst.Element], folder: str) -> None:
        """STREAMY_ROW_DUMP=<dir>: the exact bytes the mux hands the stream's sink, also written to
        <dir>/<row>-<time>.<flv|ts>, and every caps event there (rtmp2sink sends the caps'
        streamheader, not the header buffers) to the same name + .caps."""
        os.makedirs(folder, exist_ok=True)
        base = os.path.join(folder, f"{self.id}-{time.strftime('%Y%m%d-%H%M%S')}")
        path = base + (".flv" if self.kind == "rtmp" else ".ts" if self.kind == "srt" else ".bin")
        tee = Gst.ElementFactory.make("tee", f"row-{self.id}-dumptee")
        queue = Gst.ElementFactory.make("queue", f"row-{self.id}-dumpq")
        for name, value in (("max-size-time", 0), ("max-size-buffers", 0), ("max-size-bytes", 0)):
            queue.set_property(name, value)
        sink = Gst.ElementFactory.make("filesink", f"row-{self.id}-dump")
        for name, value in (("location", path), ("async", False), ("sync", False)):
            sink.set_property(name, value)
        for element in (tee, queue, sink):
            pipeline.add(element)
        link_all([tail[0], tee, tail[1]])
        link_all([tee, queue, sink])
        link_all(tail[1:])
        started = time.monotonic()

        def caps_event(_pad, info) -> Gst.PadProbeReturn:
            event = info.get_event()
            if event is not None and event.type == Gst.EventType.CAPS:
                caps = event.parse_caps()
                # The header buffers' sizes, from the caps string (GstValueArray isn't iterable in Python).
                text = caps.to_string()
                start = text.find("streamheader=(buffer)<")
                sizes = []
                if start >= 0:
                    body = text[start + len("streamheader=(buffer)<"):text.find(">", start)]
                    sizes = [len(part.strip()) // 2 for part in body.split(",")]
                try:
                    with open(base + ".caps", "a") as out:
                        out.write(f"{time.monotonic() - started:.3f} s: streamheader {sizes} "
                                  f"{caps.to_string()[:4000]}\n")
                except OSError:
                    pass
            return Gst.PadProbeReturn.OK

        tee.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, caps_event)
        log(f"row {self.id}: dumping what is sent to {path}")

    def _count(self, _pad, info) -> Gst.PadProbeReturn:
        if info.type & Gst.PadProbeType.BUFFER_LIST:
            blist = info.get_buffer_list()
            self.stats.bytes += sum(blist.get(i).get_size() for i in range(blist.length())) if blist else 0
        else:
            buffer = info.get_buffer()
            if buffer is not None:
                self.stats.bytes += buffer.get_size()
        return Gst.PadProbeReturn.OK

    def _message(self, _bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.EOS:
            log(f"row {self.id}: end of stream")
            self._teardown()
            self._done()
        elif message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            log(f"row {self.id}: error from {message.src.get_name() if message.src else '?'}: {error.message} {debug}")
            self._failed(error.message)
        elif message.type == Gst.MessageType.WARNING:
            error, debug = message.parse_warning()
            log(f"row {self.id}: warning from {message.src.get_name() if message.src else '?'}: {error.message} {debug}")

    def _failed(self, message: str) -> None:
        self._teardown()
        if self.stopping:
            self._done()
            return
        words = og.error_words(message, self.kind)
        if not self.is_stream:
            self.stats.state, self.stats.words = "error", words
            self.outputs._row_gone(self)  # gone before anyone hears it ended
            self.outputs.emit("row-state", self.id, "error", words)
            return
        self.attempt += 1
        self.stats.reconnects += 1
        delay = backoff(self.attempt)
        log(f"row {self.id}: failed ({message}); reconnect {self.attempt} in {delay:.1f} s, "
            f"{self.stats.reconnects} so far")
        self.stats.state, self.stats.words = "reconnecting", f"{words}"
        self.outputs.emit("row-state", self.id, "reconnecting", og.state_words(self.stats))

        def again() -> bool:
            with self._lock:
                self.timer = 0
                self.need_key = True
            if not self.stopping:
                log(f"row {self.id}: reconnecting now; waiting for a key frame")
                self.stats.state = "connecting"
                self.group.request_key()
                self.outputs.emit("row-state", self.id, "connecting", og.state_words(self.stats))
            return False

        with self._lock:
            self.timer = GLib.timeout_add(int(delay * 1000), again)

    def _teardown(self) -> None:
        with self._lock:
            pipeline, self.pipeline = self.pipeline, None
            self.video_src = self.sound_src = None
            self.t0 = None
            self._sound_seen = False
            self._sound_last = None
        if pipeline is None:
            return
        bus = pipeline.get_bus()
        if self._bus_id:
            bus.disconnect(self._bus_id)
            self._bus_id = 0
        bus.remove_signal_watch()
        began = time.monotonic()
        pipeline.set_state(Gst.State.NULL)
        took = time.monotonic() - began
        log(f"row {self.id}: torn down in {took * 1000:.0f} ms")

    def stop(self) -> None:
        """Finish: EOS through the muxer (a file gets its index), then down."""
        if self.stopping:
            return
        self.stopping = True
        with self._lock:
            if self.timer:
                GLib.source_remove(self.timer)
                self.timer = 0
            srcs = [s for s in (self.video_src, self.sound_src) if s is not None]
        if not srcs:
            self._done()
            return
        self.stats.state = "finishing"
        self.outputs.emit("row-state", self.id, "finishing", og.state_words(self.stats))
        for src in srcs:
            src.emit("end-of-stream")
        GLib.timeout_add_seconds(STREAM_FINISH_S if self.is_stream else RECORD_FINISH_S,
                                 lambda: (self._teardown(), self._done()) and False)

    def _done(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.stats.state, self.stats.bitrate_kbps = "off", 0.0
        if self.kind == "file" and self.stats.frames == 0:
            try:
                os.unlink(self.target)  # a header alone is not a recording
            except OSError:
                pass
        self.outputs._row_gone(self)  # gone before anyone hears it ended: listeners see it off
        self.outputs.emit("row-state", self.id, "off", "")

    # ── Stats (main loop, ≤1 Hz) ──

    def backlog(self) -> float:
        """Seconds of pictures waiting to go out: in the row's queue, then its appsrc."""
        pipeline, src = self.pipeline, self.video_src
        if pipeline is None or src is None:
            return 0.0
        queue = pipeline.get_by_name(f"row-{self.id}-videoq")
        waiting = queue.get_property("current-level-time") / Gst.SECOND if queue is not None else 0.0
        return waiting + src.get_property("current-level-buffers") / self.group.preset.fps

    def health(self) -> og.Health:
        """The last second's network, for the encode's automatic bitrate."""
        dropped = self.stats.dropped_network - self._dropped_seen
        self._dropped_seen = self.stats.dropped_network
        return og.Health(self.stats.backlog_s, dropped, self.stats.state == "live" and self.pipeline is not None)

    def _watch_stall(self, progress: int, now: float) -> bool:
        """A live stream whose bytes stopped going out for og.STALL_S while pictures still came in is torn
        down and reconnected now (the server took ~40 s to drop it); whether it was."""
        if not self.is_stream or self.stats.state != "live" or self.pipeline is None or progress != self._net_last:
            self._net_last, self._net_moved, self._frames_moved = progress, now, self.stats.frames
            return False
        still = now - self._net_moved
        if still < og.STALL_S or self.stats.frames <= self._frames_moved:
            return False
        self.stats.stalls += 1
        log(f"row {self.id}: stalled: nothing went out for {still:.0f} s (sent {self.stats.bytes}, network "
            f"{progress}, {self.stats.frames - self._frames_moved} pictures in since, queue "
            f"{self.stats.backlog_s:.1f} s); reconnecting, {self.stats.stalls} stalls so far")
        self._net_last = None
        self._failed(f"stalled: nothing went out for {still:.0f} s")
        return True

    def tick(self) -> None:
        now = time.monotonic()
        span = max(1e-3, now - self._last_tick)
        sent = self.stats.bytes - self._last_bytes
        self._last_bytes, self._last_tick = self.stats.bytes, now
        self.stats.bitrate_kbps = sent * 8 / 1000 / span
        sink = self.pipeline.get_by_name(f"row-{self.id}-sink") if self.pipeline is not None else None
        network = None
        if sink is not None and self.kind in ("rtmp", "srt"):
            try:
                info = sink.get_property("stats")
            except TypeError:
                info = None
            if info is not None:
                for field in ("out-bytes-total", "bytes-sent-total", "bytes-sent"):
                    ok, value = info.get_uint64(field)
                    if ok:
                        network = value
                        break
                ok, rtt = info.get_double("rtt-ms")
                self.stats.rtt_ms = rtt if ok else None
                ok, acked = info.get_uint64("out-bytes-acked")
                if ok and network is not None:
                    self._unacked = network - acked
        self.stats.backlog_s = self.backlog()
        if self._watch_stall(network if network is not None else self.stats.bytes, now):
            return
        self._log_ticks = getattr(self, "_log_ticks", 0) + 1
        if self._log_ticks >= STATS_LOG_S:
            self._log_ticks = 0
            src = self.video_src
            level = f"{src.get_property('current-level-buffers')}/{src.get_property('max-buffers')}" if src else "-"
            log(f"row {self.id}: {self.stats.state}, {self.stats.bitrate_kbps:.0f} kb/s, sent {self.stats.bytes}, "
                f"network {network}, frames {self.stats.frames}, dropped network {self.stats.dropped_network} "
                f"encoder {self.group.dropped}, queue {level} ({self.stats.backlog_s:.2f} s), "
                f"unacked {getattr(self, '_unacked', None)}, rtt {self.stats.rtt_ms}, "
                f"bitrate {self.group.current_kbps}/{self.group.target_kbps}{' auto' if self.group.rate else ''}, "
                f"reconnects {self.stats.reconnects}, stalls {self.stats.stalls}, "
                f"pipeline {'up' if self.pipeline is not None else 'none'}")
        if self.stats.state == "connecting" and self.pipeline is not None and sent > 0 and \
                (network is None or network > 0):
            log(f"row {self.id}: live ({sent} bytes in the last second, network {network})")
            self.stats.state = "live"
            self.attempt = 0
            self.outputs.emit("row-state", self.id, "live", og.state_words(self.stats))
        if self.stats.state == "live":
            self.stats.history.append(round(self.stats.bitrate_kbps, 1))
            del self.stats.history[:-og.HISTORY]


class Encode:
    """One (canvas, preset) encode: raw program pictures and sound in, encoded samples to its rows."""

    def __init__(self, outputs: "Outputs", preset: Preset, choice: EncoderChoice) -> None:
        self.outputs, self.preset, self.choice = outputs, preset, choice
        self.rows: dict[str, Row] = {}
        self.pipeline: Gst.Pipeline | None = None
        self.video_src = self.sound_src = None
        self.sound_caps: Gst.Caps | None = None
        self.t0: int | None = None
        self.dropped = 0
        self.paced = 0  # pictures over the preset's fps, dropped by timestamp before the encoder
        self._min_gap = min_frame_gap(preset.fps)
        self._last_pts: int | None = None
        #: The dict key Outputs holds this encode under (it moves when a live bitrate change re-keys it).
        self.slot = preset
        #: The video bitrate as set, the encoder's now, and its automatic bitrate (None: fixed).
        self.target_kbps = self.current_kbps = preset.video_kbps
        self.rate: og.RateControl | None = og.RateControl(preset.video_kbps) if preset.auto else None
        self.scaled_to: str | None = None
        self.stopping = False
        self._eos: set[str] = set()
        self._lock = threading.Lock()
        self._encoder: Gst.Element | None = None
        self._vout: Gst.Element | None = None
        self._drops: dict[str, int] = {}
        self._sound_seen = False
        #: Per medium: the first timestamp pushed into its encoder, and how far its encoder moved its output
        #: from the input's timeline (learned from the first encoded sample, then taken off every one).
        self._first_in: dict[str, int] = {}
        self._offset: dict[str, int] = {}
        #: The last SOUND_BACKLOG_NS of encoded sound, for a row that starts on a picture encoded before it.
        self._sound_backlog: collections.deque = collections.deque()
        self._backlog_lock = threading.Lock()

    def _build(self, caps: Gst.Caps) -> None:
        caps = at_rate(caps, self.preset.fps)
        structure = caps.get_structure(0)
        ok_w, width = structure.get_int("width")
        ok_h, height = structure.get_int("height")
        source = (width, height) if ok_w and ok_h else None
        gpu = self.choice.video.startswith("va") and Gst.ElementFactory.find("vapostproc") is not None
        try:
            self._build_with(caps, source, gpu)
        except OutputError as error:
            if not gpu or not self.preset.size:
                raise
            log(f"encode {self.preset.key}: scaling on the GPU failed ({error}); scaling on the CPU")
            self._build_with(caps, source, False)

    def _build_with(self, caps: Gst.Caps, source: tuple[int, int] | None, gpu: bool) -> None:
        pipeline = Gst.Pipeline.new(f"streamy-encode-{self.preset.key}")
        video_src = Gst.ElementFactory.make("appsrc", "encode-video")
        for name, value in (("is-live", True), ("format", Gst.Format.TIME), ("do-timestamp", False),
                            ("max-buffers", 8), ("block", False), ("caps", caps)):
            video_src.set_property(name, value)
        specs = og.encode_video(self.choice, self.preset, source, gpu_scale=gpu)
        video = [build(e) for e in specs]
        pipeline.add(video_src)
        for element in video:
            pipeline.add(element)
        link_all([video_src] + video)
        scale = next((e for e in specs if e.name.endswith("-size") or e.name.endswith("-i420")), None)
        self.scaled_to = scale.props["caps"] if scale is not None and "width=" in scale.props["caps"] else None
        video[-1].connect("new-sample", self._encoded, "video")
        video[-1].connect("eos", self._encoded_eos, "video")
        self._encoder = next(e for e in video if e.get_name().endswith("-encoder"))
        self._vout = video[-1]
        sound = [build(e) for e in og.encode_sound(self.choice, self.preset)]
        sound_src = None
        if sound:
            sound_src = Gst.ElementFactory.make("appsrc", "encode-sound")
            for name, value in (("is-live", True), ("format", Gst.Format.TIME), ("do-timestamp", False),
                                ("max-buffers", 200), ("block", False),
                                ("caps", Gst.Caps.from_string(
                                    "audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2"))):
                sound_src.set_property(name, value)
            pipeline.add(sound_src)
            for element in sound:
                pipeline.add(element)
            link_all([sound_src] + sound)
            sound[-1].connect("new-sample", self._encoded, "sound")
            sound[-1].connect("eos", self._encoded_eos, "sound")
        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._error)
        bus.connect("message::warning", lambda _b, m: log(
            f"encode {self.preset.key}: warning from {m.src.get_name() if m.src else '?'}: {m.parse_warning()[0].message}"))
        self.pipeline, self.video_src, self.sound_src = pipeline, video_src, sound_src
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise OutputError("the encoder could not start")
        log(f"encode {self.preset.key}: {self.choice.video} + {self.choice.audio}, {self.preset.fps} fps, "
            f"{self.target_kbps} kb/s{' (automatic)' if self.rate is not None else ''}"
            f"{f', scaled to {self.scaled_to}' if self.scaled_to else ''}")

    # Raw in (GStreamer threads: the engine's taps)

    def push_video(self, sample: Gst.Sample) -> None:
        buffer = sample.get_buffer()
        if self.stopping or buffer.pts == Gst.CLOCK_TIME_NONE:
            return
        with self._lock:
            if self.pipeline is None:
                try:
                    self._build(sample.get_caps())
                except (OutputError, GLib.Error) as error:
                    self.stopping = True
                    _on_main(self.outputs._encode_failed, self, str(error))
                    return
                self.t0 = buffer.pts
            elif self._last_pts is not None and buffer.pts + Gst.SECOND < self._last_pts:
                # The program was rebuilt (its clock starts again): carry on from the last picture sent, so
                # the encoder and every row see time keep going instead of dropping everything as "before t0".
                sent = self._last_pts - self.t0
                self.t0 = buffer.pts - sent - Gst.SECOND // self.preset.fps
                self._last_pts = None
                log(f"encode {self.preset.key}: the program restarted; timestamps carry on from {sent / Gst.SECOND:.1f} s")
            if buffer.pts < self.t0:
                return
            # The encode is its preset's fps: a picture closer than ~3/4 of a frame to the last one kept is
            # dropped here, by timestamp, whatever rate the program's tap hands over (the tap runs at the
            # fastest encode of its canvas; it once gave its 60 fps canvas rate through, which doubled every
            # bitrate: CBR spends its budget per declared frame).
            if self._last_pts is not None and buffer.pts - self._last_pts < self._min_gap:
                self.paced += 1
                if self.paced == 1:
                    log(f"encode {self.preset.key}: pictures arrive faster than {self.preset.fps} fps; "
                        f"keeping {self.preset.fps}")
                return
            self._last_pts = buffer.pts
            src = self.video_src
            if src.get_property("current-level-buffers") >= src.get_property("max-buffers"):
                self.dropped += 1  # the encoder is behind
                if self.dropped in (1, 10, 100) or self.dropped % 1000 == 0:
                    log(f"encode {self.preset.key}: the encoder is behind; {self.dropped} pictures dropped")
                return
            out = fresh(buffer)
            out.pts, out.dts = buffer.pts - self.t0, Gst.CLOCK_TIME_NONE
            self._first_in.setdefault("video", out.pts)
            src.emit("push-buffer", out)

    def push_sound(self, sample: Gst.Sample) -> None:
        buffer = sample.get_buffer()
        with self._lock:
            if self.stopping or self.pipeline is None:
                return  # the encode starts on its first picture; the sound before it isn't wanted
            key = self.preset.key
            if self.sound_src is None:
                counted(self._drops, "nopad", f"encode {key}: sound dropped: no AAC encoder")
                return
            if buffer.pts == Gst.CLOCK_TIME_NONE:
                counted(self._drops, "nopts", f"encode {key}: sound dropped: no timestamp")
                return
            if buffer.pts < self.t0:
                counted(self._drops, "early", f"encode {key}: sound dropped: {(self.t0 - buffer.pts) / Gst.SECOND:.3f} s "
                                              f"before the first picture")
                return
            src = self.sound_src
            if src.get_property("current-level-buffers") >= src.get_property("max-buffers"):
                counted(self._drops, "full", f"encode {key}: sound dropped: the AAC encoder is behind")
                return
            if not self._sound_seen:
                self._sound_seen = True
                log(f"encode {key}: first sound {(buffer.pts - self.t0) / Gst.SECOND:+.3f} s from the first picture")
            out = fresh(buffer)
            out.pts, out.dts = buffer.pts - self.t0, Gst.CLOCK_TIME_NONE
            self._first_in.setdefault("sound", out.pts)
            src.emit("push-buffer", out)

    # Encoded out (GStreamer threads)

    def _encoded(self, sink: Gst.Element, media: str) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        sample = self._input_timeline(sample, media)
        if media == "sound":
            if self.sound_caps is None:
                self.sound_caps = sample.get_caps()
            pts = sample.get_buffer().pts
            with self._backlog_lock:
                self._sound_backlog.append(sample)
                while pts != Gst.CLOCK_TIME_NONE and self._sound_backlog[0].get_buffer().pts + SOUND_BACKLOG_NS < pts:
                    self._sound_backlog.popleft()
            # Rows after the backlog's lock (a row starting holds its own lock, then takes that one).
            for row in list(self.rows.values()):
                row.push_sound(sample)
        else:
            for row in list(self.rows.values()):
                row.push_video(sample)
        return Gst.FlowReturn.OK

    def recent_sound(self, since: int) -> list[Gst.Sample]:
        """The encoded sound at or after ``since`` (pts) still held, oldest first."""
        with self._backlog_lock:
            return [s for s in self._sound_backlog if s.get_buffer().pts != Gst.CLOCK_TIME_NONE and
                    s.get_buffer().pts >= since]

    def _input_timeline(self, sample: Gst.Sample, media: str) -> Gst.Sample:
        """The encoded sample on the timeline its input was pushed on. Rows interleave video and sound by
        these times, and an encoder that moves its output to a base of its own (vah264enc adds 1000 hours,
        keeping its DTS positive) put every picture 1000 hours after the sound: each row dropped all its
        sound as "before the first picture". The offset is the first output's distance from the first
        input (an encoder's first output is its first input), taken off every sample after it."""
        buffer = sample.get_buffer()
        offset = self._offset.get(media)
        if offset is None:
            first = self._first_in.get(media)
            if first is None or buffer.pts == Gst.CLOCK_TIME_NONE:
                return sample
            offset = buffer.pts - first
            if abs(offset) < ENCODER_REBASE_MIN:
                offset = 0
            else:
                name = self.choice.video if media == "video" else self.choice.audio
                log(f"encode {self.preset.key}: {name}'s timestamps are offset by {offset / Gst.SECOND:.3f} s; "
                    f"mapped back")
            self._offset[media] = offset
        if not offset:
            return sample
        out = fresh(buffer)
        for field in ("pts", "dts"):
            value = getattr(buffer, field)
            if value != Gst.CLOCK_TIME_NONE:
                # A DTS before the first picture (an encoder's reorder priming) has no place on the input's
                # timeline: it goes (the muxers take the PTS).
                setattr(out, field, value - offset if value >= offset else
                        (0 if field == "pts" else Gst.CLOCK_TIME_NONE))
        return Gst.Sample.new(out, sample.get_caps(), sample.get_segment(), sample.get_info())

    def _encoded_eos(self, _sink, media: str) -> None:
        self._eos.add(media)
        if self._eos >= ({"video", "sound"} if self.sound_src is not None else {"video"}):
            _on_main(self._drained)

    def request_key(self) -> None:
        """Ask the encoder for a key frame now (a row joining or reconnecting starts on it)."""
        log(f"encode {self.preset.key}: key frame requested")
        if self._vout is not None:
            self._vout.send_event(GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0))

    def _bitrate_property(self) -> tuple[str, int] | None:
        """The encoder's bitrate property and its unit (bits a second per kb/s), if it changes while it runs."""
        encoder = self._encoder
        if encoder is None:
            return None
        name = encoder.get_factory().get_name()
        prop, unit = ("target-bitrate", 1) if name == "svtav1enc" else ("bitrate", 1000 if name == "openh264enc" else 1)
        spec = encoder.find_property(prop)
        if spec is None or not spec.flags & Gst.PARAM_MUTABLE_PLAYING:
            return None
        return prop, unit

    def set_bitrate(self, kbps: int) -> bool:
        """The encoder's bitrate now (kb/s); whether this encoder takes it while it runs."""
        found = self._bitrate_property()
        if found is None:
            return False
        prop, unit = found
        self._encoder.set_property(prop, kbps * unit)
        self.current_kbps = kbps
        return True

    def retarget(self, kbps: int) -> bool:
        """The rows' bitrate changed while live; whether the encoder took it."""
        if self._bitrate_property() is None:
            return False
        old = self.target_kbps
        self.target_kbps = kbps
        start = self.rate.retarget(kbps, time.monotonic()) if self.rate is not None else kbps
        self.set_bitrate(start)
        log(f"encode {self.preset.key}: bitrate set to {kbps} kb/s (was {old})")
        return True

    def set_auto(self, on: bool) -> bool:
        """Automatic bitrate on or off while live; whether the encoder can follow it."""
        if on == (self.rate is not None):
            return True
        if on and self._bitrate_property() is None:
            return False
        self.rate = og.RateControl(self.target_kbps) if on else None
        if not on:
            self.set_bitrate(self.target_kbps)
        log(f"encode {self.preset.key}: automatic bitrate {'on' if on else 'off'} at {self.target_kbps} kb/s")
        return True

    def adapt(self, now: float) -> None:
        """Once a second: automatic bitrate follows the worst of the encode's rows (og.RateControl)."""
        if self.rate is None or self._encoder is None:
            return
        if self._bitrate_property() is None:
            log(f"encode {self.preset.key}: automatic bitrate off: {self._encoder.get_factory().get_name()} "
                f"can't change its bitrate while it runs")
            self.rate = None
            return
        rows = {row.id: row.health() for row in list(self.rows.values()) if row.is_stream}
        step = self.rate.update(rows, now)
        if step is None:
            return
        kbps, why = step
        old = self.current_kbps
        self.set_bitrate(kbps)
        log(f"encode {self.preset.key}: automatic bitrate {old} -> {kbps} kb/s of {self.target_kbps}: {why}")

    def _error(self, _bus, message: Gst.Message) -> None:
        error, debug = message.parse_error()
        log(f"encode {self.preset.key}: {error.message} {debug}")
        self.outputs._encode_failed(self, error.message)

    def stop(self) -> None:
        """Drain the encoder (EOS), then finish every row behind it."""
        with self._lock:
            if self.stopping and self.pipeline is None:
                return
            self.stopping = True
            srcs = [s for s in (self.video_src, self.sound_src) if s is not None]
        if not srcs:
            self._drained()
            return
        # Its rows say Finishing now (the engine's tap follows the encodes left), not once a slow encoder
        # has drained.
        for row in list(self.rows.values()):
            if not row.stopping and row.stats.state != "finishing":
                row.stats.state = "finishing"
                self.outputs.emit("row-state", row.id, "finishing", og.state_words(row.stats))
        for src in srcs:
            src.emit("end-of-stream")
        GLib.timeout_add_seconds(RECORD_FINISH_S, lambda: self._drained() and False)

    def _drained(self) -> None:
        for row in list(self.rows.values()):
            row.stop()
        self.close()

    def close(self) -> None:
        with self._lock:
            pipeline, self.pipeline = self.pipeline, None
            self.video_src = self.sound_src = None
        if pipeline is not None:
            pipeline.get_bus().remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)


class Outputs(GObject.Object):
    """Every live destination row: start and stop rows, feed raw program media, read stats."""

    __gtype_name__ = "StreamyOutputs"
    __gsignals__ = {
        # row id, state (connecting | live | reconnecting | finishing | error | off), words
        "row-state": (GObject.SignalFlags.RUN_FIRST, None, (str, str, str)),
        # Called once a second while anything is live (the Stats panel redraws on it).
        "tick": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "problem": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, *, working: set[str] | None = None) -> None:
        super().__init__()
        Gst.init(None)
        self._working = working
        self.encodes: dict[Preset, Encode] = {}
        self.rows: dict[str, Row] = {}
        self.stats_of: dict[str, RowStats] = {}
        self._tick = 0
        self._lock = threading.Lock()

    @property
    def working(self) -> set[str]:
        if self._working is None:
            self._working = working_encoders()
        return self._working

    # ── Rows ──

    def start(self, row_id: str, preset: Preset, target: str, *, record: bool = False,
              container: str = "mkv") -> Row:
        """Start one row: ``target`` is its address (stream) or its file's path (recording)."""
        if row_id in self.rows:
            raise OutputError("already live")
        kind = "file" if record else og.kind_of(target)
        if kind == "whip" and Gst.ElementFactory.find("whipclientsink") is None:
            raise OutputError("WHIP needs GStreamer's webrtc plugin, which isn't installed")
        if kind == "srt" and Gst.ElementFactory.find("srtsink") is None:
            raise OutputError("SRT needs GStreamer's srt plugin, which isn't installed")
        choice = og.choose_record(self.working, preset.codec) if record else og.choose(self.working, preset.codec)
        if choice is None:
            raise OutputError("No video encoder works on this computer" if record else
                              f"No {'H.264' if preset.codec == 'h264' else 'AV1'} encoder works on this computer")
        if choice.audio is None and not record:
            raise OutputError("No AAC encoder is installed")
        if record:
            import dataclasses

            preset = dataclasses.replace(preset, codec=choice.codec, auto=False)
        with self._lock:
            encode = self.encodes.get(preset)
            if encode is None or encode.stopping:
                encode = self.encodes[preset] = Encode(self, preset, choice)
            row = Row(self, encode, row_id, kind, target, container=container)
            encode.rows[row_id] = row
            self.rows[row_id] = row
        self.stats_of[row_id] = row.stats
        encode.request_key()
        self.emit("row-state", row_id, "connecting", og.state_words(row.stats))
        if not self._tick:
            self._tick = GLib.timeout_add_seconds(1, self._on_tick)
        return row

    def stop(self, row_id: str) -> None:
        row = self.rows.get(row_id)
        if row is None:
            return
        encode = row.group
        if not row.is_stream and len(encode.rows) == 1:
            encode.stop()  # a recording: drain its encoder first, so its last second is in the file
        else:
            row.stop()

    def stop_all(self) -> None:
        for encode in list(self.encodes.values()):
            encode.stop()

    def close_now(self) -> None:
        """Quitting: every row and encode to NULL at once (no drain), so no thread outlives the app."""
        with self._lock:
            rows, encodes = list(self.rows.values()), list(self.encodes.values())
        for row in rows:
            row.stopping = True
            row._teardown()
        for encode in encodes:
            encode.stopping = True
            encode.close()

    def _row_gone(self, row: Row) -> None:
        with self._lock:
            if self.rows.get(row.id) is row:
                del self.rows[row.id]
            encode = row.group
            encode.rows.pop(row.id, None)
            if not encode.rows and not encode.stopping:
                encode.stopping = True
                close = encode
            else:
                close = None
            if not encode.rows and self.encodes.get(encode.slot) is encode:
                del self.encodes[encode.slot]
        if close is not None:
            close.close()

    def _encode_failed(self, encode: Encode, message: str) -> None:
        self.emit("problem", f"The encoder stopped: {message}")
        for row in list(encode.rows.values()):
            row.stats.state, row.stats.words = "error", "The encoder stopped"
            row.stopping = True
            row._teardown()
            self._row_gone(row)
            self.emit("row-state", row.id, "error", row.stats.words)
        encode.close()

    # ── Media from the engine (GStreamer threads) ──

    def wants(self) -> set[str]:
        """The canvases whose pictures an encode needs now (the engine opens those taps only)."""
        return {p.canvas for p, e in list(self.encodes.items()) if not e.stopping}

    def fps(self, canvas: str) -> int:
        """The rate canvas ``canvas``'s tap hands over: its fastest encode's (each encode paces to its own)."""
        return max([e.preset.fps for e in list(self.encodes.values()) if e.preset.canvas == canvas and
                    not e.stopping] or [og.FPS])

    # ── Live changes (the row's editor) ──

    def sharing(self, row_id: str) -> list[str]:
        """The other rows on row ``row_id``'s encode."""
        row = self.rows.get(row_id)
        return [r for r in row.group.rows if r != row_id] if row is not None else []

    def set_video_kbps(self, row_id: str, kbps: int) -> bool:
        """Row ``row_id``'s bitrate now, while live; False when it waits for the next Go live (the row
        shares its encode, or the encoder can't change it while it runs)."""
        row = self.rows.get(row_id)
        if row is None or self.sharing(row_id) or not row.group.retarget(kbps):
            return False
        self._rekey(row.group, video_kbps=kbps)
        row.stats.target_kbps, row.stats.encode_kbps = kbps, row.group.current_kbps
        return True

    def set_auto(self, row_id: str, on: bool) -> bool:
        """Row ``row_id``'s automatic bitrate now, while live (False: on the next Go live)."""
        row = self.rows.get(row_id)
        if row is None or not row.is_stream or self.sharing(row_id) or not row.group.set_auto(on):
            return False
        self._rekey(row.group, auto=on)
        row.stats.auto, row.stats.encode_kbps = on, row.group.current_kbps
        return True

    def _rekey(self, encode: Encode, **changes) -> None:
        """A row that starts later with the changed settings joins this encode."""
        import dataclasses

        with self._lock:
            new = dataclasses.replace(encode.slot, **changes)
            if self.encodes.get(encode.slot) is encode and new not in self.encodes:
                del self.encodes[encode.slot]
                self.encodes[new] = encode
                encode.slot = new

    def push_video(self, canvas: str, sample: Gst.Sample) -> None:
        for preset, encode in list(self.encodes.items()):
            if preset.canvas == canvas:
                encode.push_video(sample)

    def push_sound(self, sample: Gst.Sample) -> None:
        for encode in list(self.encodes.values()):
            encode.push_sound(sample)

    # ── Stats ──

    def stats(self, row_id: str) -> RowStats | None:
        return self.stats_of.get(row_id)

    def encoder_drops(self, row_id: str) -> int:
        row = self.rows.get(row_id)
        return row.group.dropped if row is not None else 0

    def live(self) -> bool:
        return bool(self.rows)

    def _on_tick(self) -> bool:
        for row in list(self.rows.values()):
            row.stats.dropped_encoder = row.group.dropped
            row.tick()
        now = time.monotonic()
        for encode in list(self.encodes.values()):
            encode.adapt(now)
        for row in list(self.rows.values()):
            row.stats.target_kbps, row.stats.encode_kbps = row.group.target_kbps, row.group.current_kbps
            row.stats.auto = row.group.rate is not None
        self.emit("tick")
        if not self.rows:
            self._tick = 0
            return False
        return True
