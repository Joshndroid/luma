# SPDX-License-Identifier: Apache-2.0
"""The output graph, described before it is built (pure Python, like graph.py).

One encode per (canvas, codec, video kbps, audio kbps, picture size, fps, automatic bitrate), teed to
every destination row that shares it; a picture size below the canvas's is scaled once, in the encode
(on the GPU before a VA encoder). Each row is a pipeline of its own fed by appsrc, so a row that drops its connection reconnects
alone and the encode and the other rows keep going:

    encode:  appsrc (NV12) ! queue ! [scale] ! <video encoder> ! parse ! capsfilter ! appsink
             appsrc (S16)  ! queue ! audioconvert ! <aac encoder> ! aacparse ! appsink
    rtmp:    appsrc ! queue ! flvmux | eflvmux ! rtmp2sink           (rtmp://, rtmps://)
    srt:     appsrc ! queue ! h264parse (byte-stream) ! mpegtsmux ! srtsink    (srt://)
    whip:    appsrc ! queue ! whipclientsink                          (http(s)://, if installed)
    file:    appsrc ! queue ! matroskamux | mp4mux ! filesink         (EOS finalises the file)
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .destinations import scaled
from .graph import AAC, AV1, FPS, H264, Element, EncoderChoice, audio_encoder_chain, video_encoder_chain

#: How a row's address sends: by scheme.
KINDS = {"rtmp://": "rtmp", "rtmps://": "rtmp", "srt://": "srt", "http://": "whip", "https://": "whip"}

#: A recording's encoder, best first: hardware AV1, hardware H.264, then software AV1 and H.264.
#: Software AV1 is behind hardware H.264 so a recording beside a stream never takes the CPU.
RECORD_ORDER = (("av1", ("vaav1lpenc", "vaav1enc")), ("h264", ("vah264lpenc", "vah264enc")),
                ("av1", ("svtav1enc",)), ("h264", ("openh264enc",)))


def kind_of(url: str) -> str:
    for scheme, kind in KINDS.items():
        if url.startswith(scheme):
            return kind
    raise ValueError("A server address starts with rtmp://, rtmps://, srt:// or https://")


@dataclass(frozen=True)
class Preset:
    """What one encode makes; rows with an equal preset share it."""

    canvas: str
    codec: str  # "h264" | "av1"
    video_kbps: int
    audio_kbps: int
    #: The picture's short side (0 = the canvas's own size).
    size: int = 0
    fps: int = FPS
    #: The encode lowers its bitrate while a row's network can't keep up (RateControl). Rows with and
    #: without it never share an encode: a recording or a fixed-rate stream never follows a slow one.
    auto: bool = False

    @property
    def key(self) -> str:
        key = f"{self.canvas}-{self.codec}-{self.video_kbps}-{self.audio_kbps}"
        if self.size:
            key += f"-{self.size}p"
        if self.fps != FPS:
            key += f"-{self.fps}fps"
        return key + ("-auto" if self.auto else "")


def choose(working: set[str], codec: str) -> EncoderChoice | None:
    """A stream's encoder for ``codec`` (hardware first) from the encoders that work."""
    order = H264 if codec == "h264" else AV1
    video = next((f for f in order if f in working), None)
    if video is None:
        return None
    return EncoderChoice(video, next((f for f in AAC if f in working), None), codec)


def choose_record(working: set[str], codec: str = "") -> EncoderChoice | None:
    """A recording's encoder: ``codec`` when asked for, else RECORD_ORDER."""
    if codec:
        return choose(working, codec)
    audio = next((f for f in AAC if f in working), None)
    for name, factories in RECORD_ORDER:
        video = next((f for f in factories if f in working), None)
        if video is not None:
            return EncoderChoice(video, audio, name)
    return None


def encode_video(choice: EncoderChoice, preset: Preset, source: tuple[int, int] | None = None, *,
                 gpu_scale: bool = False) -> list[Element]:
    """The encode's video half after its appsrc: graph.py's encoder chain (scaling ``source``, the canvas's
    size, to the preset's picture size first; on the GPU with ``gpu_scale``), then the caps every muxer
    takes (AVC with codec_data for H.264, temporal units for AV1)."""
    prefix = f"enc-{preset.key}"
    size = scaled(*source, preset.size) if source is not None else None
    chain = video_encoder_chain(choice, preset.video_kbps, prefix, fps=preset.fps,
                                scale=size if size is not None and size != tuple(source) else None,
                                gpu_scale=gpu_scale)
    parse = chain[-1]
    if parse.factory == "h264parse":
        parse.props = {}  # SPS/PPS travel in codec_data; an SRT row puts them in-band itself
        caps = "video/x-h264,stream-format=avc,alignment=au"
    else:
        caps = "video/x-av1,stream-format=obu-stream,alignment=tu"
    return chain + [Element("capsfilter", f"{prefix}-vcaps", {"caps": caps}),
                    Element("appsink", f"{prefix}-vout", _APPSINK)]


def encode_sound(choice: EncoderChoice, preset: Preset) -> list[Element]:
    prefix = f"enc-{preset.key}"
    chain = audio_encoder_chain(choice, preset.audio_kbps, prefix)
    if not chain:
        return []
    return chain + [Element("capsfilter", f"{prefix}-acaps", {"caps": "audio/mpeg,mpegversion=4,stream-format=raw"}),
                    Element("appsink", f"{prefix}-aout", _APPSINK)]


#: Encoded samples are handed on as they come: never synced, never dropped at the encode.
_APPSINK = {"emit-signals": True, "sync": False, "async": False, "drop": False, "max-buffers": 0}


def row_head(row_id: str, media: str, fps: int = FPS) -> list[Element]:
    """A row's appsrc and queue for ``media`` ("video" | "sound"). A full queue (the network is behind)
    drops at the row's appsrc, counted as network drops."""
    return [Element("appsrc", f"row-{row_id}-{media}", {"is-live": True, "format": 3, "do-timestamp": False,
                                                        "block": False, "max-buffers": fps * 4 if media == "video" else 400}),
            Element("queue", f"row-{row_id}-{media}q", {"max-size-time": 4_000_000_000, "max-size-buffers": 0,
                                                       "max-size-bytes": 0})]


def row_sink(row_id: str, kind: str, target: str, *, codec: str = "h264", container: str = "mkv") -> list[Element]:
    """The mux and sink a row's appsrcs link to; the first element takes both video and sound."""
    name = f"row-{row_id}"
    if kind == "rtmp":
        mux = Element("eflvmux" if codec != "h264" else "flvmux", f"{name}-mux", {"streamable": True})
        return [mux, Element("rtmp2sink", f"{name}-sink", {"location": target, "async-connect": True, "sync": False})]
    if kind == "srt":
        return [Element("mpegtsmux", f"{name}-mux", {"alignment": 7}),
                Element("srtsink", f"{name}-sink", {"uri": target, "sync": False, "wait-for-connection": False})]
    if kind == "whip":
        return [Element("whipclientsink", f"{name}-sink", {"signaller::whip-endpoint": target})]
    if kind == "file":
        if container == "mp4":
            # Finalised on EOS (moov written at the end); fragmented too, so a crash keeps what was written.
            mux = Element("mp4mux", f"{name}-mux", {"fragment-duration": 2000, "streamable": False})
        else:
            mux = Element("matroskamux", f"{name}-mux", {"writing-app": "Streamy"})
        return [mux, Element("filesink", f"{name}-sink", {"location": target, "async": False, "sync": False})]
    raise ValueError(f"unknown output kind {kind!r}")


def video_adapter(row_id: str, kind: str, codec: str) -> list[Element]:
    """What sits between a row's video queue and its mux: MPEG-TS takes H.264 as byte-stream with
    SPS/PPS before every key frame."""
    if kind == "srt" and codec == "h264":
        return [Element("h264parse", f"row-{row_id}-h264", {"config-interval": -1}),
                Element("capsfilter", f"row-{row_id}-bytestream",
                        {"caps": "video/x-h264,stream-format=byte-stream,alignment=au"})]
    return []


@dataclass
class RowStats:
    """One row's live numbers (the Stats panel reads these at most once a second)."""

    state: str = "off"  # off | connecting | live | reconnecting | finishing | error
    words: str = ""
    bytes: int = 0
    bitrate_kbps: float = 0.0
    frames: int = 0
    dropped_network: int = 0
    dropped_encoder: int = 0
    reconnects: int = 0
    rtt_ms: float | None = None
    started: float | None = None
    history: list = None  # bitrate samples, newest last
    #: The row's video bitrate as set (kb/s), the encoder's now (lower while automatic bitrate holds it
    #: down), whether that is on, and how much the row has waiting to go out (seconds).
    target_kbps: int = 0
    encode_kbps: int = 0
    auto: bool = False
    backlog_s: float = 0.0
    stalls: int = 0

    def __post_init__(self) -> None:
        if self.history is None:
            self.history = []

    @property
    def lowered(self) -> bool:
        """Automatic bitrate holds the encoder below the row's bitrate."""
        return self.auto and 0 < self.encode_kbps < self.target_kbps


HISTORY = 120


def state_words(stats: RowStats) -> str:
    """A row's status in words."""
    if stats.state == "live":
        if stats.lowered:
            return f"Live · {_mb(stats.encode_kbps)} of {_mb(stats.target_kbps)} Mb/s"
        return f"Live · {format_rate(stats.bitrate_kbps)}"
    if stats.state == "connecting":
        return "Connecting…"
    if stats.state == "reconnecting":
        return f"Reconnecting… {stats.words}".strip()
    if stats.state == "finishing":
        return "Finishing…"
    if stats.state == "error":
        return stats.words or "Couldn't connect"
    return stats.words or "Off"


def format_rate(kbps: float) -> str:
    if kbps >= 1000:
        return f"{kbps / 1000:.1f} Mb/s"
    return f"{kbps:.0f} kb/s"


def _mb(kbps: float) -> str:
    """Megabits a second without a unit or a trailing .0: 6, 3.2, 0.8."""
    return f"{kbps / 1000:.1f}".removesuffix(".0")


def video_words(target_kbps: int, size_label: str, fps: int) -> str:
    """A row's picture in words: 6 Mb/s · 1080p30."""
    return f"{_mb(target_kbps)} Mb/s · {size_label}{fps}"


def error_words(message: str, kind: str) -> str:
    """A GStreamer error as one line a person reads."""
    text = message.lower()
    if "stalled" in text:
        return "The network stalled"
    if "refused" in text or "could not connect" in text or "connection" in text and "fail" in text:
        return "The server refused the connection"
    if "resolve" in text or "name or service" in text:
        return "The server's address wasn't found"
    if "timed out" in text or "timeout" in text:
        return "The server didn't answer"
    if "auth" in text or "unauthor" in text or "forbidden" in text or "denied" in text:
        return "The server didn't accept the stream key"
    if "no space" in text:
        return "The disk is full"
    if "permission" in text and kind == "file":
        return "Streamy can't write to that folder"
    return message[:120]


# ── Automatic bitrate ──

#: A row is behind when this much (seconds) waits in its queue and appsrc and it grew since the last
#: second, or when it drops pictures for the network; past HIGH_S it is behind however it moves (the
#: queues hold about 8 s before they drop).
CONGESTED_S = 1.0
HIGH_S = 5.0
#: A row is keeping up while at most this much waits and nothing was dropped.
HEALTHY_S = 0.3
#: Down fast: ×0.75 a step, at most one step every STEP_DOWN_GAP_S (the queue answers the last step
#: first), never below FLOOR of the row's bitrate.
STEP_DOWN = 0.75
STEP_DOWN_GAP_S = 2.0
FLOOR = 0.25
#: Up slowly: STEP_UP of the row's bitrate a step, once the network kept up for STEP_UP_AFTER_S since
#: the last change and UP_AFTER_DOWN_S since the last step down.
STEP_UP = 0.10
STEP_UP_AFTER_S = 10.0
UP_AFTER_DOWN_S = 20.0
#: A live stream whose network bytes haven't moved this long (seconds) while pictures came in is torn
#: down and reconnected by Streamy; servers took about 40 s to reset a stalled connection themselves.
STALL_S = 10.0


@dataclass
class Health:
    """One row's network over the last second."""

    backlog_s: float = 0.0  # waiting in the row's queue and appsrc
    dropped: int = 0  # pictures dropped for the network since the last sample
    live: bool = True  # connected (a row reconnecting says nothing about the network's rate)


@dataclass
class RateControl:
    """One encode's automatic bitrate. ``update`` takes every row's Health once a second and returns the
    bitrate to set now (kb/s) and why, or None.

    Rows that share an encode follow the worst of them: a step costs nothing, while splitting a row off
    to an encode of its own would cost a second encoder (a hardware session or the CPU) and a reconnect
    for that row, and a shaky uplink (the usual cause) slows every row at once. A row that is
    reconnecting doesn't count, and a stalled one is reconnected (STALL_S), so a dead server never holds
    the others down."""

    target: int
    current: int = 0
    _last_down: float = -1e9
    _last_change: float = -1e9
    _healthy_since: float | None = None
    _last_backlog: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.current = self.current or self.target

    @property
    def floor(self) -> int:
        return max(1, int(self.target * FLOOR))

    def retarget(self, target: int, now: float) -> int:
        """The row's bitrate changed (the editor, while live): start from it again."""
        self.target = self.current = target
        self._last_change = now
        self._healthy_since = None
        return self.current

    def update(self, rows: dict[str, Health], now: float) -> tuple[int, str] | None:
        live = {key: h for key, h in rows.items() if h.live}
        if not live:
            self._healthy_since = None
            return None
        behind = []
        for key, h in live.items():
            grew = h.backlog_s > self._last_backlog.get(key, 0.0) + 0.05
            if h.dropped:
                behind.append(f"{key}: {h.dropped} dropped")
            elif h.backlog_s >= HIGH_S or (h.backlog_s >= CONGESTED_S and grew):
                behind.append(f"{key}: {h.backlog_s:.1f} s waiting")
        self._last_backlog = {key: h.backlog_s for key, h in live.items()}
        if behind:
            self._healthy_since = None
            if self.current <= self.floor or now - self._last_down < STEP_DOWN_GAP_S:
                return None
            self.current = max(self.floor, int(self.current * STEP_DOWN))
            self._last_down = self._last_change = now
            return self.current, "behind (" + ", ".join(behind) + ")"
        if max(h.backlog_s for h in live.values()) > HEALTHY_S:
            self._healthy_since = None  # draining: hold
            return None
        if self._healthy_since is None:
            self._healthy_since = now
        if self.current >= self.target:
            return None
        if now - max(self._healthy_since, self._last_change) < STEP_UP_AFTER_S or \
                now - self._last_down < UP_AFTER_DOWN_S:
            return None
        self.current = min(self.target, self.current + max(1, int(self.target * STEP_UP)))
        self._last_change = now
        return self.current, f"keeping up for {now - self._healthy_since:.0f} s"
