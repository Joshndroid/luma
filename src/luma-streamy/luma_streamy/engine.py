# SPDX-License-Identifier: Apache-2.0
"""Streamy's engine: builds and edits the GStreamer graph from the model.

Python only builds and edits the graph; every frame stays in GStreamer.

- One program pipeline. Every source a scene uses is captured once (a feed: a
  camera by its device and mode, anything else by its source id), shared by
  every widget of every scene that shows it, and kept open while the program
  runs. Each visible picture widget is a layer per canvas (valve → queue →
  lumaframe → glvideomixer pad); only the active scene's layers pass pictures
  and have alpha. Sound feeds go through a gate (volume) into the audiomixer.
  A scene switch only changes which layers draw: nothing opens or closes.
- Transitions (cut, fade) are keyframes on the mixer pads' alpha (and the sound
  gates' volume) on the program clock, so every frame of the preview, the
  streams and the recordings shows the same step. A picture in both scenes
  glides from its old place to its new one instead of fading.
- Outputs (a recording, a stream) are pipelines of their own, fed from the
  taps by appsrc. They exist only while recording or live, and a failure in
  one (a dropped connection) never stops the program or the other output.
"""
from __future__ import annotations

import atexit
import os
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Callable

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstBase", "1.0")
gi.require_version("GstController", "1.0")
from gi.repository import GLib, GObject, Gst, GstBase, GstController  # noqa: E402,F401 (GstBase: Aggregator.peek_next_sample)

from . import graph  # noqa: E402
from .graph import ALPHA_FORMATS, ALPHA_FOURCCS, Chain, Element, EncoderChoice, only_alpha, prefer_alpha  # noqa: E402,F401
from .model import FORMATS, Project, Scene, Widget  # noqa: E402

FADE_MS = 300
#: A change of scene starts this far ahead (two program frames), so every layer it shows has its first
#: picture in the mixer by then: the switch is frame-accurate on every canvas, the outputs' too.
LEAD_NS = 2 * Gst.SECOND // 60
LEAD_MS = 34
#: A change of scene waits at most this long for its pictures (a parked camera's next one, a late GL).
SWITCH_WAIT_MS = 250
#: Mixer pad stacking: each scene's widgets in their order, the shown scene above the one it replaces.
ZSTEP = 10_000


def _smooth(t: float) -> float:
    """Ease in and out (smoothstep)."""
    return t * t * (3 - 2 * t)


def _fade_out(t: float) -> float:
    """The outgoing scene's fade: slow at first, under the incoming one's ease (so a picture in the same
    place in both scenes barely dims), all gone at the end."""
    e = _smooth(t)
    return e * e

#: The GL display every program pipeline in this process shares (a gst.gl.GLDisplay context). Each pipeline
#: making its own would open the same EGLDisplay underneath, and an old program's eglTerminate as its display
#: goes breaks the GL of the program running after it (a restart, a second engine). The first display a
#: program used (its own, or the preview sink's from GDK) is kept for the life of the process.
_gl_display: Gst.Context | None = None


#: Every engine alive in this process, so the interpreter never goes while one of them still runs.
_engines: "weakref.WeakSet[Engine]" = weakref.WeakSet()


@atexit.register
def _stop_engines() -> None:
    """At exit (also on an uncaught exception): every program NULL and every stop finished before the
    interpreter finalizes; a GL thread still at work when Mesa unloads crashes the process."""
    for engine in list(_engines):
        engine.stop(wait=5.0)
    # Then nothing GL may outlive the interpreter: a GL context still alive when libEGL's destructor runs
    # is torn down by it, and llvmpipe rasterizes its last scene while the driver is being unloaded.
    # Everything is NULL by now, so it all goes here, on the main thread.
    global _gl_display
    _dropping.clear()
    _gl_display = None
    import gc

    gc.collect()
    if os.environ.get("STREAMY_STOP_TRACE"):
        try:
            names = [open(f"/proc/self/task/{t}/comm").read().strip() for t in os.listdir("/proc/self/task")]
        except OSError:
            names = []
        log(f"at exit: {names.count('gstglcontext')} GL context threads left")


#: Elements and pipelines leaving the program, held until the main loop finds itself their only owner.
_dropping: list[Gst.Element] = []


def _drop_on_main(elements: list[Gst.Element]) -> None:
    """Let ``elements`` go on the main thread. GTK's paintable sink holds GDK objects that may only be
    dropped there (dropped elsewhere, it aborts the process), and a pipeline disposed on a close thread
    could still be disposing GL elements when a quit exits. Whatever else still holds them (the old
    pipeline going NULL on a close thread, a pool stop, a reference cycle the collector could free on a
    streaming thread), the last reference is this list's, released from the main loop once they are out
    of their pipeline and nothing else refers to them. At a quit they are simply never released (all NULL)."""
    if not _dropping:
        GLib.timeout_add(250, _release_dropped)
    _dropping.extend(elements)


def _release_dropped() -> bool:
    i = 0
    while i < len(_dropping):
        element = _dropping[i]
        # References: the list, the name ``element`` and getrefcount's own argument.
        if element.get_parent() is None and element.__grefcount__ == 1 and sys.getrefcount(element) <= 3:
            del _dropping[i]
        else:
            i += 1
        element = None  # the last one goes here, on the main thread
    return bool(_dropping)


class Teardown:
    """Counts the stops still running off the main loop (GStreamer's pool, a close thread), so a quit can
    wait for them: a process that exits while a program is still going to NULL tears Mesa down under
    its GL thread."""

    def __init__(self) -> None:
        self._done = threading.Condition()
        self._running: dict[str, int] = {}

    def begin(self, what: str) -> None:
        with self._done:
            self._running[what] = self._running.get(what, 0) + 1

    def end(self, what: str) -> None:
        with self._done:
            self._running[what] -= 1
            if not self._running[what]:
                del self._running[what]
            self._done.notify_all()

    def wait(self, timeout: float) -> bool:
        """Whether every stop finished within ``timeout`` seconds (the GIL is free meanwhile)."""
        with self._done:
            return self._done.wait_for(lambda: not self._running, timeout)

    def running(self) -> str:
        with self._done:
            return ", ".join(f"{what} ×{n}" for what, n in self._running.items())


def _set_prop(element: Gst.Element, name: str, value) -> None:
    if name == "caps":
        element.set_property("caps", Gst.Caps.from_string(value))
        return
    if name in ("stream-properties", "client-properties"):
        element.set_property(name, Gst.Structure.new_from_string(value))
        return
    if isinstance(value, str):
        spec = element.find_property(name)
        if spec is not None and spec.value_type.is_a(GObject.TYPE_ENUM) or (
                spec is not None and spec.value_type.is_a(GObject.TYPE_FLAGS)):
            Gst.util_set_object_arg(element, name, value)
            return
    element.set_property(name, value)


#: Properties GStreamer takes only when an element is made.
CONSTRUCT_ONLY = {"force-live"}


def source_fds(elements) -> list[int]:
    """The PipeWire connections given to ``elements``' pipewiresrc (pipewiresrc copies the fd it is given
    and never closes it: open, the connection and its portal client stay)."""
    fds = []
    for element in elements:
        factory = element.get_factory()
        if factory is not None and factory.get_name() == "pipewiresrc" and element.get_property("fd") >= 0 and \
                not element.is_locked_state():  # a branch on its way out closes its own (Engine._remove_branch)
            fds.append(element.get_property("fd"))
    return fds


def close_fds(fds: list[int]) -> None:
    """Once their sources are NULL."""
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def make(spec: Element) -> Gst.Element:
    early = {k: v for k, v in spec.props.items() if k in CONSTRUCT_ONLY}
    if early:
        element = Gst.ElementFactory.make_with_properties(spec.factory, ["name", *early], [spec.name, *early.values()])
    else:
        element = Gst.ElementFactory.make(spec.factory, spec.name)
    if element is None:
        raise EngineError(f"GStreamer has no {spec.factory} element")
    for name, value in spec.props.items():
        if name in CONSTRUCT_ONLY:
            continue
        if element.find_property(name) is None:
            log(f"{spec.factory} has no {name}; left at its default")
            continue
        _set_prop(element, name, value)
    return element


class EngineError(RuntimeError):
    pass


def link_all(elements) -> None:
    elements = list(elements)
    for a, b in zip(elements, elements[1:]):
        if not a.link(b):
            raise EngineError(f"{a.get_name()} does not link to {b.get_name()}")


def available_factories() -> set[str]:
    Gst.init(None)
    wanted = graph.H264 + graph.AV1 + graph.AAC + ("eflvmux", "lumaframe", "gtk4paintablesink")
    return {name for name in wanted if Gst.ElementFactory.find(name) is not None}


_WORKING: dict[str, bool] = {}


#: Encoders that are what they say once installed: no driver behind them and no stub package
#: (openh264's noopenh264 is one) to stand in for them. Probing SVT-AV1 would also start its
#: thread pool in this process for nothing.
_SELF_CONTAINED = ("svtav1enc",)


def encoder_works(factory: str) -> bool:
    """Whether an encoder really encodes (a stub library or a missing driver fails here)."""
    if factory in _SELF_CONTAINED:
        return True
    if factory in _WORKING:
        return _WORKING[factory]
    Gst.init(None)
    pipeline = None
    try:
        if factory.startswith("va"):
            desc = f"videotestsrc num-buffers=2 ! video/x-raw,format=NV12,width=320,height=240 ! {factory} ! fakesink"
        else:
            desc = f"videotestsrc num-buffers=2 ! video/x-raw,format=I420,width=320,height=240 ! {factory} ! fakesink"
        pipeline = Gst.parse_launch(desc)
        pipeline.set_state(Gst.State.PLAYING)
        message = pipeline.get_bus().timed_pop_filtered(5 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
        ok = message is not None and message.type == Gst.MessageType.EOS
    except GLib.Error:
        ok = False
    finally:
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)
    _WORKING[factory] = ok
    return ok


def choose_encoders(codec: str) -> EncoderChoice | None:
    """The first encoder for ``codec`` that is installed and works, hardware first."""
    available = available_factories()
    order = graph.H264 if codec == "h264" else graph.AV1
    working = {f for f in available if f not in order or encoder_works(f)}
    return graph.choose(working, codec)


#: A capture that has sent no picture in this long says so ("waiting"). It is not a failure:
#: a window cast sends nothing until the window redraws, so the stream stays open.
FIRST_PICTURE_S = 8
#: A camera whose GPU decoding stops after it showed pictures (its picture size changed) is opened on the
#: GPU again at most this often; a second stop within it moves it to software decoding.
GPU_REOPEN_S = 60
#: A change no frame has shown after this long (its source sends nothing new) is applied directly.
SETTLE_S = 2.0

#: Why a source failed: "busy", "missing" (the device or node is gone), "denied", "format"
#: (no picture format both ends take) or "stopped" (any other pipeline error).
REASONS = ("busy", "missing", "denied", "format", "stopped")


def source_problem(message: str, debug: str) -> str:
    """A source's GStreamer error as one of REASONS."""
    text = f"{message} {debug}".lower()
    if "not-negotiated" in text or "not negotiated" in text or "no common format" in text:
        return "format"  # before "busy": a negotiation failure's text can mention a device in use
    if "busy" in text or "ebusy" in text or "in use" in text:
        return "busy"
    if "target not found" in text or "no such" in text or "not found" in text or "removed" in text:
        return "missing"
    if "permission" in text or "not allowed" in text or "denied" in text:
        return "denied"
    if "not-negotiated" in text or "negotiat" in text or "no common format" in text:
        return "format"
    return "stopped"


def crop_meta(buffer: Gst.Buffer) -> tuple[int, int, int, int] | None:
    """A buffer's GstVideoCropMeta (x, y, width, height), or None. PyGObject hands the meta out as a
    plain Gst.Meta, so its fields are read where they sit: after GstMeta (flags, info), four guints."""
    import ctypes
    import re

    meta = buffer.get_meta("GstVideoCropMetaAPI")
    if meta is None:
        return None
    found = re.search(r"at (0x[0-9a-f]+)\)>?$", repr(meta))
    if found is None:
        return None
    base = int(found.group(1), 16) + 2 * ctypes.sizeof(ctypes.c_void_p)  # flags (padded) and info
    values = [ctypes.c_uint.from_address(base + 4 * i).value for i in range(4)]
    return tuple(values) if values[2] and values[3] else None


#: Formats with alpha: SHM names and DRM fourccs (DMABuf drm-format, "AR24:<modifier>").
def log(text: str) -> None:
    """One line for the journal (stderr is the app unit's journal)."""
    import sys

    print(f"streamy: {text}", file=sys.stderr, flush=True)


@dataclass
class Feed:
    """One capture (a source's pictures or sound), shared by every widget that shows it."""

    key: str
    #: The id its element names carry ("w-<id>-g<n>-…"): the source id of the widget it was opened for.
    name_id: str
    elements: list[Gst.Element]
    picture: bool = True
    #: The sound mixer's pad it feeds, and its gate (a volume element faded on the program clock).
    pad: Gst.Pad | None = None
    gate: "Animation | None" = None
    gate_on: bool = True
    #: A camera's park valve (before decoding): closed while no shown scene uses it.
    park: Gst.Element | None = None
    parked: bool = False
    #: A parked camera passes nothing (its next picture is waited for); a parked screen one picture a
    #: second (it may send nothing new for long), so a switch shows a recent one at once.
    waits: bool = False
    passed: float = 0.0
    shown: bool = False
    watchdog: int = 0
    generation: int = 0
    monitor: list = field(default_factory=list)
    monitor_device: str | None = None
    #: The source's last crop region (width, height, (x, y, w, h) or None).
    crop: tuple | None = None
    #: A picture that never changes by itself (an image, a card): the preview may rest while only these show.
    still: bool = False
    #: The device it opens (a camera: one stream at a time, see Engine._releasing) and when it started.
    device: str = ""
    started: float = 0.0
    #: Its newest picture (on the GPU, as its layers take it): a layer that starts drawing (a scene
    #: switch, a resize) draws it at once instead of waiting for the source's next frame.
    last: Gst.Buffer | None = None
    #: Out of the program: a late probe keeps no picture (its GPU memory would hold a GL context).
    closed: bool = False
    #: Its source has sent a buffer (what it sends reaches the feed; a picture may not, yet).
    sourced: bool = False
    #: The widgets drawn or heard from it (widget id -> source id).
    users: dict = field(default_factory=dict)

    @property
    def links(self) -> list[str]:
        """The sources it shows (a camera's feed can serve two sources on the same device)."""
        return sorted(set(self.users.values())) or [self.name_id]


@dataclass
class Branch:
    """A widget of one scene drawn (or heard) from its feed: a layer per canvas, placed and styled as
    that scene has it."""

    widget_id: str
    feed: Feed
    scene_id: str | None = None
    view: Widget | None = None
    zorder: int = 0
    #: canvas -> Layer
    layers: dict = field(default_factory=dict)
    #: FADE_MS once its first picture has faded in (it shows), else 0.
    fade: int = 0

    @property
    def elements(self) -> list[Gst.Element]:
        return self.feed.elements

    @property
    def picture(self) -> bool:
        return self.feed.picture

    @property
    def shown(self) -> bool:
        return self.feed.shown

    @property
    def still(self) -> bool:
        return self.feed.still

    @property
    def device(self) -> str:
        return self.feed.device

    @property
    def pad(self) -> Gst.Pad | None:
        return self.feed.pad

    @property
    def generation(self) -> int:
        return self.feed.generation

    def frame(self, fmt: str = "landscape") -> Gst.Element | None:
        layer = self.layers.get(fmt)
        return layer.frame if layer is not None else None


class Animation:
    """One property animated on the program clock: keyframes in a GstController control source bound
    to it (absolute values), sampled by its element for every frame (or sample) it produces."""

    def __init__(self, target: Gst.Object, prop: str, value: float) -> None:
        self.target, self.prop = target, prop
        self.source = GstController.InterpolationControlSource()
        self.source.set_property("mode", GstController.InterpolationMode.LINEAR)
        self.binding = GstController.DirectControlBinding.new_absolute(target, prop, self.source)
        #: The keyframes set (stream time, value), kept here too: what it is at any time, from Python.
        self.points: list[tuple[int, float]] = []
        self.hold(value)
        target.add_control_binding(self.binding)

    def value_at(self, stream_time: int) -> float:
        points = self.points
        if stream_time <= points[0][0]:
            return points[0][1]
        for (t0, v0), (t1, v1) in zip(points, points[1:]):
            if stream_time <= t1:
                return v0 + (v1 - v0) * (stream_time - t0) / max(1, t1 - t0)
        return points[-1][1]

    def hold(self, value: float) -> None:
        self.source.unset_all()
        self.points = [(0, float(value))]
        self.source.set(0, float(value))

    def ramp(self, start: int, points: list[tuple[int, float]]) -> None:
        """From the value it has at ``start`` through ``points`` (stream time, value); the last stays.
        What it was set to do before ``start`` stays (a late mixer may not have drawn it yet)."""
        start = max(0, start)
        begin = self.value_at(start)
        before = [p for p in self.points if p[0] < start][-1:]
        for when, _value in self.points:
            if (when, _value) not in before:
                self.source.unset(when)
        self.points = before + [(start, begin)] + [(max(start + 1, when), float(value)) for when, value in points]
        for when, value in self.points[len(before):]:
            self.source.set(when, value)

    def remove(self) -> None:
        self.target.remove_control_binding(self.binding)


@dataclass
class OutputStats:
    started: float = field(default_factory=time.monotonic)
    bytes: int = 0
    bitrate_kbps: float = 0.0
    dropped: int = 0
    frames: int = 0
    _window_bytes: int = 0
    _window_start: float = field(default_factory=time.monotonic)


class Output:
    """One recording or stream: appsrc ! encoders ! mux ! sink, in its own pipeline."""

    def __init__(self, engine: "Engine", kind: str, choice: EncoderChoice, sink: list[Element],
                 video_kbps: int, audio_kbps: int, with_sound: bool, fmt: str = "landscape") -> None:
        self.engine, self.kind, self.choice, self.fmt = engine, kind, choice, fmt
        self.stats = OutputStats()
        self.pipeline = Gst.Pipeline.new(f"streamy-{kind}")
        _label, width, height = FORMATS[fmt]
        self.video_src = Gst.ElementFactory.make("appsrc", f"{kind}-video")
        self.video_src.set_property("caps", Gst.Caps.from_string(
            f"video/x-raw,format=NV12,width={width},height={height},framerate={graph.FPS}/1"))
        for name, value in (("is-live", True), ("format", Gst.Format.TIME), ("do-timestamp", False),
                            ("max-buffers", graph.FPS * 2), ("block", False)):
            self.video_src.set_property(name, value)
        self.pipeline.add(self.video_src)
        video = [make(e) for e in graph.video_encoder_chain(choice, video_kbps, kind)]
        tail = [make(e) for e in sink]
        for element in video + tail:
            self.pipeline.add(element)
        self._link([self.video_src] + video)
        mux = tail[0]
        video[-1].link(mux)
        link_all(tail)
        self.sound_src = None
        if with_sound and choice.audio is not None:
            self.sound_src = Gst.ElementFactory.make("appsrc", f"{kind}-sound")
            self.sound_src.set_property("caps", Gst.Caps.from_string(
                f"audio/x-raw,format=S16LE,layout=interleaved,rate={graph.AUDIO_RATE},channels=2"))
            for name, value in (("is-live", True), ("format", Gst.Format.TIME), ("do-timestamp", False),
                                ("max-buffers", 200), ("block", False)):
                self.sound_src.set_property(name, value)
            self.pipeline.add(self.sound_src)
            sound = [make(e) for e in graph.audio_encoder_chain(choice, audio_kbps, kind)]
            for element in sound:
                self.pipeline.add(element)
            self._link([self.sound_src] + sound)
            sound[-1].link(mux)
        mux.get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._count)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_id = bus.connect("message", self._message)
        self.on_error: Callable[[str], None] | None = None
        self.on_finished: Callable[[], None] | None = None
        self.t0: int | None = None
        self.started = False
        self.stopping = False

    @staticmethod
    def _link(elements: list[Gst.Element]) -> None:
        for a, b in zip(elements, elements[1:]):
            if not a.link(b):
                raise EngineError(f"{a.get_name()} does not link to {b.get_name()}")

    def start(self, clock: Gst.Clock, base_time: int, running: int) -> None:
        # Share the program's clock; time starts at zero for this output.
        self.pipeline.use_clock(clock)
        self.pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
        self.pipeline.set_base_time(base_time + running)
        # Time starts at the first picture that arrives, not at the clock's "now": the program's
        # pictures reach the tap behind the clock, further behind on a busy machine, so a clock
        # start dropped every picture of a later output.
        self.t0 = None
        self.started = True
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise EngineError(f"the {self.kind} could not start")

    def push(self, src: Gst.Element, sample: Gst.Sample) -> None:
        if self.stopping or src is None or not self.started:
            return
        buffer = sample.get_buffer()
        if buffer.pts == Gst.CLOCK_TIME_NONE:
            return
        if self.t0 is None:
            if src is not self.video_src:
                return  # a recording starts on a picture; the sound before it is dropped
            self.t0 = buffer.pts
        if buffer.pts < self.t0:
            return
        if src is self.video_src:
            if src.get_property("current-level-buffers") >= src.get_property("max-buffers"):
                self.stats.dropped += 1  # the encoder or the network is far behind
                return
            self.stats.frames += 1
        # copy_region, not copy(): in Python copy() is the boxed copy, a ref, and setting its time
        # would change the buffer the other output receives too.
        out = buffer.copy_region(Gst.BufferCopyFlags.FLAGS | Gst.BufferCopyFlags.TIMESTAMPS |
                                 Gst.BufferCopyFlags.META | Gst.BufferCopyFlags.MEMORY, 0, buffer.get_size())
        out.pts = buffer.pts - self.t0
        out.dts = Gst.CLOCK_TIME_NONE
        src.emit("push-buffer", out)

    def _count(self, _pad, info) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        if buffer is not None:
            size = buffer.get_size()
            self.stats.bytes += size
            self.stats._window_bytes += size
            now = time.monotonic()
            span = now - self.stats._window_start
            if span >= 1.0:
                self.stats.bitrate_kbps = self.stats._window_bytes * 8 / 1000 / span
                self.stats._window_bytes = 0
                self.stats._window_start = now
        return Gst.PadProbeReturn.OK

    def stop(self) -> None:
        """Finish cleanly: EOS through the encoders and the muxer, then stop."""
        if self.stopping:
            return
        self.stopping = True
        for src in (self.video_src, self.sound_src):
            if src is not None:
                src.emit("end-of-stream")
        # A stream whose connection is gone never sees its EOS: give up after 5 s. A recording
        # waits longer, so a busy machine still drains its encoder and finishes the file.
        GLib.timeout_add_seconds(5 if self.kind == "stream" else 20, self._finish)

    def _message(self, _bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.EOS:
            self._finish()
        elif message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            if self.on_error is not None and not self.stopping:
                self.on_error(error.message)
            self._finish()

    def _finish(self) -> bool:
        if self.pipeline is None:
            return False
        bus = self.pipeline.get_bus()
        bus.disconnect(self._bus_id)
        bus.remove_signal_watch()
        sink = self.pipeline.get_by_name("record-sink")
        self.pipeline.set_state(Gst.State.NULL)
        self.pipeline = None
        if self.kind == "record" and self.stats.frames == 0 and sink is not None:
            # No picture ever reached it: a header alone is not a recording; say so, leave no file.
            path = sink.get_property("location")
            try:
                os.unlink(path)
            except OSError:
                pass
            log(f"recording {path}: no picture reached it; removed")
            self.engine.emit("problem", "Nothing was recorded: no picture reached the recording.")
        if self.on_finished is not None:
            self.on_finished()
        return False


@dataclass
class Layer:
    """One picture widget drawn on one canvas: valve ! queue ! lumaframe ! mixer-<canvas> pad. The valve
    passes pictures only while its scene is shown; its pad's alpha is animated (``alpha``)."""

    elements: list[Gst.Element]
    pad: Gst.Pad | None = None
    tee_pad: Gst.Pad | None = None
    alpha: Animation | None = None
    #: While gliding: the pad's geometry animations (xpos, ypos, width, height), the box path
    #: (from, to, start, duration, curve) and the box the lumaframe draws at.
    glide: list = field(default_factory=list)
    path: tuple | None = None
    fmt: str = "landscape"
    #: The tee feeds it only once its elements are PLAYING (Engine._add_layer); taken out before that,
    #: it never is. Guarded by ``lock`` (the link is made on GStreamer's pool, the removal on the main loop).
    lock: threading.Lock = field(default_factory=threading.Lock)
    linked: bool = False
    gone: bool = False

    @property
    def valve(self) -> Gst.Element:
        return self.elements[0]

    @property
    def frame(self) -> Gst.Element:
        return self.elements[-1]


@dataclass
class Program:
    """One canvas's compositor and what hangs off it (its preview, its encoder tap)."""

    fmt: str
    size: tuple[int, int]
    elements: list[Gst.Element]
    preview: list[Gst.Element] = field(default_factory=list)
    paintable: object = None
    preview_size: tuple[int, int] | None = None
    handler: int = 0
    composited: list = field(default_factory=list)  # (program pts, {widget id: box}), newest last
    on_screen: dict = field(default_factory=dict)
    tap: list[Gst.Element] = field(default_factory=list)
    pacer: list[Gst.Element] = field(default_factory=list)
    #: While something changes (a drag, a Look edit) the mixer reports each frame's layers and the preview
    #: sink each frame shown; idle, neither runs (no Python per frame).
    watching: bool = False
    sink_pad: Gst.Pad | None = None
    probe: int = 0

    @property
    def mixer(self) -> Gst.Element:
        return self.elements[0]

    @property
    def tee(self) -> Gst.Element:
        return self.elements[-1]


class Engine(GObject.Object):
    """The program: the active scene composited once per canvas that is on, its sound mixed, and the
    outputs. A picture is captured once and teed to a layer per canvas; a canvas's compositor exists
    only while it is shown or an output takes it (``set_canvases``)."""

    __gsignals__ = {
        # widget id, left dB, right dB
        "levels": (GObject.SignalFlags.RUN_FIRST, None, (str, float, float)),
        # a plain sentence for a toast
        "problem": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # "record" | "stream", "starting" | "on" | "reconnecting" | "off"
        "output-state": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # canvas: its preview's paintable came or went (a compositor exists only while needed)
        "preview-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # widget id: its first picture arrived
        "widget-ready": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # widget id, a plain reason: its source failed; it is out of the program
        "widget-failed": (GObject.SignalFlags.RUN_FIRST, None, (str, str)),
        # widget id: its stream is open but no picture has come yet (it stays open and may start)
        "widget-waiting": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        # widget id, width, height: the picture's own size changed (a window cast's crop region)
        "source-size": (GObject.SignalFlags.RUN_FIRST, None, (str, int, int)),
        # canvas, widget id, x, y, width, height: where that canvas's preview frame on screen now draws
        # the widget's box (canvas pixels), so an outline drawn over it never parts from the picture
        "frame-shown": (GObject.SignalFlags.RUN_FIRST, None, (str, str, float, float, float, float)),
    }

    def __init__(self, *, fake: bool | None = None, preview: bool = True,
                 sources: Callable[[Widget], tuple[int | None, str | None]] | None = None) -> None:
        super().__init__()
        Gst.init(None)
        self.fake = bool(int(os.environ.get("STREAMY_FAKE_SOURCES", "0"))) if fake is None else fake
        self.want_preview = preview
        self.sources = sources  # widget -> (pipewire fd, node) from the portals
        self.pipeline: Gst.Pipeline | None = None
        #: The canvases shown (the window's panels); outputs keep theirs running too.
        self.shown_canvases: tuple[str, ...] = ("landscape",)
        self.programs: dict[str, Program] = {}
        #: The open captures, by source key (Engine.source_key), and the widgets drawn from them, by id.
        self.feeds: dict[str, Feed] = {}
        self.branches: dict[str, Branch] = {}
        #: Every scene (the window's project): their sources stay open, ready to show. None: the shown one only.
        self.scenes_of: Callable[[], list[Scene]] | None = None
        #: How a change of scene shows: ("cut" | "fade", milliseconds).
        self.transition: tuple[str, int] = ("fade", FADE_MS)
        self._active_id: str | None = None
        self._ranks: dict[str | None, int] = {}
        self._switch_timer = 0
        #: Where the last change of scene starts on each canvas (stream time), for checks and logs.
        self.switched_at: dict[str, int] = {}
        #: Each layer's lumaframe -> its widget's id; pads placed by a glide's controllers, not by _place.
        self._frames: dict[Gst.Element, str] = {}
        self._gliding: set[Gst.Pad] = set()
        self._shown_ids: frozenset = frozenset()
        #: A change of scene waiting for parked cameras' first pictures: (previous, active, timer, probes).
        self._pending: tuple | None = None
        #: Picture widgets without a live picture show a card (graph.placeholder_chain) in their place.
        self.placeholders: dict[str, Branch] = {}
        #: ``widget`` as canvas ``fmt`` has it (its placement and Look there); the window's project knows.
        self.view_of: Callable[[Widget, str], Widget] = lambda widget, _fmt: widget
        self._placeholder_words: dict[str, str] = {}
        self.outputs: dict[str, Output] = {}
        #: An outputs fan-out (outputs.Outputs) fed from the taps: push_video(fmt, sample), push_sound(sample),
        #: wants() -> the canvases it encodes.
        self.fanout = None
        self.stream_retry: dict | None = None
        self._sound = False
        self._sound_tap: list[Gst.Element] = []
        self._zorder = 0
        self._preview_sizes: dict[str, tuple[int, int]] = {}
        self.enabled = False
        self._generation = 0
        #: Devices whose last stream is still stopping (device -> streams stopping).
        self._releasing: dict[str, int] = {}
        self._scene: Scene | None = None
        #: Atomic layer geometry: each mixer pad's geometry per lumaframe revision, applied by the
        #: mixer's samples-selected for the buffer it actually composites (see _samples_selected).
        self._geom_lock = threading.Lock()
        self._revision = 0
        self._revs: dict[Gst.Pad, dict[int, tuple]] = {}
        self._applied: dict[Gst.Pad, tuple] = {}
        self._applied_rev: dict[Gst.Pad, int] = {}
        self._pad_widget: dict[Gst.Pad, str] = {}
        #: Window casts that offered no alpha format: taken opaque (alpha first, not alpha only).
        self._no_alpha: set[str] = set()
        #: mixer pad -> the revision whose first picture drops the older ones waiting there (_supersede)
        self._superseding: dict = {}
        self._supersede_probes: set = set()
        #: Cameras the GPU couldn't decode (VA failed to negotiate): decoded in software from then on.
        self._sw_decode: set[str] = set()
        self._gpu_reopened: dict[str, float] = {}  # widget id: when its GPU decoding was last reopened
        #: Enum properties last set by nick, per (element name, property): a move never re-sets them.
        self._strings: dict[tuple[str, str], str] = {}
        self._teardown = Teardown()
        _engines.add(self)

    # ── Canvases ──

    @property
    def canvas(self) -> tuple[int, int]:
        """The first canvas shown (the size older callers and checks know)."""
        fmt = self.shown_canvases[0] if self.shown_canvases else "landscape"
        return FORMATS[fmt][1], FORMATS[fmt][2]

    @property
    def paintable(self):
        """The first shown canvas's preview (older callers and checks)."""
        return self.paintable_of(self.shown_canvases[0]) if self.shown_canvases else None

    def paintable_of(self, fmt: str):
        program = self.programs.get(fmt)
        return program.paintable if program is not None else None

    def wanted_canvases(self) -> list[str]:
        """The canvases composited: those shown, and those an output or the fan-out takes."""
        wanted = list(self.shown_canvases)
        sent = [o.fmt for o in self.outputs.values()]
        if self.fanout is not None:
            sent += list(self.fanout.wants())
        for fmt in sent:
            if fmt in FORMATS and fmt not in wanted:
                wanted.append(fmt)
        return wanted

    def set_canvases(self, shown) -> None:
        """Show these canvases (in order); a canvas neither shown nor sent stops compositing."""
        shown = tuple(f for f in shown if f in FORMATS)
        if shown == self.shown_canvases:
            return
        self.shown_canvases = shown
        if self.enabled:
            self.show_scene(self._scene, skip=self._skip, states=self._states)

    def _sync_programs(self) -> None:
        if self.pipeline is None:
            return
        wanted = self.wanted_canvases()
        for fmt in [f for f in self.programs if f not in wanted]:
            self._drop_program(fmt)
        for fmt in wanted:
            if fmt not in self.programs:
                self._add_program(fmt)

    def _add_program(self, fmt: str) -> None:
        size = (FORMATS[fmt][1], FORMATS[fmt][2])
        elements = [make(spec) for spec in graph.program(size, fmt)]
        for element in elements:
            self.pipeline.add(element)
        link_all(elements)
        program = Program(fmt, size, elements, preview_size=self._preview_sizes.get(fmt))
        mixer = program.mixer
        if mixer.find_property("emit-signals") is not None:
            program.handler = mixer.connect("samples-selected", self._samples_selected, program)
        self.programs[fmt] = program
        self._add_pacer(program)
        if self.want_preview:
            self._add_preview(program)
        # Before any layer links to it: a mixer still in NULL answers FLUSHING, and a still (imagefreeze)
        # that is answered so stops for good.
        for element in reversed(elements + program.preview + program.pacer):
            element.sync_state_with_parent()
        # Every widget already captured gets its layer here.
        for branch in list(self.branches.values()) + list(self.placeholders.values()):
            if branch.picture and branch.view is not None:
                self._add_layer(branch, fmt, branch.view, branch.zorder,
                                card=self.placeholders.get(branch.widget_id) is branch)
        self.emit("preview-changed", fmt)

    def _add_pacer(self, program: Program) -> None:
        """The canvas's mixer composites on its clock, a frame every 16 ms (graph.pacer)."""
        chain = graph.pacer(program.fmt)
        try:
            elements = [make(e) for e in chain.elements]
        except EngineError as error:
            log(f"no pacer for {program.fmt}: {error}")
            return
        for element in elements:
            self.pipeline.add(element)
        link_all(elements)
        pad = program.mixer.request_pad_simple("sink_%u")
        for name, value in chain.pad.items():
            pad.set_property(name, value)
        elements[-1].get_static_pad("src").link(pad)
        source = elements[0]

        def first(_source, _length) -> None:
            # Once, when it runs (a source not yet started drops what it is given); stamped as it is sent.
            source.disconnect(handler)
            picture = Gst.Buffer.new_wrapped(bytes(16 * 16 * 4))
            picture.duration = graph.PACER_DURATION
            source.emit("push-buffer", picture)

        handler = source.connect("need-data", first)
        program.pacer = elements

    def _drop_program(self, fmt: str) -> None:
        program = self.programs.pop(fmt, None)
        if program is None:
            return
        for branch in list(self.branches.values()) + list(self.placeholders.values()):
            self._remove_layer(branch, fmt)
        if program.probe and program.sink_pad is not None:
            program.sink_pad.remove_probe(program.probe)
            program.probe = 0
        if program.handler:
            program.mixer.set_property("emit-signals", False)
            program.mixer.disconnect(program.handler)
            program.handler = 0
        pipeline = self.pipeline
        items = program.elements + program.preview + program.tap + program.pacer
        sink = next((e for e in program.preview if e.get_name().startswith("preview-sink")), None)
        if sink is not None:
            # The preview's GTK sink stops here, on the thread its paintable belongs to.
            for element in program.preview:
                element.set_locked_state(True)
            for element in reversed(program.preview):
                element.set_state(Gst.State.NULL)
        for element in items:
            element.set_locked_state(True)

        def stop(_pipeline, elements) -> None:
            for element in elements:
                element.set_state(Gst.State.NULL)
            GLib.idle_add(lambda: [pipeline.remove(e) for e in elements if e.get_parent() is pipeline] and False)

        self._call_async(pipeline, stop, items)
        _drop_on_main(program.preview)
        program.preview, program.paintable, program.sink_pad = [], None, None
        self.emit("preview-changed", fmt)

    # ── The program pipeline ──

    def _call_async(self, pipeline: Gst.Pipeline, func, data) -> None:
        """``func(pipeline, data)`` on GStreamer's pool, counted by the teardown a quit waits for."""
        what = func.__qualname__.replace("Engine.", "").replace(".<locals>", "")
        # ``func`` and ``data`` (and everything their closures hold) are let go before the stop counts as
        # done: an element released after it would be disposed while a quit no longer waits.
        held = [func, data]

        def run(p, holder) -> None:
            try:
                call, items = holder
                holder.clear()
                call(p, items)
            finally:
                call = items = None
                self._teardown.end(what)

        self._teardown.begin(what)
        pipeline.call_async(run, held)

    def start(self, project: Project) -> None:
        """Show ``project``'s active scene. The pipeline itself exists only while something
        captures or is sent; an empty scene costs nothing."""
        if self.enabled:
            return
        self.enabled = True
        self.show_scene(project.active_scene)

    def _open(self) -> None:
        if self.pipeline is not None:
            return
        self.pipeline = Gst.Pipeline.new("streamy-program")
        if _gl_display is not None:
            self.pipeline.set_context(_gl_display)
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._message)
        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise EngineError("the preview could not start")

    def _close(self) -> None:
        if self.pipeline is None:
            return
        # Everything goes to NULL with the pipeline, in one state change, before anything is unlinked or
        # dropped: no branch stops on its own while the mixers it feeds are torn down.
        feeds = list(self.feeds.values()) + [b.feed for b in self.placeholders.values()]
        self.feeds, self.branches, self.placeholders = {}, {}, {}
        for feed in feeds:
            feed.closed, feed.last = True, None  # its GPU memory would keep a GL context alive
        self._placeholder_words.clear()
        self._revs.clear()
        self._applied_rev.clear()
        self._frames.clear()
        self._gliding.clear()
        self._active_id, self._ranks = None, {}
        if self._switch_timer:
            GLib.source_remove(self._switch_timer)
            self._switch_timer = 0
        devices = [f.device for f in feeds if f.device]
        for feed in feeds:
            if feed.watchdog:
                GLib.source_remove(feed.watchdog)
                feed.watchdog = 0
        for device in devices:
            self._releasing[device] = self._releasing.get(device, 0) + 1
        pipeline = self.pipeline
        pipeline.get_bus().remove_signal_watch()
        global _gl_display
        if _gl_display is None:
            _gl_display = pipeline.get_context("gst.gl.GLDisplay")
        programs, self.programs = self.programs, {}
        if self._watch_timer:
            GLib.source_remove(self._watch_timer)
            self._watch_timer = 0
        for program in programs.values():
            if program.probe and program.sink_pad is not None:
                program.sink_pad.remove_probe(program.probe)
                program.probe = 0
            if program.handler:
                program.mixer.set_property("emit-signals", False)
                program.mixer.disconnect(program.handler)
            for element in program.preview:
                element.set_locked_state(True)
                element.set_state(Gst.State.NULL)
            _drop_on_main(program.preview)  # the old pipeline may well go on the close thread
            program.preview, program.paintable, program.sink_pad = [], None, None
        self.pipeline = None
        # Off the window's thread: a source still stuck opening holds its state lock, and the
        # pipeline's own NULL waits on it. Nothing here needs the old pipeline any more.
        # The close thread lets the pipeline go before it counts as done; the last reference is the main
        # thread's (_drop_on_main), so the pipeline is never disposed on a thread a quit no longer waits for.
        held = [pipeline]
        inside: list = []
        pipeline.iterate_recurse().foreach(inside.append)
        fds = source_fds(inside)
        del inside
        _drop_on_main([pipeline])
        del pipeline

        def close(holder) -> None:
            try:
                old = holder.pop()
                old.set_state(Gst.State.NULL)
                close_fds(fds)
                for device in devices:
                    GLib.idle_add(self._released, device)
            finally:
                old = None
                self._teardown.end("close")

        self._teardown.begin("close")
        threading.Thread(target=close, args=(held,), name="streamy-close", daemon=True).start()
        self._sound = False
        self._sound_tap = []
        for fmt in programs:
            self.emit("preview-changed", fmt)

    def stop(self, wait: float = 0.0) -> bool:
        """Everything off: outputs finish, captures stop, the pipeline goes. The stops run off the main
        loop; with ``wait``, block up to that many seconds until every one of them (also those of
        earlier removals) has finished, and say whether they did. Quitting waits: no GStreamer or GL
        thread may still be stopping when the process exits."""
        for output in list(self.outputs.values()):
            output.stop()
        self.enabled = False
        self._close()
        return self.wait_stopped(wait) if wait > 0 else True

    def wait_stopped(self, timeout: float) -> bool:
        """Block up to ``timeout`` seconds until every stop running off the main loop has finished."""
        done = self._teardown.wait(timeout)
        if not done and timeout >= 1:
            log(f"the program did not stop within {timeout:g} s: still running {self._teardown.running()}")
            if os.environ.get("STREAMY_STOP_TRACE"):
                import faulthandler

                faulthandler.dump_traceback(all_threads=True)
        return done

    @property
    def running(self) -> bool:
        return self.enabled

    def set_canvas(self, project: Project) -> None:
        """The canvas edited changed: every canvas keeps running; nothing to rebuild."""

    def _add_preview(self, program: Program) -> None:
        fmt = program.fmt
        sink = Gst.ElementFactory.make("gtk4paintablesink", f"preview-sink-{fmt}")
        if sink is None:
            raise EngineError("GStreamer has no gtk4paintablesink (gstreamer1-plugin-gtk4)")
        program.paintable = sink.get_property("paintable")
        sink.set_property("sync", False)
        bin_ = Gst.ElementFactory.make("glsinkbin", f"preview-{fmt}")
        bin_.set_property("sink", sink)
        # A live program never waits for the preview to preroll: with an async sink, a branch
        # added later (a microphone, a camera that never sends) can leave the pipeline PAUSED
        # waiting for it, and every live source in it paused with it (meters still, PipeWire
        # nodes suspended).
        sink.set_property("async", False)
        if bin_.find_property("async") is not None:
            bin_.set_property("async", False)
        # The preview is scaled on the GPU to the size it is shown at: no full-size copy for the screen.
        # Bilinear scaling aliases past 2:1, so a large reduction goes in two steps (at most 2:1 each,
        # a box filter in effect); a small one passes the first step through.
        # Closed while nothing on the canvas moves (stills and cards only, no change under way): the screen
        # keeps its last picture and neither the scaler nor GTK draws 30 times a second for nothing.
        valve = make(Element("valve", f"preview-valve-{fmt}", {"drop": False}))
        queue = make(Element("queue", f"preview-queue-{fmt}", {"max-size-buffers": 1, "leaky": 2}))
        half = make(Element("glcolorscale", f"preview-scale-half-{fmt}"))
        half_size = make(Element("capsfilter", f"preview-size-half-{fmt}", {"caps": self._preview_caps(program, 2)}))
        scale = make(Element("glcolorscale", f"preview-scale-{fmt}"))
        size = make(Element("capsfilter", f"preview-size-{fmt}", {"caps": self._preview_caps(program)}))
        program.preview = [valve, queue, half, half_size, scale, size, bin_]
        for element in program.preview:
            self.pipeline.add(element)
        link_all(program.preview)
        program.tee.link(valve)
        program.sink_pad = sink.get_static_pad("sink")

    @staticmethod
    def _preview_caps(program: Program, factor: int = 1) -> str:
        cw, ch = program.size
        width, height = program.preview_size or (cw // 2, ch // 2)
        width, height = min(cw, width * factor), min(ch, height * factor)
        return f"video/x-raw(memory:GLMemory),width={width},height={height}"

    def set_preview_size(self, width: int, height: int = 0, fmt: str | None = None) -> None:
        """Show canvas ``fmt``'s program at this many pixels wide (its size on screen, in device pixels)."""
        fmt = fmt or (self.shown_canvases[0] if self.shown_canvases else "landscape")
        _label, cw, ch = FORMATS[fmt]
        width = max(2, min(cw, int(width) // 2 * 2))
        height = max(2, min(ch, int(round(width * ch / cw)) // 2 * 2))
        if self._preview_sizes.get(fmt) == (width, height):
            return
        self._preview_sizes[fmt] = (width, height)
        program = self.programs.get(fmt)
        if program is None:
            return
        program.preview_size = (width, height)
        for element, factor in ((program.preview[3] if program.preview else None, 2),
                                (program.preview[5] if program.preview else None, 1)):
            if element is not None:
                element.set_property("caps", Gst.Caps.from_string(self._preview_caps(program, factor)))

    # ── Scenes and widgets ──

    _skip: set[str] | frozenset = frozenset()
    _states: dict[str, str] | None = None

    def show_scene(self, scene: Scene | None, *, skip: set[str] | frozenset = frozenset(),
                   states: dict[str, str] | None = None) -> None:
        """Show ``scene``: its visible widgets draw (less ``skip``: widgets or sources that failed).

        Every other scene's visible widgets stay ready (``scenes_of``): their sources open and captured
        once, shared with any scene that shows them, their layers in place but closed and transparent.
        A change of scene is then only a change of which layers draw (``transition``), never a reopen.

        A visible picture widget of the shown scene with no live picture (connecting, failed, nothing
        chosen: ``states`` says which, by widget or source id, "connecting" by default) shows its
        placeholder card through its own lumaframe, so its Look shows. The card is the preview's only:
        while recording or live it is transparent (its pad's alpha 0), so viewers never see it."""
        if not self.enabled:
            return
        self._scene, self._skip, self._states = scene, set(skip), dict(states or {})
        states = states or {}
        active = scene.id if scene is not None else None
        scenes = [scene] if scene is not None else []
        if self.scenes_of is not None:
            scenes += [s for s in self.scenes_of() if s is not None and s.id != active]
        composited = bool(self.wanted_canvases())

        def skipped(w: Widget) -> bool:
            return w.id in skip or w.link in skip

        # No canvas composited: pictures capture nothing (the sound still runs, for its meters).
        wanted: dict[str, tuple[str, int, Widget]] = {}
        for s in scenes:
            for zorder, w in enumerate(s.widgets):
                if w.visible and not skipped(w) and (composited or not w.has_picture) and w.id not in wanted:
                    wanted[w.id] = (s.id, zorder, w)
        shown = [w for w in (scene.widgets if scene else []) if w.visible]
        if not wanted and not self._sent_canvases() and not (composited and shown):
            self._close()  # nothing to show and nothing sent: no pipeline at all
            return
        self._open()
        # One feed per source key; a device (a camera) takes one mode at a time: the shown scene's wins.
        keys: dict[str, str] = {}
        devices: dict[str, str] = {}
        for widget_id, (_sid, _z, w) in wanted.items():
            key = self.source_key(w)
            device = self._device(w)
            if device and devices.setdefault(device, key) != key:
                continue
            keys[widget_id] = key
        # Widgets leave (and feeds nobody uses close) before the compositors they feed change.
        for widget_id in [i for i, b in self.branches.items() if keys.get(i) != b.feed.key]:
            self._remove_branch(widget_id)
        self._sync_programs()
        previous, switching = self._active_id, self._active_id != active
        if switching:
            self._ranks = {active: 2, previous: 1} if previous is not None else {active: 2}
        self._active_id = active
        self._shown_ids = frozenset(w.id for w in (scene.widgets if scene else []))
        for widget_id, key in keys.items():
            sid, zorder, w = wanted[widget_id]
            branch = self.branches.get(widget_id)
            if branch is not None:
                branch.scene_id = sid
                self.update_widget(w, zorder)
            else:
                self._add_branch(w, zorder, sid, key)
        pending = self._pending
        if switching and pending is not None:
            previous = pending[0]  # still on screen: the change waiting for its pictures never started
            self._cancel_pending()
        if switching and previous is not None:
            self._begin_switch(previous, active)
        elif self._pending is None:
            self._sync_shown()
        # Placeholders: every shown picture widget of this scene whose source has no picture yet.
        cards = {}
        for zorder, widget in enumerate(scene.widgets if scene else []):
            if not composited or not widget.visible or not widget.has_picture:
                continue
            branch = self.branches.get(widget.id)
            if branch is not None and branch.shown:
                continue
            state = states.get(widget.id) or states.get(widget.link) or (
                "failed" if skipped(widget) else "connecting" if branch else "choose")
            cards[widget.id] = (widget, zorder, graph.placeholder_words(widget, state))
        for widget_id in [i for i in self.placeholders if i not in cards or
                          self._placeholder_words.get(i) != cards[i][2]]:
            self._remove_placeholder(widget_id)
        for widget_id, (widget, zorder, words) in cards.items():
            if widget_id in self.placeholders:
                self._update_placeholder(widget, zorder)
            else:
                self._add_placeholder(widget, zorder, words)
        self._sync_sound()
        self._keep_playing()
        self._idle_check()

    def _keep_playing(self) -> None:
        """The program runs whenever it exists: anything that left it short of PLAYING is undone.

        A source that fails while it starts (a camera that is gone) fails the pipeline's own state
        change, which then stays PAUSED with PLAYING still pending, and every live source in it
        pauses with it: a microphone's meter stops and its PipeWire node suspends. Asking again,
        once the failed branch is out, completes the change."""
        if self.pipeline is None:
            return
        _ok, state, _pending = self.pipeline.get_state(0)
        if state != Gst.State.PLAYING:
            # On GStreamer's pool: a source still opening (a stuck device) never holds the window.
            self._call_async(self.pipeline, lambda pipeline, _data: pipeline.set_state(Gst.State.PLAYING), None)

    def _idle_check(self) -> None:
        """Nothing captures, shows or is sent: the program goes, so it costs nothing."""
        def idle() -> bool:
            return not self.feeds and not self._sent_canvases() and not self.placeholders and not self.programs

        if self.pipeline is not None and idle():
            GLib.idle_add(lambda: (self._close() if idle() else None) and False)


    def _on(self, branch: Branch) -> bool:
        """Its scene is the one shown (a card always is)."""
        return branch.scene_id is None or branch.scene_id == self._active_id


    def _z(self, branch: Branch, zorder: int | None = None) -> int:
        """Its mixer pads' stacking: the shown scene above the one it replaces, above the rest."""
        index = branch.zorder if zorder is None else zorder
        return self._ranks.get(branch.scene_id, 0 if branch.scene_id is not None else 2) * ZSTEP + index + 1

    # ── Layers: a picture on each canvas ──

    def _add_layer(self, branch: Branch, fmt: str, widget: Widget, zorder: int, *, card: bool = False) -> None:
        """Draw ``branch``'s picture on canvas ``fmt`` (placed and styled as that canvas has it)."""
        program = self.programs.get(fmt)
        if program is None or fmt in branch.layers or self.pipeline is None:
            return
        view = self.view_of(widget, fmt)
        self._generation += 1
        prefix = f"{'p' if card else 'l'}-{branch.widget_id}-g{self._generation}"
        chain = graph.layer_chain(view, self._z(branch, zorder), prefix, fmt)
        elements = [make(e) for e in chain.elements]
        on = self._on(branch)
        elements[0].set_property("drop", not on)
        for element in elements:
            self.pipeline.add(element)
        link_all(elements)
        tee = branch.feed.elements[-1]
        tee_pad = tee.request_pad_simple("src_%u")
        pad = program.mixer.request_pad_simple("sink_%u")
        for name, value in chain.pad.items():
            pad.set_property(name, value)
        layer = Layer(elements, pad, tee_pad, fmt=fmt)
        if card:
            pad.set_property("alpha", 0.0 if self.outputs else 1.0)
        else:
            layer.alpha = Animation(pad, "alpha", 1.0 if on and branch.fade >= FADE_MS else 0.0)
        if elements[-1].get_static_pad("src").link(pad) != Gst.PadLinkReturn.OK:
            log(f"widget {branch.widget_id}: its layer does not link to {fmt}'s mixer")
        self._frames[elements[-1]] = branch.widget_id
        self._place(elements[-1], pad, view, chain.pad, True)
        branch.layers[fmt] = layer
        feed = branch.feed
        if feed.crop is not None and feed.crop[2] is not None:
            width, height, (x, y, w, h) = feed.crop
            x, y = min(x, width - 1), min(y, height - 1)
            self._crop_layer(branch, layer, (x, y, min(w, width - x), min(h, height - y)))

        def started(_p, items) -> None:
            for element in reversed(items):
                element.sync_state_with_parent()
            # Only now does the tee feed it. Linked while still NULL, it answered the tee FLUSHING, and a
            # picture that reached it before its stream's events stopped the source (not negotiated).
            # The tee's new pad sends the stream's sticky events (stream-start, caps, segment) ahead of
            # the first picture it pushes.
            with layer.lock:
                if layer.gone:
                    return
                if tee_pad.link(items[0].get_static_pad("sink")) != Gst.PadLinkReturn.OK:
                    log(f"widget {branch.widget_id}: its layer does not link to its source on {fmt}")
                    return
                layer.linked = True
                queue = items[1].get_static_pad("sink")
                if not self._negotiated(queue):
                    # Drawn once as soon as its stream's events are in (the first picture through the tee
                    # brings them, also past a closed valve): its shaders are built before it is shown.
                    queue.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self._primed, (branch, layer))
            # Its source's newest picture at once: a layer of the shown scene draws it on the next frame,
            # one of another scene is ready (negotiated, its shaders built) for when that scene shows.
            GLib.idle_add(lambda: self._push_last(branch, layer) and False)

        self._call_async(self.pipeline, started, elements)


    def _push_last(self, branch: Branch, layer: Layer) -> None:
        """Draw the feed's newest picture through ``layer`` now, stamped now."""
        feed = branch.feed
        if feed.last is None or feed.closed or self.pipeline is None or branch.layers.get(layer.fmt) is not layer:
            return
        clock = self.pipeline.get_clock()
        if clock is None:
            return
        sink = layer.elements[1].get_static_pad("sink")
        if not self._negotiated(sink):
            return  # its stream's events haven't reached it yet: its first picture comes from the source
        picture = feed.last.copy()  # the same GPU memory, its own timestamps
        picture.pts = self._stamp(layer.fmt)
        picture.dts = Gst.CLOCK_TIME_NONE
        # Pushed on this thread: the pad's stream lock keeps it in turn with the source's own pictures.
        sink.chain(picture)

    def _stamp(self, fmt: str) -> int:
        """The time a picture pushed now is due on canvas ``fmt``: now, or the frame a late mixer makes next
        (stamped by the clock, it would wait for the mixer to catch up and miss the frame it was for)."""
        running = self.pipeline.get_clock().get_time() - self.pipeline.get_base_time()
        program = self.programs.get(fmt)
        made = self._made(program) if program is not None else None
        # A late mixer's next frames start at ``made``: due a lead after it, the picture lands on the frame
        # a switch starts on (_times), and the source's own, stamped by the clock, follow it in order.
        return made + LEAD_NS if made is not None and made + LEAD_NS < running else running

    def _primed(self, _pad: Gst.Pad, info: Gst.PadProbeInfo, data) -> Gst.PadProbeReturn:
        event = info.get_event()
        if event is None or event.type != Gst.EventType.SEGMENT:
            return Gst.PadProbeReturn.OK
        branch, layer = data
        GLib.idle_add(lambda: self._push_last(branch, layer) and False)
        return Gst.PadProbeReturn.REMOVE

    @staticmethod
    def _negotiated(pad: Gst.Pad) -> bool:
        """``pad`` has had its stream's caps and segment: a picture may be pushed into it."""
        return pad.get_current_caps() is not None and pad.get_sticky_event(Gst.EventType.SEGMENT, 0) is not None

    def _remove_layer(self, branch: Branch, fmt: str) -> None:
        layer = branch.layers.pop(fmt, None)
        if layer is None or self.pipeline is None:
            return
        with layer.lock:
            layer.gone = True
            linked = layer.linked
        if not linked and layer.tee_pad is not None:
            # Never fed: its tee pad goes now (the pool's link, if it runs later, sees it gone).
            tee = layer.tee_pad.get_parent_element()
            if tee is not None:
                tee.release_request_pad(layer.tee_pad)
        self._end_glide(layer)
        self._frames.pop(layer.frame, None)
        if layer.alpha is not None:
            layer.alpha.hold(0.0)  # hidden now: a mixer keeps compositing a pad's last frame until it goes
        self._forget_pad(layer.pad)
        self._stop_then_unlink(layer.elements, layer.elements[0].get_static_pad("sink"),
                               layer.frame.get_static_pad("src") if layer.pad is not None else None)

    def _style_layer(self, layer: Layer, view: Widget, zorder: int, *, alpha: float | None = None) -> None:
        """Move, resize or restyle one layer live (no renegotiation unless its size changed)."""
        frame = layer.frame
        props = graph.frame_props(view)
        changed = False
        for name, value in props.items():
            if frame.find_property(name) is None:
                continue  # an older lumaframe (no stroke-position): its default
            if isinstance(value, str):
                if self._strings.get((frame.get_name(), name)) != value:
                    self._strings[(frame.get_name(), name)] = value
                    _set_prop(frame, name, value)
                    changed = True
            elif frame.get_property(name) != value:
                frame.set_property(name, value)
                changed = True
        pad_props = graph.mixer_pad_props(view, zorder)
        if alpha is not None:
            pad_props["alpha"] = alpha
        self._place(frame, layer.pad, view, pad_props, changed)

    # ── Placeholders ──

    def _add_placeholder(self, widget: Widget, zorder: int, words: str) -> None:
        self._generation += 1
        chain = graph.placeholder_chain(widget, zorder, words, self._generation)
        try:
            elements = [make(e) for e in chain.elements]
        except EngineError as error:
            log(f"no placeholder for {widget.id}: {error}")
            return
        for element in elements:
            self.pipeline.add(element)
        link_all(elements)
        feed = Feed(f"card:{widget.id}", widget.id, elements, True, shown=True, generation=self._generation,
                    still=True)
        card = Branch(widget.id, feed, None, widget, zorder, fade=FADE_MS)
        self.placeholders[widget.id] = card
        self._placeholder_words[widget.id] = words
        for fmt in self.programs:
            self._add_layer(card, fmt, widget, zorder, card=True)
        self._call_async(self.pipeline, lambda _p, items: [e.sync_state_with_parent() for e in reversed(items)], elements)

    def _update_placeholder(self, widget: Widget, zorder: int | None = None) -> None:
        card = self.placeholders.get(widget.id)
        if card is None:
            return
        card.view = widget
        if zorder is not None:
            card.zorder = zorder
        for fmt, layer in card.layers.items():
            self._style_layer(layer, self.view_of(widget, fmt), self._z(card), alpha=0.0 if self.outputs else 1.0)

    def _remove_placeholder(self, widget_id: str) -> None:
        card = self.placeholders.pop(widget_id, None)
        self._placeholder_words.pop(widget_id, None)
        if card is None:
            return
        for fmt in list(card.layers):
            self._remove_layer(card, fmt)
        self._drop_feed(card.feed)

    def _sync_placeholder_alpha(self) -> None:
        """Sending or not: the cards are the preview's only."""
        for card in self.placeholders.values():
            for layer in card.layers.values():
                if layer.pad is not None:
                    layer.pad.set_property("alpha", 0.0 if self.outputs else 1.0)

    def update_widget(self, widget: Widget, zorder: int | None = None) -> None:
        """Move, resize or restyle a widget live on every canvas it is drawn on (its source's settings, a
        camera's mirror or a sound's gain, reach every widget of that source)."""
        self._update_placeholder(widget, zorder)
        branch = self.branches.get(widget.id)
        if branch is None:
            return
        branch.view = widget
        if zorder is not None:
            branch.zorder = zorder
        feed = branch.feed
        if branch.picture:
            for fmt, layer in branch.layers.items():
                if layer.glide:
                    continue  # it lands where it now is when its glide ends (_end_glide)
                self._style_layer(layer, self.view_of(widget, fmt), self._z(branch))
            flip = next((e for e in feed.elements if e.get_factory().get_name() == "glvideoflip"), None)
            if flip is not None:
                Gst.util_set_object_arg(flip, "method", graph.flip_method(widget))
        else:
            volume = next(e for e in feed.elements if e.get_name().endswith("-volume"))
            for name, value in graph.volume_props(widget).items():
                volume.set_property(name, value)
            if feed.pad is not None:
                feed.pad.set_property("mute", widget.monitor == "only")
            if self._feed_on(feed):
                self._sync_monitor(feed, widget)

    def restack(self, scene: Scene) -> None:
        for zorder, widget in enumerate(scene.widgets):
            for branch in (self.placeholders.get(widget.id), self.branches.get(widget.id)):
                if branch is None or not branch.picture:
                    continue
                branch.zorder = zorder
                for layer in branch.layers.values():
                    if layer.pad is not None:
                        layer.pad.set_property("zorder", self._z(branch))


    def _restack_all(self) -> None:
        for branch in list(self.branches.values()) + list(self.placeholders.values()):
            for layer in branch.layers.values():
                if layer.pad is not None and layer.pad.get_property("zorder") != self._z(branch):
                    layer.pad.set_property("zorder", self._z(branch))

    @staticmethod
    def device_of(widget: Widget) -> str:
        """The device a widget's capture holds alone: a camera (its PipeWire node takes one format at a
        time, so a stream asking a new mode while the last one still holds it gets no picture)."""
        if widget.kind == "camera":
            return f"camera:{widget.source.get('camera') or widget.link}"
        return ""


    def _device(self, widget: Widget) -> str:
        return "" if self.fake and not os.environ.get("STREAMY_FAKE_DEVICES") else self.device_of(widget)


    @staticmethod
    def source_key(widget: Widget) -> str:
        """What a feed is shared by: a camera by its device and the picture asked of it (any widget of
        any source on it shares the one stream), anything else by its source."""
        if widget.kind == "camera" and widget.source.get("camera"):
            import json

            # The mirror is drawn after the stream (glvideoflip), so it never reopens the camera.
            asked = {k: widget.source.get(k) for k in ("mode", "scaled", "fake-caps", "fake-image")}
            return f"camera:{widget.source['camera']}:{json.dumps(asked, sort_keys=True)}"
        return f"source:{widget.link}"

    def _add_branch(self, widget: Widget, zorder: int, scene_id: str | None = None, key: str | None = None) -> None:
        key = key or self.source_key(widget)
        feed = self.feeds.get(key) or self._open_feed(widget, key)
        if feed is None:
            return
        branch = Branch(widget.id, feed, scene_id, widget, zorder, fade=FADE_MS if feed.shown else 0)
        feed.users[widget.id] = widget.link
        self.branches[widget.id] = branch
        if feed.picture:
            for fmt in self.programs:
                self._add_layer(branch, fmt, widget, zorder)
        elif self._feed_on(feed):
            self._sync_monitor(feed, widget)


    def _open_feed(self, widget: Widget, key: str) -> Feed | None:
        """Open ``widget``'s source: its capture, once, for every widget that shows it."""
        device = self._device(widget)
        if device and self._releasing.get(device):
            # The last stream on this device is still stopping: this one opens once it has let go
            # (Engine._released shows the scene again), never beside it.
            return None
        fd = node = None
        if not self.fake and self.sources is not None and widget.kind in ("display", "window", "camera", "app-audio"):
            fd, node = self.sources(widget)
            if fd is None:
                return None  # not chosen yet: the window asks the portal first
        self._generation += 1
        named = Widget(widget.link, widget.kind, shared=widget.shared)  # its elements carry the source's id
        if widget.has_picture:
            hw = widget.kind == "camera" and (not self.fake or bool(widget.source.get("fake-caps"))) and \
                key not in self._sw_decode and graph.hardware_decode()
            chain = graph.picture_chain(named, 0, fake=self.fake, fd=fd, node=node, generation=self._generation,
                                        hw_decode=hw)
        else:
            chain = graph.sound_chain(named, fake=self.fake, fd=fd, node=node, generation=self._generation)
            self._ensure_sound()
        elements = [make(e) for e in chain.elements]
        for element in elements:
            self.pipeline.add(element)
        self._link_chain(elements)
        feed = Feed(key, widget.link, elements, widget.has_picture, generation=self._generation, device=device,
                    started=time.monotonic())
        feed.still = graph.is_still(widget, self.fake)
        feed.park = next((e for e in elements if e.get_name().endswith("-park")), None)
        feed.waits = widget.kind == "camera"
        if feed.park is not None and not feed.waits:
            feed.park.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._trickle, weakref.ref(feed))
        if not widget.has_picture:
            target = self.pipeline.get_by_name(chain.target)
            pad = target.request_pad_simple("sink_%u")
            for name, value in chain.pad.items():
                pad.set_property(name, value)
            if elements[-1].get_static_pad("src").link(pad) != Gst.PadLinkReturn.OK:
                target.release_request_pad(pad)
                for element in elements:
                    self.pipeline.remove(element)
                log(f"widget {widget.id} failed: its branch does not link to the mixer")
                self.emit("widget-failed", widget.link, "stopped")
                return None
            feed.pad = pad
            gate = next(e for e in elements if e.get_name().endswith("-gate"))
            feed.gate_on = self._uses_shown(widget)
            feed.gate = Animation(gate, "volume", 1.0 if feed.gate_on else 0.0)
        self.feeds[key] = feed
        if widget.has_picture:
            # Probes hold their feed weakly: a feed held by a probe on its own pads is a cycle through
            # GStreamer the collector never sees, and its elements (and their GL objects) would never go.
            elements[-1].get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._first_picture,
                                                          weakref.ref(feed))
            elements[-1].get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, self._keep_last,
                                                          weakref.ref(feed))
        # State changes run on GStreamer's thread pool: a source that takes its time
        # (a camera opening, a portal stream negotiating) never holds up the window.
        self._call_async(self.pipeline, lambda _pipeline, items: [e.sync_state_with_parent() for e in reversed(items)],
                         elements)
        if widget.has_picture:
            feed.watchdog = GLib.timeout_add_seconds(FIRST_PICTURE_S, self._no_picture, feed)
        if not self.fake and widget.kind in ("display", "window", "camera"):
            elements[0].get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, self._source_buffer,
                                                        {"feed": weakref.ref(feed), "seen": None})
        alpha = next((e for e in elements if e.get_name().endswith("-alpha")), None)
        if alpha is not None:
            strict = key not in self._no_alpha
            elements[0].get_static_pad("src").add_probe(Gst.PadProbeType.QUERY_DOWNSTREAM, self._alpha_first,
                                                        (alpha, strict, widget.link))
        if os.environ.get("STREAMY_TRACE"):
            self._trace(feed, elements)
        return feed


    def _uses_shown(self, widget: Widget) -> bool:
        scene = self._scene
        return scene is not None and any(w.link == widget.link for w in scene.widgets if w.visible)


    def _feed_on(self, feed: Feed) -> bool:
        """A widget of the shown scene uses it."""
        return any(self._on(self.branches[i]) for i in feed.users if i in self.branches)

    @staticmethod
    def _keep_last(_pad: Gst.Pad, info: Gst.PadProbeInfo, ref) -> Gst.PadProbeReturn:
        feed = ref()
        if feed is not None and not feed.closed:
            feed.last = info.get_buffer()
        return Gst.PadProbeReturn.OK

    def _redraw(self, frame: Gst.Element) -> None:
        """``frame`` committed a new revision: its layer draws the source's newest picture again now,
        stamped now, so the change shows on the next canvas frame (the source may send none for a while)."""
        sink = frame.get_static_pad("sink")
        peer = sink.get_peer() if sink is not None else None
        queue = peer.get_parent_element() if peer is not None else None
        branch = self.branches.get(self._frames.get(frame, ""))
        if queue is None or branch is None or branch.feed.last is None or branch.still or self.pipeline is None:
            return
        if not self._negotiated(queue.get_static_pad("sink")):
            return  # a layer not fed yet: its first picture (with this revision) comes from the source
        clock = self.pipeline.get_clock()
        if clock is None:
            return
        picture = branch.feed.last.copy()  # the same GPU memory, its own timestamps
        picture.pts = clock.get_time() - self.pipeline.get_base_time()
        picture.dts = Gst.CLOCK_TIME_NONE
        self._supersede(frame)
        # Pushed on this thread: the pad's stream lock keeps it in turn with the source's own pictures.
        queue.get_static_pad("sink").chain(picture)

    def _supersede(self, frame: Gst.Element) -> None:
        """The first picture drawn at ``frame``'s new revision replaces the older ones still waiting at
        its mixer pad: the mixer takes it on its next frame instead of one source frame later."""
        src = frame.get_static_pad("src")
        pad = src.get_peer() if src is not None else None
        if pad is None or not hasattr(pad, "drop_buffer") or os.environ.get("STREAMY_NO_SUPERSEDE"):
            return
        with self._geom_lock:
            self._superseding[pad] = frame.get_property("revision")
            if pad in self._supersede_probes:
                return
            self._supersede_probes.add(pad)
        # One probe per mixer pad for its life, never removed from a streaming thread: GStreamer frees a
        # removed probe's Python closure while that thread holds the pad's lock, and the mixer's thread,
        # holding the GIL in _samples_selected, takes the same lock to set the pad's geometry (a deadlock).
        pad.add_probe(Gst.PadProbeType.BUFFER, self._superseded, None)

    def _superseded(self, pad: Gst.Pad, info: Gst.PadProbeInfo, _data) -> Gst.PadProbeReturn:
        buffer = info.get_buffer()
        with self._geom_lock:
            revision = self._superseding.get(pad)
            if buffer is None or revision is None or buffer.offset < revision:
                return Gst.PadProbeReturn.OK
            del self._superseding[pad]
        while True:
            waiting = pad.peek_buffer()
            if waiting is None or waiting.offset >= revision or not pad.drop_buffer():
                break
        return Gst.PadProbeReturn.OK

    def _source_buffer(self, pad: Gst.Pad, info: Gst.PadProbeInfo, state: dict) -> Gst.PadProbeReturn:
        """What a capture actually sends: its caps and, for a window cast, the crop region the window
        is in. The first is logged; a crop that changes (the window resized) is applied and reported."""
        feed = state["feed"]()
        if feed is None:
            return Gst.PadProbeReturn.OK
        feed.sourced = True
        buffer = info.get_buffer()
        caps = pad.get_current_caps()
        if buffer is None or caps is None:
            return Gst.PadProbeReturn.OK
        state["n"] = state.get("n", 0) + 1
        window = feed.elements[0].get_name().startswith("w-window") or any(
            e.get_name().endswith("-alpha") for e in feed.elements)
        if state["seen"] is not None:
            if not window:
                return Gst.PadProbeReturn.REMOVE  # only a window's crop region changes as it goes
            if state["n"] % 10:
                return Gst.PadProbeReturn.OK  # a window resized shows within a third of a second
        st = caps.get_structure(0)
        width, height = st.get_value("width"), st.get_value("height")
        crop = crop_meta(buffer)
        seen = (width, height, crop)
        if seen != state["seen"]:
            state["seen"] = seen
            if state.get("logged") is None:
                state["logged"] = True
                log(f"source {feed.name_id}: {caps.to_string()[:160]}; crop meta {crop or 'none'}")
                if window:
                    offered = pad.peer_query_caps(None)
                    log(f"source {feed.name_id}: downstream took {offered.to_string()[:600] if offered else '?'}")
            GLib.idle_add(self._apply_crop, feed, width, height, crop)
        return Gst.PadProbeReturn.OK

    @staticmethod
    def _alpha_first(_pad: Gst.Pad, info: Gst.PadProbeInfo, data) -> Gst.PadProbeReturn:
        """A window cast asks what it may send: before the query passes, the capsfilter after the
        source takes what downstream accepts cut to the alpha formats (``strict``; a window's rounded
        corners and shadow room stay transparent), or ordered alpha first once a cast without any
        alpha format failed that way."""
        capsfilter, strict, widget_id = data
        query = info.get_query()
        if query is None or query.type != Gst.QueryType.CAPS or not capsfilter.get_property("caps").is_any():
            return Gst.PadProbeReturn.OK
        ask = Gst.Query.new_caps(query.parse_caps())
        if not capsfilter.get_static_pad("src").peer_query(ask):
            return Gst.PadProbeReturn.OK
        caps = ask.parse_caps_result()
        if caps is None or caps.is_any() or caps.is_empty():
            return Gst.PadProbeReturn.OK
        text = caps.to_string()
        chosen = only_alpha(text) if strict else ""
        if strict and not chosen:
            log(f"widget {widget_id}: downstream takes no alpha format; the window is opaque")
        chosen = chosen or prefer_alpha(text)
        if chosen != text:
            capsfilter.set_property("caps", Gst.Caps.from_string(chosen))
        return Gst.PadProbeReturn.OK

    def _apply_crop(self, feed: Feed, width: int, height: int, crop) -> bool:
        """A window cast's buffers are the size of the cast and the window a crop region inside them
        (GstVideoCropMeta, often on DMABuf the GL upload takes as it is). The region is the picture:
        lumaframe draws only it (content-x/y/width/height), on the GPU, and it is the widget's shape."""
        if self.feeds.get(feed.key) is not feed:
            return False
        feed.crop = (width, height, crop)
        size = (width, height)
        if crop is not None:
            x, y, w, h = crop
            x, y = min(x, width - 1), min(y, height - 1)
            w, h = min(w, width - x), min(h, height - y)
            for branch in self._branches_of(feed):
                for layer in branch.layers.values():
                    self._crop_layer(branch, layer, (x, y, w, h))
            size = (w, h)
        for link in feed.links:
            self.emit("source-size", link, *size)
        return False


    def _branches_of(self, feed: Feed) -> list[Branch]:
        return [self.branches[i] for i in feed.users if i in self.branches and self.branches[i].feed is feed]

    def _crop_layer(self, branch: Branch, layer: Layer, rect) -> None:
        frame = layer.frame
        if frame.find_property("content-width") is None:
            log(f"widget {branch.widget_id}: this lumaframe has no content rect; the crop region is not applied")
            return
        changed = False
        for name, value in zip(("content-x", "content-y", "content-width", "content-height"), rect):
            if frame.get_property(name) != value:
                frame.set_property(name, type(frame.get_property(name))(value))
                changed = True
        if changed:
            self._commit(frame, layer.pad)

    # ── Atomic layer geometry ──
    #
    # A lumaframe layer's size includes its shadow margin, so a Look change resizes the buffer and moves
    # its mixer pad (xpos = x - margin). Set from here, the two would land on different frames and the
    # picture would jump. Instead every lumaframe change is committed as one revision, stamped on the
    # buffers drawn with it, and the mixer pad takes the geometry of the revision of the buffer the
    # mixer is about to composite. A plain move (same revision) applies on the next program frame.

    _GEOMETRY = ("xpos", "ypos", "width", "height")

    def _place(self, frame: Gst.Element, pad: Gst.Pad | None, widget: Widget, pad_props: dict,
               changed: bool) -> None:
        """Commit ``frame``'s pending changes (``changed``) and where its pad draws it."""
        if pad is None:
            return
        self._watch()
        for name, value in pad_props.items():
            if name not in self._GEOMETRY and pad.get_property(name) != value:
                pad.set_property(name, value)
        geometry = tuple(pad_props[k] for k in self._GEOMETRY)
        box = (float(widget.x), float(widget.y), float(widget.width), float(widget.height))
        if pad not in self._applied_rev:
            # Never composited yet: it takes its geometry now (no frame of it to keep in step with).
            self._set_geometry(pad, geometry)
        if frame.find_property("revision") is None:  # an older lumaframe: as it was, not atomic
            self._set_geometry(pad, geometry)
            return
        with self._geom_lock:
            self._pad_widget[pad] = self._frames.get(frame, widget.id)
            revs = self._revs.setdefault(pad, {})
            if not changed and revs:
                latest = max(revs)
                revs[latest] = (geometry, box)  # same layer, moved: the next frame shows it
                if self._applied_rev.get(pad) != latest:
                    return  # a change is on its way: the mixer applies the move with it
                self._applied[pad] = geometry
            else:
                geometry = None
        if geometry is not None:
            # The mixer already composites this revision: move the pad now, as a plain move.
            self._set_geometry(pad, revs[latest][0])
            return
        with self._geom_lock:
            self._revision += 1
            revision = self._revision
            revs[revision] = (tuple(pad_props[k] for k in self._GEOMETRY), box)
        frame.set_property("revision", revision)
        self._redraw(frame)

    _last_change = 0.0
    _watch_timer = 0

    def _watch(self) -> None:
        """Something changes: every canvas reports which revision each frame shows, until it settles."""
        self._last_change = time.monotonic()
        self._preview_flow(True)
        for program in self.programs.values():
            if program.watching:
                continue
            program.watching = True
            if program.handler:
                program.mixer.set_property("emit-signals", True)
            if program.sink_pad is not None and not program.probe:
                program.probe = program.sink_pad.add_probe(Gst.PadProbeType.BUFFER, self._preview_frame, program)
        if not self._watch_timer:
            self._watch_timer = GLib.timeout_add(500, self._settle)

    def _moving(self) -> bool:
        """Something on the canvases changes by itself: a live capture (not a still) with a picture, shown."""
        return any(b.picture and b.layers and not b.still and self._on(b) for b in self.branches.values())

    def _preview_flow(self, on: bool) -> None:
        for program in self.programs.values():
            if program.preview and program.preview[0].get_property("drop") == on:
                program.preview[0].set_property("drop", not on)

    def _settle(self) -> bool:
        """Nothing changed for a second and every pad shows its latest revision: stop watching."""
        quiet = time.monotonic() - self._last_change
        if quiet < 1.0:
            return True
        with self._geom_lock:
            # A pad never composited (its source hasn't sent yet) can't be pending: its first buffer
            # carries the newest revision.
            pending = [(pad, max(revs)) for pad, revs in self._revs.items()
                       if revs and pad in self._applied_rev and self._applied_rev[pad] != max(revs)]
        if pending and quiet < SETTLE_S:
            return True
        for pad, latest in pending:
            # Its source sends nothing new (a still window, a paused share): no frame will carry the
            # change in step, so the pad takes it now and the watch ends.
            with self._geom_lock:
                revs = self._revs.get(pad)
                if not revs or latest not in revs:
                    continue
                geometry = revs[latest][0]
                for old in [r for r in revs if r < latest]:
                    del revs[old]
                self._applied[pad] = geometry
                self._applied_rev[pad] = latest
            self._set_geometry(pad, geometry)
        self._watch_timer = 0
        self._preview_flow(self._moving())
        for program in self.programs.values():
            program.watching = False
            if program.handler:
                program.mixer.set_property("emit-signals", False)
            if program.probe and program.sink_pad is not None:
                program.sink_pad.remove_probe(program.probe)
            program.probe = 0
        return False

    def _commit(self, frame: Gst.Element, pad: Gst.Pad | None) -> None:
        """A change to ``frame`` that keeps its geometry (opacity, content rect)."""
        if pad is None or frame.find_property("revision") is None:
            return
        self._watch()
        with self._geom_lock:
            revs = self._revs.get(pad)
            if not revs:
                return
            self._revision += 1
            revision = self._revision
            revs[revision] = revs[max(revs)]
        frame.set_property("revision", revision)
        self._redraw(frame)

    def _forget_pad(self, pad: Gst.Pad | None) -> None:
        with self._geom_lock:
            self._revs.pop(pad, None)
            self._applied.pop(pad, None)
            self._applied_rev.pop(pad, None)
            self._pad_widget.pop(pad, None)
            self._superseding.pop(pad, None)
            self._supersede_probes.discard(pad)


    def _set_geometry(self, pad: Gst.Pad, geometry) -> None:
        """A pad's place, unless it is gliding (its controller places it frame by frame)."""
        if pad in self._gliding:
            return
        for name, value in zip(self._GEOMETRY, geometry):
            if pad.get_property(name) != value:
                pad.set_property(name, value)

    def _samples_selected(self, mixer, _segment, pts, _dts, _duration, _info, program: Program) -> None:
        """The mixer's thread, before it composites a frame: each layer's pad takes the geometry of
        the revision its selected buffer was drawn with."""
        boxes = {}
        # The mixer's own pads, each held while it is looked at (one being released stays valid).
        pads = []
        iterator = mixer.iterate_sink_pads()
        while True:
            result, pad = iterator.next()
            if result == Gst.IteratorResult.RESYNC:
                pads, _ = [], iterator.resync()
                continue
            if result != Gst.IteratorResult.OK:
                break
            pads.append(pad)
        for pad in pads:
            with self._geom_lock:
                revs = self._revs.get(pad)
            if revs is None or pad in self._gliding:
                continue
            sample = mixer.peek_next_sample(pad)
            buffer = sample.get_buffer() if sample is not None else None
            if buffer is None:
                continue
            with self._geom_lock:
                entry = revs.get(buffer.offset)
                if entry is None:
                    continue
                for old in [r for r in revs if r < buffer.offset]:
                    del revs[old]
                applied = self._applied.get(pad)
                self._applied[pad] = entry[0]
                self._applied_rev[pad] = buffer.offset
                widget_id = self._pad_widget.get(pad)
            if applied != entry[0]:
                for name, value in zip(self._GEOMETRY, entry[0]):
                    pad.set_property(name, value)
            if widget_id is not None and widget_id in self._shown_ids:
                boxes[widget_id] = entry[1]  # only the shown scene's: the window outlines those
        with self._geom_lock:
            program.composited.append((pts, boxes))
            del program.composited[:-16]

    def _preview_frame(self, _pad, info, program: Program) -> Gst.PadProbeReturn:
        """A program frame reaches the preview: what it shows, for the outline (frame-shown)."""
        buffer = info.get_buffer()
        if buffer is None:
            return Gst.PadProbeReturn.OK
        with self._geom_lock:
            boxes = next((b for t, b in reversed(program.composited) if t <= buffer.pts), None)
        if boxes is not None:
            changed = {k: v for k, v in boxes.items() if program.on_screen.get(k) != v}
            if changed:
                program.on_screen.update(changed)
                GLib.idle_add(self._emit_shown, program.fmt, changed, priority=GLib.PRIORITY_DEFAULT)
        return Gst.PadProbeReturn.OK

    def _emit_shown(self, fmt: str, boxes: dict) -> bool:
        for widget_id, box in boxes.items():
            self.emit("frame-shown", fmt, widget_id, *box)
        return False

    def _trace(self, feed: Feed, elements: list) -> None:
        """STREAMY_TRACE=1: count pictures at the source and at the tee, every 2 s, on stderr."""
        import sys

        counts = {}
        for element in (elements[0], elements[-2] if len(elements) > 1 else elements[0]):
            pad = element.get_static_pad("src")
            if pad is None:
                continue
            counts[element.get_name()] = [0, None]

            def seen(p, info, name=element.get_name()):
                counts[name][0] += 1
                buffer = info.get_buffer()
                counts[name][1] = (buffer.pts, p.get_current_caps().to_string()[:120] if p.get_current_caps() else None)
                return Gst.PadProbeReturn.OK
            pad.add_probe(Gst.PadProbeType.BUFFER, seen)

        def report() -> bool:
            print("streamy-trace", feed.name_id, counts, file=sys.stderr, flush=True)
            return self.feeds.get(feed.key) is feed
        GLib.timeout_add_seconds(2, report)

    def _no_picture(self, feed: Feed) -> bool:
        feed.watchdog = 0
        if self.feeds.get(feed.key) is not feed or feed.shown:
            return False
        if feed.key not in self._no_alpha and any(e.get_name().endswith("-alpha") for e in feed.elements):
            self._fail(feed, "format", "no picture with an alpha format")  # opaque, at once
            return False
        if feed.sourced and feed.key not in self._sw_decode and any(e.get_name().endswith("-vapp") for e in feed.elements):
            # The camera sends, yet nothing decoded on the GPU reached the canvas: the CPU decodes it instead.
            log(f"source {feed.name_id}: the camera sends but GPU decoding showed no picture in "
                f"{FIRST_PICTURE_S} s; decoding in software")
            self._sw_decode.add(feed.key)
            self._reopen(feed)
            return False
        log(f"source {feed.name_id}: no picture after {FIRST_PICTURE_S} s; the stream stays open")
        for link in feed.links:
            self.emit("widget-waiting", link)
        return False


    def _reopen(self, feed: Feed) -> None:
        """Take ``feed`` out with every widget on it; the scene shown again opens it afresh."""
        for branch in self._branches_of(feed):
            self._remove_branch(branch.widget_id)
        self._drop_feed(feed)
        GLib.idle_add(lambda: self.show_scene(self._scene, skip=self._skip, states=self._states) and False)

    def _fail(self, feed: Feed, reason: str, detail: str = "") -> None:
        if self.feeds.get(feed.key) is not feed:
            return
        if reason in ("format", "stopped") and feed.key not in self._no_alpha and \
                any(e.get_name().endswith("-alpha") for e in feed.elements):
            # The cast offers no format with alpha: take it opaque, at once, rather than fail it.
            log(f"source {feed.name_id}: the window cast offers no alpha format ({detail[:160]}); taken opaque")
            self._no_alpha.add(feed.key)
            self._reopen(feed)
            return
        gpu = reason in ("format", "stopped") and feed.key not in self._sw_decode and \
            any(e.get_name().endswith("-vapp") for e in feed.elements)
        if gpu and feed.shown and time.monotonic() - self._gpu_reopened.get(feed.key, -GPU_REOPEN_S) >= GPU_REOPEN_S:
            # GPU decoding worked until the camera changed its picture size (the decoder's pool keeps the old
            # size): open it again on the GPU, once a minute at most, rather than move it to the CPU for good.
            log(f"source {feed.name_id}: GPU decoding stopped after it showed pictures ({detail[:160]}); "
                f"opening it again on the GPU")
            self._gpu_reopened[feed.key] = time.monotonic()
            self._reopen(feed)
            return
        if gpu:
            log(f"source {feed.name_id}: GPU decoding failed ({detail[:160]}); decoding in software")
            self._sw_decode.add(feed.key)
            self._reopen(feed)
            return
        log(f"source {feed.name_id} failed: {reason}" + (f": {detail}" if detail else ""))
        links = feed.links
        for branch in self._branches_of(feed):
            self._remove_branch(branch.widget_id)
        self._drop_feed(feed)
        for link in links:
            self.emit("widget-failed", link, reason)
        self._idle_check()
        # Once its elements are out, the rest of the program runs on (see _keep_playing).
        GLib.timeout_add(100, lambda: self._keep_playing() and False)

    @staticmethod
    def _link_chain(elements: list[Gst.Element]) -> None:
        for a, b in zip(elements, elements[1:]):
            if a.get_factory().get_name() == "decodebin":
                if b.get_factory().get_name() == "vapostproc":
                    a.connect("autoplug-factories", graph.gpu_decoders_first)
                def pad_added(_bin, pad, following=b):
                    sink = following.get_static_pad("sink")
                    if not sink.is_linked() and pad.query_caps(None).to_string().startswith(("video/", "audio/")):
                        pad.link(sink)
                a.connect("pad-added", pad_added)
            elif not a.link(b):
                raise EngineError(f"{a.get_name()} does not link to {b.get_name()}")

    def _first_picture(self, _pad, _info, ref) -> Gst.PadProbeReturn:
        feed = ref()
        if feed is None:
            return Gst.PadProbeReturn.REMOVE
        feed.shown = True
        GLib.idle_add(self._fade_in, feed)
        return Gst.PadProbeReturn.REMOVE

    def _fade_in(self, feed: Feed) -> bool:
        """A feed's first picture: its widgets of the shown scene fade in on the program clock (their
        cards go once they are in); its widgets of other scenes get the picture, ready to show."""
        if self.feeds.get(feed.key) is not feed:
            return False
        if feed.device:
            how = "GPU" if any(e.get_name().endswith("-vapp") for e in feed.elements) else "software"
            log(f"source {feed.name_id}: first picture after {(time.monotonic() - feed.started) * 1000:.0f} ms"
                f" ({how} decode)")
        for link in feed.links:
            self.emit("widget-ready", link)
        shown = []
        for branch in self._branches_of(feed):
            if not self._on(branch):
                branch.fade = FADE_MS
                for layer in branch.layers.values():
                    self._push_last(branch, layer)
                continue
            shown.append(branch)
            for fmt, layer in branch.layers.items():
                start, _now = self._times(fmt)
                if layer.alpha is not None and start is not None:
                    layer.alpha.ramp(start, self._curve(start, FADE_MS * Gst.MSECOND, 0.0, 1.0, _smooth))
                elif layer.alpha is not None:
                    layer.alpha.hold(1.0)

        def done() -> bool:
            for branch in shown:
                if self.branches.get(branch.widget_id) is branch:
                    branch.fade = FADE_MS
                    self._remove_placeholder(branch.widget_id)  # the real picture is in: the card goes
            return False

        GLib.timeout_add(FADE_MS + LEAD_MS, done)
        self._sync_parking()  # a camera only another scene shows parks once it has its first picture
        return False


    def _times(self, fmt: str, lead: int = LEAD_NS) -> tuple[int | None, int | None]:
        """(stream time ``lead`` ns past canvas ``fmt``'s next frame, running time now), or Nones.

        The next frame is the mixer's, not the clock's: a mixer running late (a loaded machine on
        software GL) composites frames stamped behind the clock, and keyframes placed by the clock would
        land on frames it hasn't made yet, or be replaced before it gets to them."""
        program = self.programs.get(fmt)
        if program is None or self.pipeline is None:
            return None, None
        clock = self.pipeline.get_clock()
        if clock is None:
            return None, None
        running = clock.get_time() - self.pipeline.get_base_time()
        now = self._stream_time(program.mixer.get_static_pad("src"), running)
        made = self._made(program)
        if made is not None and made < now:
            now = made
        return now + lead, running

    @staticmethod
    def _made(program: Program) -> int | None:
        """How far canvas ``program``'s mixer has composited (stream time), or None."""
        ok, position = program.mixer.query_position(Gst.Format.TIME)
        return position if ok and position not in (-1, Gst.CLOCK_TIME_NONE) else None


    @staticmethod
    def _stream_time(pad: Gst.Pad, running: int) -> int:
        """Running time ``running`` as stream time on ``pad`` (what its element syncs controllers on)."""
        event = pad.get_sticky_event(Gst.EventType.SEGMENT, 0) if pad is not None else None
        if event is None:
            return running
        segment = event.parse_segment()
        if segment.format != Gst.Format.TIME:
            return running
        position = segment.position_from_running_time(Gst.Format.TIME, running)
        if position in (-1, Gst.CLOCK_TIME_NONE):
            return running
        stream = segment.to_stream_time(Gst.Format.TIME, position)
        return running if stream in (-1, Gst.CLOCK_TIME_NONE) else stream


    @staticmethod
    def _curve(start: int, duration: int, begin: float, end: float, ease) -> list[tuple[int, float]]:
        """Keyframes from ``begin`` to ``end`` over ``duration`` ns, one per program frame, eased."""
        if duration <= 0:
            return [(start, end)]
        step = Gst.SECOND // graph.PREVIEW_FPS
        n = max(1, -(-duration // step))
        return [(start + duration * i // n, begin + (end - begin) * ease(i / n)) for i in range(1, n + 1)]


    def _sync_shown(self) -> None:
        """No change of scene: each layer passes pictures and shows exactly while its scene is shown."""
        for branch in self.branches.values():
            on = self._on(branch)
            for layer in branch.layers.values():
                if layer.valve.get_property("drop") == on:
                    if on:
                        self._push_last(branch, layer)
                    layer.valve.set_property("drop", not on)
                    if layer.alpha is not None:
                        layer.alpha.hold(1.0 if on and branch.fade >= FADE_MS else 0.0)
        self._sync_gates(0)
        self._sync_parking()


    def _sync_parking(self) -> None:
        """A camera no shown scene uses, once it has shown a picture, parks: open, its pictures dropped
        before decoding. One a shown scene uses passes them."""
        if self._pending is not None or self._switch_timer:
            return  # a change of scene under way still draws the cameras it moves between
        for feed in self.feeds.values():
            if feed.park is not None and feed.shown:
                self._set_parked(feed, not self._feed_on(feed))

    @staticmethod
    def _set_parked(feed: Feed, parked: bool) -> None:
        if feed.parked == parked:
            return
        feed.parked = parked
        if feed.waits:
            feed.park.set_property("drop", parked)

    @staticmethod
    def _trickle(_pad, _info, ref) -> Gst.PadProbeReturn:
        """A parked screen's pictures: one a second passes, the rest are dropped before the GPU."""
        feed = ref()
        if feed is None or not feed.parked:
            return Gst.PadProbeReturn.OK
        now = time.monotonic()
        if now - feed.passed >= 1.0:
            feed.passed = now
            return Gst.PadProbeReturn.OK
        return Gst.PadProbeReturn.DROP

    def _begin_switch(self, previous: str, active: str | None) -> None:
        """Change scene once every picture the new scene shows is in its mixer pads: each layer's newest
        picture is drawn through it first (a parked camera's next one, which it is woken for), and the
        change starts when each has come out of its lumaframe, at most SWITCH_WAIT_MS later. So the first
        frame of the change has every picture in it, also on a mixer that runs late."""
        woken = set()
        for feed in self.feeds.values():
            if feed.parked and any(b.scene_id == active for b in self._branches_of(feed)):
                if feed.waits:
                    woken.add(feed.key)  # a parked camera's last picture is old: its next one is waited for
                self._set_parked(feed, False)
        waiting: set = set()
        probes, told = [], set()

        def arrived(_pad, _info, key):
            # Removed with the change (_cancel_pending), never from here: one id, removed once.
            if key not in told:
                told.add(key)
                GLib.idle_add(ready, key)
            return Gst.PadProbeReturn.OK

        def ready(key=None) -> bool:
            pending = self._pending
            if pending is None or pending[1] != active:
                return False
            waiting.discard(key)
            if key is None or not waiting:
                self._cancel_pending()
                self._switch(previous, active)
            return False

        for branch in [b for b in self.branches.values() if b.scene_id == active and b.picture]:
            if not branch.feed.shown or branch.fade < FADE_MS:
                continue  # no picture yet: it fades in when it has one (_fade_in)
            for fmt, layer in branch.layers.items():
                if layer.alpha is None or not layer.linked:
                    continue
                key = (branch.widget_id, fmt)
                waiting.add(key)
                out = layer.frame.get_static_pad("src")
                probes.append((out, out.add_probe(Gst.PadProbeType.BUFFER, arrived, key)))
                if branch.feed.key not in woken:
                    self._push_last(branch, layer)
                layer.valve.set_property("drop", False)
        if not waiting:
            for pad, probe in probes:
                pad.remove_probe(probe)
            self._switch(previous, active)
            return
        self._pending = (previous, active, GLib.timeout_add(SWITCH_WAIT_MS, ready), probes)

    def _cancel_pending(self) -> None:
        pending, self._pending = self._pending, None
        if pending is None:
            return
        _previous, _active, timer, probes = pending
        GLib.source_remove(timer)  # (a timer that fired removed itself first: ready runs once)
        for pad, probe in probes:
            pad.remove_probe(probe)

    def _switch(self, previous: str, active: str | None) -> None:
        """From scene ``previous`` to ``active``, on every canvas at once: a cut, or a fade over
        ``transition_ms``. The incoming scene's layers draw above the outgoing one's and fade in; the
        outgoing ones fade out on an ease-in curve, so a picture in both places never dips. A picture
        shown in both scenes (the same feed) glides from its old place, size and stacking to its new one:
        its two layers move together and cross-fade between the two Looks."""
        if self._switch_timer:
            GLib.source_remove(self._switch_timer)
            self._switch_timer = 0
        kind, ms = self.transition
        duration = int(ms) * Gst.MSECOND if kind == "fade" else 0
        self._restack_all()
        incoming = [b for b in self.branches.values() if b.scene_id == active and b.picture]
        outgoing = [b for b in self.branches.values() if b.scene_id == previous and b.picture]
        pairs: dict[str, Branch] = {}  # outgoing widget id -> the incoming one showing the same picture
        if duration:
            free = list(incoming)
            for out in outgoing:
                twin = next((b for b in free if b.feed is out.feed and b.fade >= FADE_MS), None)
                if twin is not None and out.fade >= FADE_MS:
                    free.remove(twin)
                    pairs[out.widget_id] = twin
        self.switched_at = {}
        for fmt in self.programs:
            start, _running = self._times(fmt)
            if start is None:
                continue
            self.switched_at[fmt] = start
            if os.environ.get("STREAMY_TRACE_SWITCH"):
                event = self.programs[fmt].mixer.get_static_pad("src").get_sticky_event(Gst.EventType.SEGMENT, 0)
                seg = event.parse_segment() if event is not None else None
                log(f"switch {previous} -> {active} on {fmt}: start {start / 1e6:.0f} ms, running {_running / 1e6:.0f} ms"
                    + (f", segment start {seg.start / 1e6:.0f} time {seg.time / 1e6:.0f} base {seg.base / 1e6:.0f}"
                       f" position {seg.position / 1e6:.0f}" if seg is not None else ", no segment"))
            end = start + duration
            for branch in incoming:
                layer = branch.layers.get(fmt)
                if layer is None:
                    continue
                if layer.valve.get_property("drop"):
                    # Its newest picture first, then the source's next ones: never one older after a newer.
                    self._push_last(branch, layer)
                    layer.valve.set_property("drop", False)
                if layer.alpha is None or branch.fade < FADE_MS:
                    continue  # no picture yet: it fades in when it has one (_fade_in)
                if duration:
                    layer.alpha.ramp(start, self._curve(start, duration, layer.alpha.value_at(start), 1.0, _smooth))
                else:
                    layer.alpha.ramp(start - 1, [(start, 1.0)])
            for branch in outgoing:
                layer = branch.layers.get(fmt)
                if layer is None or layer.alpha is None:
                    continue
                if not duration:
                    layer.alpha.ramp(start - 1, [(start, 0.0)])
                elif branch.widget_id in pairs:
                    # Held under its twin (which fades in over it, in the same place), then gone.
                    layer.alpha.ramp(start, [(end, layer.alpha.value_at(start)), (end + 1, 0.0)])
                else:
                    layer.alpha.ramp(start, self._curve(start, duration, layer.alpha.value_at(start), 0.0, _fade_out))
            for out_id, twin in pairs.items():
                out = self.branches[out_id]
                self._glide(out, twin, fmt, start, duration)
            for branch in self.branches.values():
                if branch.scene_id in (previous, active) or not branch.picture:
                    continue
                layer = branch.layers.get(fmt)
                if layer is not None and layer.alpha is not None:
                    self._end_glide(layer)
                    layer.alpha.hold(0.0)
                    layer.valve.set_property("drop", True)
        self._sync_gates(duration)
        self._watch()

        ends = {fmt: at + duration for fmt, at in self.switched_at.items()}

        def done() -> bool:
            # Over once every canvas has composited its last step (a late mixer gets there after the clock).
            late = [fmt for fmt, end in ends.items() if fmt in self.programs and
                    (self._made(self.programs[fmt]) or 0) < end + 2 * LEAD_NS]
            if late and time.monotonic() < given:
                self._switch_timer = GLib.timeout_add(LEAD_MS, done)
                return False
            self._switch_timer = 0
            for branch in list(self.branches.values()):
                for layer in branch.layers.values():
                    if layer.glide:
                        self._end_glide(layer, branch)
                if not self._on(branch):
                    for layer in branch.layers.values():
                        layer.valve.set_property("drop", True)
            self._sync_parking()
            return False

        given = time.monotonic() + (int(ms) if duration else 0) / 1000 + 5.0  # a mixer stalled for good
        self._switch_timer = GLib.timeout_add(int(ms if duration else 0) + 2 * LEAD_MS, done)


    def _glide(self, out: Branch, twin: Branch, fmt: str, start: int, duration: int) -> None:
        """``out``'s and ``twin``'s layers on ``fmt`` (the same picture) move together from where ``out``
        is to where ``twin`` goes, each grown by its own Look's margin at the size it is drawn at."""
        a, b = out.layers.get(fmt), twin.layers.get(fmt)
        if a is None or b is None:
            return
        va, vb = self.view_of(out.view, fmt), self.view_of(twin.view, fmt)
        here = a.path and self._box_at(a.path, start)
        frm = here or (va.x, va.y, va.width, va.height)
        to = (vb.x, vb.y, vb.width, vb.height)
        ma, mb = va.effects.clamped().margin, vb.effects.clamped().margin
        if frm == to and ma == mb:
            return  # the same place: held, nothing moves
        path = (frm, to, start, duration)
        for layer, view, margin in ((a, va, ma), (b, vb, mb)):
            self._end_glide(layer)
            layer.path = path
            own = (max(1.0, float(view.width)), max(1.0, float(view.height)))
            keys = {name: [] for name in self._GEOMETRY}
            for when, _t in [(start, 0.0)] + self._curve(start, duration, 0.0, 1.0, lambda t: t):
                for name, value in zip(self._GEOMETRY, self._pad_rect(self._box_at(path, when), own, margin)):
                    keys[name].append((when, value))
            for name in self._GEOMETRY:
                animation = Animation(layer.pad, name, keys[name][0][1])
                animation.ramp(start, keys[name][1:])
                layer.glide.append(animation)
            self._gliding.add(layer.pad)


    @staticmethod
    def _box_at(path, when: int) -> tuple[float, float, float, float]:
        frm, to, start, duration = path
        t = 1.0 if duration <= 0 else max(0.0, min(1.0, (when - start) / duration))
        e = _smooth(t)
        return tuple(f + (g - f) * e for f, g in zip(frm, to))


    @staticmethod
    def _pad_rect(box, own: tuple[float, float], margin: float) -> tuple[int, int, int, int]:
        """The pad a layer drawn at box size ``own`` (plus ``margin``) takes to show its box at ``box``."""
        x, y, w, h = box
        mx, my = margin * w / own[0], margin * h / own[1]
        return int(round(x - mx)), int(round(y - my)), max(1, int(round(w + 2 * mx))), max(1, int(round(h + 2 * my)))


    def _end_glide(self, layer: Layer, branch: Branch | None = None) -> None:
        """A glide is over: the pad's controllers go and it takes its own place again."""
        if not layer.glide:
            return
        for animation in layer.glide:
            animation.remove()
        layer.glide, layer.path = [], None
        self._gliding.discard(layer.pad)
        branch = branch or next((b for b in self.branches.values() if layer in b.layers.values()), None)
        if branch is not None and branch.view is not None:
            view = self.view_of(branch.view, layer.fmt)
            self._set_geometry(layer.pad, tuple(graph.mixer_pad_props(view, 0)[k] for k in self._GEOMETRY))
            self._style_layer(layer, view, self._z(branch))


    def _sync_gates(self, duration: int) -> None:
        """Sound is sent while a widget of the shown scene uses it: each gate fades (or cuts) to that."""
        for feed in self.feeds.values():
            if feed.gate is None:
                continue
            on = self._feed_on(feed)
            if feed.gate_on == on:
                continue
            feed.gate_on = on
            pad = feed.gate.target.get_static_pad("sink")
            clock = self.pipeline.get_clock() if self.pipeline is not None else None
            if clock is None:
                feed.gate.hold(1.0 if on else 0.0)
                continue
            start = self._stream_time(pad, clock.get_time() - self.pipeline.get_base_time() + LEAD_NS)
            ease = _smooth if on else _fade_out
            feed.gate.ramp(start, self._curve(start, duration, feed.gate.value_at(start), 1.0 if on else 0.0, ease)
                           if duration else [(start, 1.0 if on else 0.0)])
            widget = next((b.view for b in self._branches_of(feed) if self._on(b) and b.view is not None), None)
            self._sync_monitor(feed, widget)

    def _sync_monitor(self, feed: Feed, widget: Widget | None) -> None:
        """A sound's monitor exists exactly while it is on and a shown widget uses it (and follows its device)."""
        want = widget is not None and widget.monitor in ("only", "both")
        device = widget.source.get("monitor-device") if widget is not None else None
        if feed.monitor and (not want or device != feed.monitor_device):
            self._drop_monitor(feed)
        if not want or feed.monitor or self.pipeline is None:
            return
        tee = next((e for e in feed.elements if e.get_factory().get_name() == "tee"), None)
        if tee is None:
            return
        named = Widget(feed.name_id, widget.kind, shared=widget.shared)
        elements = [make(e) for e in graph.monitor_chain(named, feed.generation)]
        for element in elements:
            self.pipeline.add(element)
        link_all(elements)
        tee.link(elements[0])
        feed.monitor, feed.monitor_device = elements, device
        self._call_async(self.pipeline, lambda _p, items: [e.sync_state_with_parent() for e in reversed(items)], elements)

    def _drop_monitor(self, feed: Feed) -> None:
        elements, feed.monitor = feed.monitor, []
        if not elements or self.pipeline is None:
            return
        self._stop_then_unlink(elements, elements[0].get_static_pad("sink"))

    def _stop_then_unlink(self, elements: list, sink: Gst.Pad, src: Gst.Pad | None = None) -> None:
        """Take a chain out in the order GStreamer needs: its data dropped at once (at the request pad
        it hangs from, and at its own ``src`` into a mixer), its elements NULL on the pool, then, on the
        main loop, its request pads released and its elements removed."""
        pipeline = self.pipeline
        upstream = sink.get_peer()
        downstream = src.get_peer() if src is not None else None
        for pad in (upstream, src):
            if pad is not None:
                pad.add_probe(Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST | Gst.PadProbeType.EVENT_DOWNSTREAM,
                              lambda *_a: Gst.PadProbeReturn.DROP)
        for element in elements:
            element.set_locked_state(True)
        if downstream is not None and downstream.find_property("alpha") is not None:
            # A mixer keeps compositing a pad's last frame until the pad goes: hidden now, not after NULL.
            downstream.set_property("alpha", 0.0)
            self._watch()

        def drop() -> bool:
            for peer, pad in ((upstream, sink), (src, downstream)):
                if peer is None or pad is None:
                    continue
                if peer.get_peer() is pad:
                    peer.unlink(pad)
                request = peer if peer is upstream else pad  # the tee's or the mixer's request pad
                owner = request.get_parent_element()
                if owner is not None:
                    owner.release_request_pad(request)
            for element in elements:
                if element.get_parent() is pipeline:
                    pipeline.remove(element)
            if self.pipeline is pipeline:
                self._watch()  # the canvases changed: the preview shows it before it rests
            return False

        def stop(_pipeline, items) -> None:
            for element in items:
                element.set_state(Gst.State.NULL)
            GLib.idle_add(drop)

        self._call_async(pipeline, stop, elements)

    def _remove_branch(self, widget_id: str) -> None:
        """Take a widget out: its layers go; its feed closes once no widget uses it."""
        branch = self.branches.pop(widget_id, None)
        if branch is None:
            return
        for fmt in list(branch.layers):
            self._remove_layer(branch, fmt)
        feed = branch.feed
        feed.users.pop(widget_id, None)
        if not feed.users and self.feeds.get(feed.key) is feed:
            self._drop_feed(feed)
        elif not feed.picture and feed.monitor and not self._feed_on(feed):
            self._drop_monitor(feed)


    def _drop_feed(self, feed: Feed) -> None:
        """Close a capture without waiting. Its data stops at once (a probe drops it before the mixers);
        its elements go to NULL on GStreamer's thread pool (a stuck source can take its time); only then,
        back on the main loop, do its pads unlink and its elements leave the pipeline. A sound mixer goes
        only once no stopping feed still feeds it."""
        if self.feeds.get(feed.key) is feed:
            del self.feeds[feed.key]
        feed.closed, feed.last = True, None
        if self.pipeline is None:
            return
        self._drop_monitor(feed)
        if feed.watchdog:
            GLib.source_remove(feed.watchdog)
            feed.watchdog = 0
        pipeline = self.pipeline
        if feed.pad is not None:
            # Nothing more reaches the sound mixer; the pad stays linked until the feed is NULL.
            feed.elements[-1].get_static_pad("src").add_probe(
                Gst.PadProbeType.BUFFER | Gst.PadProbeType.BUFFER_LIST | Gst.PadProbeType.EVENT_DOWNSTREAM,
                lambda *_a: Gst.PadProbeReturn.DROP)
        device = feed.device
        if device:
            self._releasing[device] = self._releasing.get(device, 0) + 1

        def stop(_pipeline, elements) -> None:  # on the pool
            for element in elements:
                element.set_state(Gst.State.NULL)
            GLib.idle_add(drop)

        def drop() -> bool:  # on the main loop, every element NULL
            if feed.gate is not None:
                feed.gate.remove()
                feed.gate = None
            if feed.pad is not None:
                last = feed.elements[-1].get_static_pad("src")
                if last.get_peer() is feed.pad:
                    last.unlink(feed.pad)
                mixer = feed.pad.get_parent_element()
                if mixer is not None:
                    mixer.release_request_pad(feed.pad)
                feed.pad = None
            for element in feed.elements:
                if element.get_parent() is pipeline:
                    pipeline.remove(element)
            for element in feed.elements:
                element.set_locked_state(False)
            close_fds(source_fds(feed.elements))
            if device:
                self._released(device)
            if not feed.picture and self.pipeline is pipeline:
                self._sync_sound()
            return False

        for element in feed.elements:
            element.set_locked_state(True)  # the pipeline's own state changes leave it alone now
        self._call_async(pipeline, stop, feed.elements)


    def reopen(self, link: str | None = None) -> None:
        """Close source ``link``'s feeds (every source's with None) and every widget drawn from them; the
        next show_scene opens them afresh (a new mode, a device back, an undo step's settings)."""
        for feed in [f for f in self.feeds.values() if link is None or link in f.links]:
            for branch in self._branches_of(feed):
                self._remove_branch(branch.widget_id)
            self._drop_feed(feed)


    def has_source(self, link: str) -> bool:
        """Source ``link`` is open (captured for some scene)."""
        return any(link in f.links for f in self.feeds.values())

    def _released(self, device: str) -> bool:
        """A stream on ``device`` has stopped: one waiting for it opens now."""
        left = self._releasing.get(device, 0) - 1
        if left > 0:
            self._releasing[device] = left
            return False
        self._releasing.pop(device, None)
        if self.pipeline is not None and self._scene is not None:
            self.show_scene(self._scene, skip=self._skip, states=self._states)
        return False

    # ── Sound ──

    def _needs_sound(self) -> bool:
        return any(not f.picture for f in self.feeds.values()) or bool(self.outputs) or \
            bool(self.fanout is not None and self.fanout.wants())

    def _ensure_sound(self) -> None:
        if self._sound or self.pipeline is None:
            return
        elements = [make(e) for e in graph.sound_program()]
        for element in elements:
            self.pipeline.add(element)
        link_all(elements)
        for element in reversed(elements):
            element.sync_state_with_parent()
        self._sound = True

    def _sync_sound(self) -> None:
        """The sound mixer runs only while something makes or takes sound."""
        if self._needs_sound():
            self._ensure_sound()
            return
        if not self._sound or self.pipeline is None:
            return
        mixer = self.pipeline.get_by_name("audiomixer")
        if mixer is not None and mixer.numsinkpads:
            return  # a stopping branch still feeds it: the last one's drop() comes back here
        # The sound tap goes with the mixer it hangs from (nothing flows there once it stops).
        for element in self._sound_tap:
            element.set_state(Gst.State.NULL)
            self.pipeline.remove(element)
        self._sound_tap = []
        elements = [self.pipeline.get_by_name(spec.name) for spec in graph.sound_program()]
        elements = [e for e in elements if e is not None]
        for element in elements:
            element.set_locked_state(True)
            element.set_state(Gst.State.NULL)
        for element in elements:
            self.pipeline.remove(element)
        self._sound = False

    # ── Outputs ──

    def _sent_canvases(self) -> set[str]:
        sent = {o.fmt for o in self.outputs.values()}
        if self.fanout is not None:
            sent |= set(self.fanout.wants())
        return sent

    def sync_taps(self) -> None:
        """Open the taps the outputs read (made on first use, then only their valves open and close);
        a canvas nothing sends has its valve closed, so it converts and reads back nothing.

        The taps stay linked. Taking a tee branch down while the program runs raced the next
        output's GL elements, and a branch shut down before it is unlinked answers the tee
        FLUSHING and stops the program; a closed valve costs nothing either way."""
        if self.pipeline is None:
            return
        self._sync_programs()
        sent = self._sent_canvases()
        if sent:
            self._ensure_sound()
        for fmt, program in self.programs.items():
            if fmt in sent and not program.tap:
                program.tap = self._attach_tap(("video", fmt), program.tee, [make(e) for e in graph.tap_video(fmt)])
            if program.tap:
                program.tap[0].set_property("drop", fmt not in sent)
                self._tap_rate(fmt)
        if sent and not self._sound_tap and self._sound:
            self._sound_tap = self._attach_tap("sound", self.pipeline.get_by_name("sound"),
                                               [make(e) for e in graph.tap_sound()])
        if self._sound_tap:
            self._sound_tap[0].set_property("drop", not sent)
        if not sent:
            self._sync_sound()
        if self.enabled:
            # A canvas an output starts taking captures its pictures; one kept only for an output that
            # ended goes now.
            self.show_scene(self._scene, skip=self._skip, states=self._states)

    def _tap_rate(self, fmt: str) -> None:
        """Canvas ``fmt``'s tap hands over its fastest encode's rate (outputs.Outputs.fps: 30 or 60);
        each encode paces itself to its own."""
        rate = getattr(self.fanout, "fps", None)
        fps = max(graph.FPS, rate(fmt) if rate is not None else graph.FPS)
        caps = self.pipeline.get_by_name(f"tap-fps-{fmt}")
        if caps is None:
            return
        current = caps.get_property("caps")
        ok, num, den = current.get_structure(0).get_fraction("framerate") if current is not None and \
            current.get_size() else (False, 0, 1)
        if ok and den and num // den == fps:
            return
        caps.set_property("caps", Gst.Caps.from_string(f"video/x-raw(memory:GLMemory),framerate={fps}/1"))
        log(f"tap {fmt}: {fps} fps")

    # Older names.
    def _ensure_taps(self) -> None:
        self.sync_taps()

    def _drop_taps(self) -> None:
        self.sync_taps()

    def _attach_tap(self, key, tee: Gst.Element, elements: list[Gst.Element]) -> list[Gst.Element]:
        for element in elements:
            self.pipeline.add(element)
        link_all(elements)
        tee.link(elements[0])
        elements[-1].connect("new-sample", self._sample, key)
        for element in reversed(elements):
            element.sync_state_with_parent()
        return elements

    def _sample(self, sink: Gst.Element, key) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        if key == "sound":
            for output in list(self.outputs.values()):
                output.push(output.sound_src, sample)
            if self.fanout is not None:
                self.fanout.push_sound(sample)
        else:
            fmt = key[1]
            self._count_tap(fmt, sample)
            for output in list(self.outputs.values()):
                if output.fmt == fmt:
                    output.push(output.video_src, sample)
            if self.fanout is not None:
                self.fanout.push_video(fmt, sample)
        return Gst.FlowReturn.OK

    def _count_tap(self, fmt: str, sample: Gst.Sample) -> None:
        """Every 5 s, how many pictures canvas ``fmt``'s tap gave and how far their timestamps moved:
        pictures per wall second and media seconds per wall second (1.0 when the timestamps keep time)."""
        counts = self.__dict__.setdefault("_tap_counts", {})
        now = time.monotonic()
        pts = sample.get_buffer().pts
        entry = counts.get(fmt)
        if entry is None:
            counts[fmt] = [now, pts, 0]
            return
        entry[2] += 1
        span = now - entry[0]
        if span >= 5:
            media = (pts - entry[1]) / Gst.SECOND if pts != Gst.CLOCK_TIME_NONE else float("nan")
            caps = sample.get_caps().get_structure(0)
            ok, num, den = caps.get_fraction("framerate")
            log(f"tap {fmt}: {entry[2] / span:.1f} pictures/s, timestamps {media / span:.2f} s per s, "
                f"caps framerate {num}/{den if ok else '?'}")
            counts[fmt] = [now, pts, 0]

    def start_output(self, kind: str, choice: EncoderChoice, sink: list[Element], *, video_kbps: int,
                     audio_kbps: int = 160, fmt: str | None = None) -> Output:
        if not self.enabled:
            raise EngineError("nothing to send: the program isn't running")
        fmt = fmt or (self.shown_canvases[0] if self.shown_canvases else "landscape")
        self._open()
        if kind in self.outputs:
            raise EngineError(f"already {kind}ing")
        output = Output(self, kind, choice, sink, video_kbps, audio_kbps, with_sound=True, fmt=fmt)
        self.outputs[kind] = output
        self._ensure_sound()
        self.sync_taps()
        self._sync_placeholder_alpha()

        def finished() -> None:
            if self.outputs.get(kind) is output:
                del self.outputs[kind]
            self._sync_placeholder_alpha()
            if self.pipeline is not None:
                self.sync_taps()
                if not self.outputs:
                    self._idle_check()
            if kind == "stream" and self.stream_retry is not None:
                return  # reconnecting, not off
            self.emit("output-state", kind, "off")

        output.on_finished = finished
        output.on_error = lambda message: self.emit("problem", message)
        clock = self.pipeline.get_clock()
        running = clock.get_time() - self.pipeline.get_base_time()
        output.start(clock, self.pipeline.get_base_time(), running)
        self.emit("output-state", kind, "on")
        return output

    def stop_output(self, kind: str) -> None:
        output = self.outputs.get(kind)
        if output is not None:
            output.stop()

    def clock_now(self) -> tuple[Gst.Clock, int, int] | None:
        """(clock, base time, running time) of the program, for an output pipeline of its own to share."""
        if self.pipeline is None:
            return None
        clock = self.pipeline.get_clock()
        if clock is None:
            return None
        return clock, self.pipeline.get_base_time(), clock.get_time() - self.pipeline.get_base_time()

    # ── Recording and streaming ──

    def start_record(self, path: str, *, container: str = "mkv", codec: str = "h264",
                     video_kbps: int = 12000, fmt: str | None = None) -> Output:
        choice = choose_encoders(codec) or (choose_encoders("av1") if codec == "h264" else None)
        if choice is None:
            raise EngineError("No video encoder works on this computer")
        return self.start_output("record", choice, graph.record_sink(container, path), video_kbps=video_kbps,
                                 audio_kbps=192, fmt=fmt)

    def stop_record(self) -> None:
        self.stop_output("record")

    def start_stream(self, url: str, *, codec: str = "h264", video_kbps: int = 6000, audio_kbps: int = 160,
                     fmt: str | None = None) -> Output:
        """Go live; a dropped connection reconnects with backoff (2 s, ×1.5, up to 15 min)."""
        choice = choose_encoders(codec)
        if choice is None:
            raise EngineError("No H.264 encoder works on this computer" if codec == "h264"
                              else "No AV1 encoder works on this computer")
        if choice.audio is None:
            raise EngineError("No AAC encoder is installed")
        self.stream_retry = {"url": url, "choice": choice, "video": video_kbps, "audio": audio_kbps,
                             "attempt": 0, "timer": 0, "started": time.monotonic(), "fmt": fmt}
        return self._open_stream()

    def _open_stream(self) -> Output:
        retry = self.stream_retry
        enhanced = retry["choice"].codec != "h264" and Gst.ElementFactory.find("eflvmux") is not None
        output = self.start_output("stream", retry["choice"], graph.stream_sink(retry["url"], enhanced=enhanced),
                                   video_kbps=retry["video"], audio_kbps=retry["audio"], fmt=retry.get("fmt"))
        output.on_error = self._stream_failed
        return output

    def _stream_failed(self, message: str) -> None:
        retry = self.stream_retry
        if retry is None:
            self.emit("problem", message)
            return
        retry["attempt"] += 1
        delay = graph.backoff(retry["attempt"])
        self.emit("output-state", "stream", "reconnecting")

        def again() -> bool:
            retry["timer"] = 0
            if self.stream_retry is retry and "stream" not in self.outputs and self.pipeline is not None:
                try:
                    self._open_stream()
                except EngineError as error:
                    self._stream_failed(str(error))
            return False

        retry["timer"] = GLib.timeout_add(int(delay * 1000), again)

    def stop_stream(self) -> None:
        retry, self.stream_retry = self.stream_retry, None
        if retry is not None and retry["timer"]:
            GLib.source_remove(retry["timer"])
        self.stop_output("stream")
        if "stream" not in self.outputs:
            self.emit("output-state", "stream", "off")

    def stats(self, kind: str) -> OutputStats | None:
        output = self.outputs.get(kind)
        return output.stats if output is not None else None

    def set_stream_bitrate(self, kbps: int) -> None:
        """Change a live stream's bitrate (the VA encoders take it while playing)."""
        output = self.outputs.get("stream")
        if output is None or output.pipeline is None:
            return
        encoder = output.pipeline.get_by_name("stream-encoder")
        if encoder is not None:
            name = encoder.get_factory().get_name()
            if name == "svtav1enc":
                encoder.set_property("target-bitrate", kbps)  # read at the next key frame
            else:
                encoder.set_property("bitrate", kbps * (1000 if name == "openh264enc" else 1))

    # ── Bus ──

    def _feed_named(self, name_id: str | None) -> Feed | None:
        """The open feed whose elements carry ``name_id`` (a source's id)."""
        if name_id is None:
            return None
        return next((f for f in self.feeds.values() if f.name_id == name_id), None)

    def _message(self, _bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.STATE_CHANGED and message.src is self.pipeline:
            old, new, _pending = message.parse_state_changed()
            if old == Gst.State.PLAYING and new == Gst.State.PAUSED and self.enabled:
                GLib.idle_add(lambda: self._keep_playing() and False)
        elif message.type == Gst.MessageType.ELEMENT:
            structure = message.get_structure()
            if structure is not None and structure.get_name() == "level":
                name = message.src.get_name()
                feed = self._feed_named(graph.widget_of(name)) if name.endswith("-level") else None
                if feed is not None:
                    peaks = structure.get_value("peak")
                    left = peaks[0] if peaks else -60.0
                    right = peaks[1] if len(peaks) > 1 else left
                    for link in feed.links:
                        self.emit("levels", link, max(-60.0, left), max(-60.0, right))
        elif message.type == Gst.MessageType.ERROR:
            error, debug = message.parse_error()
            source = message.src.get_name() if message.src else ""
            widget_id = graph.widget_of(source)
            feed = self._feed_named(widget_id)
            if feed is not None and graph.generation_of(source) not in (None, feed.generation):
                # A feed already replaced (a new mode, a reopen) failing as it stops.
                log(f"{source}: {error.message} (stopping)")
            elif feed is not None:
                # One source failed (a window closed, a camera unplugged or busy): take it out, keep going.
                self._fail(feed, source_problem(error.message, debug or ""),
                           f"{error.message} ({(debug or '').splitlines()[0] if debug else ''})")
            elif widget_id is not None or source.startswith(("p-", "l-")):
                # A widget already out (its second error) or a card: its own words were said; never GStreamer's.
                log(f"{source}: {error.message}")
            else:
                log(f"program: {error.message} ({(debug or '').splitlines()[0] if debug else ''})")
                GLib.idle_add(lambda: self._keep_playing() and False)
