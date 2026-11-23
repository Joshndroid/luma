#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""What keeping other scenes' sources open costs: a display shown, two cameras of another scene warm.

Each run is its own process: "cold" opens only the shown scene's sources (as before), "warm" keeps every
scene's open, "sources" runs just the two stand-in cameras' own picture making (a test pattern, one
MJPEG-encoded), which a real camera does in hardware: warm minus cold minus sources is what Streamy adds.
On llvmpipe the GL work (upload, colour conversion) is CPU time too.

    GST_PLUGIN_PATH=<lumaframe> python3 tests/engine_warm_cost.py [seconds]
"""
import json
import os
import subprocess
import sys
import time

os.environ.setdefault("STREAMY_FAKE_SOURCES", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CAMERAS = tuple(c for i, c in enumerate(("video/x-raw,format=YUY2,width=1280,height=720,framerate=30/1",
                                          "image/jpeg,width=1920,height=1080,framerate=30/1"))
                if str(i) in os.environ.get("STREAMY_WARM_CAMERAS", "01"))
DISPLAY = "video/x-raw,format=BGRx,width=1920,height=1080,framerate=30/1"


def usage() -> tuple[float, int, int]:
    """(CPU seconds of this process, RSS in KiB, threads)."""
    with open("/proc/self/stat") as handle:
        fields = handle.read().rsplit(")", 1)[1].split()
    ticks = os.sysconf("SC_CLK_TCK")
    cpu = (int(fields[11]) + int(fields[12])) / ticks
    status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
    return cpu, int(status["VmRSS"].split()[0]), int(status["Threads"])


def measure(seconds: float, run) -> dict:
    run(5.0)  # settle
    cpu0, _rss, _t = usage()
    t0 = time.monotonic()
    run(seconds)
    cpu1, rss, threads = usage()
    return {"cpu_percent": round(100 * (cpu1 - cpu0) / (time.monotonic() - t0), 1), "rss_mib": round(rss / 1024, 1),
            "threads": threads}


def child(mode: str, seconds: float) -> dict:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst

    Gst.init(None)

    def run(s: float) -> None:
        loop = GLib.MainLoop()
        GLib.timeout_add(int(s * 1000), loop.quit)
        loop.run()

    if mode == "sources":
        parts = [f"videotestsrc is-live=true pattern=smpte100 ! {caps.replace('image/jpeg', 'video/x-raw,format=I420')}"
                 + (" ! jpegenc quality=60" if caps.startswith("image/jpeg") else "") + " ! fakesink sync=false"
                 for caps in CAMERAS]
        if not parts:
            return {"cpu_percent": 0.0, "rss_mib": 0.0, "threads": 0}
        pipeline = Gst.parse_launch(" ".join(parts))
        pipeline.set_state(Gst.State.PLAYING)
        result = measure(seconds, run)
        pipeline.set_state(Gst.State.NULL)
        return result
    from luma_streamy.engine import Engine
    from luma_streamy.model import Project

    project = Project.starter()
    shown = project.active_scene
    other = project.add_scene("Cameras")
    display = project.add_widget(shown.id, "display", source={"fake-caps": DISPLAY})
    display.x, display.y, display.width, display.height = 0, 0, 1920, 1080
    if os.environ.get("STREAMY_WARM_DISPLAY"):
        # A second display in the other scene: displays are never parked (a still screen sends nothing new).
        project.add_widget(other.id, "display", source={"fake-caps": DISPLAY})
    for i, caps in enumerate(CAMERAS):
        cam = project.add_widget(other.id, "camera", source={"camera": f"cam-{i}", "fake-caps": caps})
        cam.x, cam.y, cam.width, cam.height = 960 * i, 540, 960, 540
    engine = Engine(fake=True, preview=False)
    if mode == "warm":
        engine.scenes_of = lambda: project.scenes
    engine.start(project)
    run(3.0)
    result = measure(seconds, run)
    result["feeds"] = len(engine.feeds)
    result["shown"] = sum(f.shown for f in engine.feeds.values())
    engine.stop(wait=5.0)
    return result


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    if len(sys.argv) > 2:
        print(json.dumps(child(sys.argv[2], seconds)))
        return 0
    results = {}
    for mode in ("cold", "warm", "sources"):
        out = subprocess.run([sys.executable, "-B", __file__, str(seconds), mode], capture_output=True, text=True)
        line = next((s for s in reversed(out.stdout.splitlines()) if s.startswith("{")), None)
        if line is None:
            print(out.stderr[-2000:], file=sys.stderr)
            return 1
        results[mode] = json.loads(line)
        print(mode, results[mode])
    added = results["warm"]["cpu_percent"] - results["cold"]["cpu_percent"] - results["sources"]["cpu_percent"]
    print(f"{len(CAMERAS)} warm camera(s): +{results['warm']['cpu_percent'] - results['cold']['cpu_percent']:.1f}% CPU in all, "
          f"of which their stand-in picture making {results['sources']['cpu_percent']:.1f}%: Streamy adds "
          f"{added:.1f}% (one core = 100%); memory +{results['warm']['rss_mib'] - results['cold']['rss_mib']:.1f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
