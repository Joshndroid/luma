#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Automatic bitrate and stall reconnects against a real RTMP server behind a throttling proxy.

One stream row (automatic bitrate on) goes to an RTMP server through a TCP proxy in this process that
passes everything, then only THROTTLE_KBPS, then everything again, then nothing at all (a stalled
network: the connection stays open and no byte moves). Asserts the encode steps its bitrate down while
throttled and back up to the row's bitrate once the network keeps up, and that the stalled row is torn
down and reconnected by Streamy within og.STALL_S (plus the socket's own buffer) and goes live again.

    python3 tests/outputs_network.py rtmp://127.0.0.1:1935/live/net

The server address's host and port are proxied. The server must wait longer than og.STALL_S for a
silent publisher (mediamtx: readTimeout 60s), as the servers seen live did (~40 s). The recovery waits
are shortened (STEP_UP_AFTER_S 2 s,
UP_AFTER_DOWN_S 4 s) so the climb back takes seconds, not minutes. Needs an H.264 encoder whose bitrate
changes while it runs (openh264enc, vah264enc) and an AAC encoder.
"""
import os
import socket
import sys
import threading
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import gi  # noqa: E402

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

from luma_streamy import output_graph as og  # noqa: E402
from luma_streamy import outputs as om  # noqa: E402
from luma_streamy.output_graph import Preset  # noqa: E402

SERVER = sys.argv[1] if len(sys.argv) > 1 else "rtmp://127.0.0.1:1935/live/net"
TARGET_KBPS = int(os.environ.get("TARGET_KBPS", "2500"))
THROTTLE_KBPS = int(os.environ.get("THROTTLE_KBPS", "700"))
og.STEP_UP_AFTER_S, og.UP_AFTER_DOWN_S = 2.0, 4.0

lines: list[str] = []
_log = om.log
om.log = lambda text: (lines.append(f"{time.monotonic() - T0:6.1f} {text}"), _log(text))
T0 = time.monotonic()


class Throttle:
    """A TCP proxy: "open" passes everything, "slow" passes ``kbps`` towards the server, "stall" moves
    nothing either way. Small socket buffers on its side, so the sender feels it at once."""

    def __init__(self, upstream: tuple[str, int]) -> None:
        self.upstream, self.mode, self.kbps = upstream, "open", 0
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(4)
        self.port = self.listener.getsockname()[1]
        self.connections = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            client, _addr = self.listener.accept()
            self.connections += 1
            try:
                server = socket.create_connection(self.upstream, timeout=5)
                server.settimeout(None)
            except OSError as error:
                print(f"proxy: the server didn't take the connection: {error}", flush=True)
                client.close()
                continue
            for a, b, up in ((client, server, True), (server, client, False)):
                threading.Thread(target=self._pump, args=(a, b, up), daemon=True).start()

    def _hold(self) -> None:
        while self.mode == "stall":
            time.sleep(0.02)

    def _pump(self, src: socket.socket, dst: socket.socket, up: bool) -> None:
        budget, last = 0.0, time.monotonic()
        try:
            while True:
                if self.mode == "stall":
                    time.sleep(0.02)
                    last = time.monotonic()
                    continue
                size = 16384
                if up and self.mode == "slow":
                    now = time.monotonic()
                    budget = min(budget + (now - last) * self.kbps * 125, self.kbps * 125 * 0.1)
                    last = now
                    if budget < 1024:
                        time.sleep(0.01)
                        continue
                    size = int(min(size, budget))
                data = src.recv(size)
                self._hold()  # a stall began while this waited: nothing (not even an end) gets through
                if not data:
                    why = "end"
                    break
                if up and self.mode == "slow":
                    budget -= len(data)
                dst.sendall(data)
        except OSError as error:
            why = str(error)
            self._hold()
        print(f"proxy: {'to the server' if up else 'from the server'} closed ({why})", flush=True)
        for s in (src, dst):
            try:
                s.close()
            except OSError:
                pass


def main() -> int:
    Gst.init(None)
    url = urlparse(SERVER)
    proxy = Throttle((url.hostname, url.port or 1935))
    target = f"rtmp://127.0.0.1:{proxy.port}{url.path}"
    loop = GLib.MainLoop()
    outputs = om.Outputs()
    # Moving bars: the encoder spends about its bitrate on them (noise overshoots it tenfold).
    feed = Gst.parse_launch(
        "videotestsrc is-live=true pattern=smpte horizontal-speed=3 ! video/x-raw,format=NV12,width=640,height=360,framerate=30/1 ! "
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
    states: list[tuple[float, str]] = []
    outputs.connect("row-state", lambda _o, _row, state, words: states.append((time.monotonic() - T0, state)) or
                    print(f"{time.monotonic() - T0:6.1f} row: {state} {words}", flush=True))
    errors: list[str] = []
    timeline: list[str] = []
    phase = {"name": "start", "since": time.monotonic()}

    def encode():
        row = outputs.rows.get("net")
        return row.group if row is not None else None

    def sample() -> bool:
        row, enc = outputs.rows.get("net"), encode()
        if row is not None and enc is not None:
            timeline.append(f"{time.monotonic() - T0:6.1f} {phase['name']:7} {row.stats.state:12} "
                            f"encoder {enc.current_kbps:5d}/{enc.target_kbps} kb/s, sent {row.stats.bitrate_kbps:6.0f} "
                            f"kb/s, waiting {row.stats.backlog_s:4.1f} s, dropped {row.stats.dropped_network}")
        return True

    def wait_until(what, seconds: float, then, why: str):
        started = time.monotonic()

        def check() -> bool:
            if what():
                then()
                return False
            if time.monotonic() - started > seconds:
                errors.append(f"{why} (after {seconds:.0f} s in {phase['name']})")
                loop.quit()
                return False
            return True

        GLib.timeout_add(200, check)

    def to(name: str) -> None:
        phase.update(name=name, since=time.monotonic())
        print(f"{time.monotonic() - T0:6.1f} == {name}", flush=True)

    marks: dict[str, float] = {}

    def start() -> bool:
        outputs.start("net", Preset("landscape", "h264", TARGET_KBPS, 128, auto=True), target)
        wait_until(lambda: outputs.stats("net") and outputs.stats("net").state == "live", 30, open_phase,
                   "the row never went live")
        return False

    def open_phase() -> None:
        to("open")
        enc = encode()
        if enc is None or enc.rate is None:
            errors.append(f"automatic bitrate is off on {enc.choice.video if enc else '?'}")
            loop.quit()
            return
        GLib.timeout_add_seconds(8, slow_phase)

    def slow_phase() -> bool:
        if encode().current_kbps != TARGET_KBPS:
            errors.append(f"stepped down on an open network: {encode().current_kbps}")
        to("slow")
        proxy.mode, proxy.kbps = "slow", THROTTLE_KBPS
        wait_until(lambda: encode().current_kbps <= TARGET_KBPS * og.STEP_DOWN ** 2, 40, held,
                   "the bitrate did not step down while throttled")
        return False

    def held() -> None:
        marks["down"] = time.monotonic() - phase["since"]
        GLib.timeout_add_seconds(10, recover_phase)

    def recover_phase() -> bool:
        marks["low"] = encode().current_kbps
        if outputs.stats("net").state != "live":
            errors.append(f"throttled (not stalled) row left live: {outputs.stats('net').state}")
        to("recover")
        proxy.mode = "open"
        wait_until(lambda: encode().current_kbps == TARGET_KBPS, 90, recovered,
                   "the bitrate did not climb back to the row's")
        return False

    def recovered() -> None:
        marks["up"] = time.monotonic() - phase["since"]
        GLib.timeout_add_seconds(3, stall_phase)

    def stall_phase() -> bool:
        to("stall")
        marks["connections"] = proxy.connections
        proxy.mode = "stall"
        wait_until(lambda: outputs.stats("net").stalls >= 1, og.STALL_S + 20, stalled,
                   "a stalled row was not reconnected by Streamy")
        return False

    def stalled() -> None:
        marks["stalled"] = time.monotonic() - phase["since"]
        to("resume")
        proxy.mode = "open"
        wait_until(lambda: outputs.stats("net").state == "live" and proxy.connections > marks["connections"],
                   40, resumed, "the stalled row did not go live again")

    def resumed() -> None:
        marks["resumed"] = time.monotonic() - phase["since"]
        outputs.stop_all()
        wait_until(lambda: not outputs.rows, 20, loop.quit, "the row did not stop")

    feed.set_state(Gst.State.PLAYING)
    GLib.timeout_add(500, start)
    GLib.timeout_add_seconds(1, sample)
    GLib.timeout_add_seconds(300, lambda: (errors.append("timed out"), loop.quit()))
    loop.run()
    outputs.close_now()
    feed.set_state(Gst.State.NULL)
    print("\n".join(timeline[::2]))
    print("\n".join(line for line in lines if "automatic bitrate" in line or "stalled" in line or
                    "torn down" in line or "reconnect" in line))
    print(f"marks: {marks}")
    if "down" in marks and marks["down"] > 20:
        errors.append(f"stepping down took {marks['down']:.0f} s")
    if "stalled" in marks and marks["stalled"] > og.STALL_S + 12:
        errors.append(f"the stall took {marks['stalled']:.0f} s to reconnect")
    if not any("automatic bitrate" in line and "->" in line for line in lines):
        errors.append("no automatic bitrate step was logged")
    for error in errors:
        print(f"FAIL: {error}", flush=True)
    if not errors:
        print(f"ok: down to {marks.get('low')} kb/s in {marks['down']:.0f} s, back to {TARGET_KBPS} in "
              f"{marks['up']:.0f} s, a stall reconnected after {marks['stalled']:.0f} s and was live again "
              f"{marks['resumed']:.0f} s later", flush=True)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
