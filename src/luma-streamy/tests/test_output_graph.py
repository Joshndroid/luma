# SPDX-License-Identifier: Apache-2.0
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from luma_streamy import output_graph as og  # noqa: E402
from luma_streamy.output_graph import Preset, RowStats  # noqa: E402


class OutputGraphTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(og.kind_of("rtmps://live.twitch.tv/app/k"), "rtmp")
        self.assertEqual(og.kind_of("srt://h:9000?streamid=x"), "srt")
        self.assertEqual(og.kind_of("https://whip.example/endpoint"), "whip")
        with self.assertRaises(ValueError):
            og.kind_of("example.org")

    def test_stream_encoders_hardware_first(self):
        self.assertEqual(og.choose({"openh264enc", "vah264enc", "fdkaacenc"}, "h264").video, "vah264enc")
        self.assertIsNone(og.choose({"fdkaacenc"}, "h264"))

    def test_recording_never_takes_software_av1_over_hardware_h264(self):
        both = {"svtav1enc", "vah264lpenc", "fdkaacenc"}
        self.assertEqual(og.choose_record(both).video, "vah264lpenc")
        self.assertEqual(og.choose_record(both | {"vaav1enc"}).video, "vaav1enc")
        self.assertEqual(og.choose_record({"svtav1enc", "openh264enc"}).codec, "av1")
        self.assertEqual(og.choose_record(both, "av1").video, "svtav1enc")

    def test_encode_ends_in_muxable_caps(self):
        choice = og.choose({"openh264enc", "fdkaacenc"}, "h264")
        video = og.encode_video(choice, Preset("landscape", "h264", 6000, 160))
        self.assertEqual(video[-1].factory, "appsink")
        self.assertIn("stream-format=avc", video[-2].props["caps"])
        self.assertEqual(video[-3].props, {})  # no in-band SPS/PPS in the shared encode
        sound = og.encode_sound(choice, Preset("landscape", "h264", 6000, 160))
        self.assertEqual([e.factory for e in sound][-3:], ["aacparse", "capsfilter", "appsink"])

    def test_row_sinks(self):
        self.assertEqual([e.factory for e in og.row_sink("a", "rtmp", "rtmp://x/app/k")], ["flvmux", "rtmp2sink"])
        self.assertEqual(og.row_sink("a", "rtmp", "rtmp://x", codec="av1")[0].factory, "eflvmux")
        self.assertEqual([e.factory for e in og.row_sink("a", "srt", "srt://x")], ["mpegtsmux", "srtsink"])
        self.assertEqual(og.row_sink("a", "file", "/t/x.mp4", container="mp4")[0].factory, "mp4mux")
        self.assertEqual([e.factory for e in og.video_adapter("a", "srt", "h264")], ["h264parse", "capsfilter"])
        self.assertEqual(og.video_adapter("a", "rtmp", "h264"), [])
        with self.assertRaises(ValueError):
            og.row_sink("a", "ftp", "x")

    def test_words(self):
        stats = RowStats(state="live", bitrate_kbps=6012)
        self.assertEqual(og.state_words(stats), "Live · 6.0 Mb/s")
        self.assertEqual(og.error_words("Could not connect to 1.2.3.4: Connection refused", "rtmp"),
                         "The server refused the connection")
        self.assertEqual(og.error_words("No space left on device", "file"), "The disk is full")

    def test_preset_keys(self):
        self.assertEqual(Preset("landscape", "h264", 6000, 160).key, "landscape-h264-6000-160")
        self.assertEqual(Preset("vertical", "h264", 4000, 160, 720, 60, True).key, "vertical-h264-4000-160-720p-60fps-auto")
        self.assertNotEqual(Preset("landscape", "h264", 6000, 160, auto=True), Preset("landscape", "h264", 6000, 160))

    def test_scaled_encodes(self):
        va = og.choose({"vah264lpenc", "fdkaacenc"}, "h264")
        small = Preset("landscape", "h264", 3000, 160, 720, 60)
        gpu = og.encode_video(va, small, (1920, 1080), gpu_scale=True)
        self.assertEqual([e.factory for e in gpu][:4], ["queue", "vapostproc", "capsfilter", "vah264lpenc"])
        self.assertIn("memory:VAMemory", gpu[2].props["caps"])
        self.assertIn("width=1280,height=720", gpu[2].props["caps"])
        self.assertEqual(gpu[3].props["key-int-max"], 120)  # 2 s at 60 fps
        cpu = og.encode_video(va, small, (1920, 1080))
        self.assertEqual(cpu[1].factory, "videoscale")
        soft = og.encode_video(og.choose({"openh264enc"}, "h264"), small, (1920, 1080))
        self.assertEqual(soft[1].factory, "videoconvertscale")
        self.assertIn("format=I420,width=1280,height=720", soft[2].props["caps"])
        same = og.encode_video(va, Preset("landscape", "h264", 6000, 160), (1920, 1080), gpu_scale=True)
        self.assertEqual([e.factory for e in same][:2], ["queue", "vah264lpenc"])  # its own size: no scale
        self.assertEqual(og.row_head("a", "video", 60)[0].props["max-buffers"], 240)

    def test_live_words(self):
        stats = RowStats(state="live", bitrate_kbps=3150, target_kbps=6000, encode_kbps=3200, auto=True)
        self.assertEqual(og.state_words(stats), "Live · 3.2 of 6 Mb/s")
        stats.encode_kbps = 6000
        self.assertEqual(og.state_words(stats), "Live · 3.1 Mb/s")
        self.assertEqual(og.video_words(6000, "720p", 60), "6 Mb/s · 720p60")
        self.assertEqual(og.error_words("stalled: nothing went out for 10 s", "rtmp"), "The network stalled")


class RateControlTest(unittest.TestCase):
    def run_seconds(self, rate, health, seconds, start):
        steps = []
        for t in range(seconds):
            step = rate.update({"a": health(t)}, start + t)
            if step:
                steps.append((start + t, step[0]))
        return steps

    def test_steps_down_fast_and_up_slowly(self):
        rate = og.RateControl(6000)
        # Healthy: nothing changes.
        self.assertEqual(self.run_seconds(rate, lambda t: og.Health(0.1, 0), 30, 0), [])
        # Behind and growing for 10 s: a 25% step every 2 s, down to the floor (25%), never below.
        steps = self.run_seconds(rate, lambda t: og.Health(1.0 + t * 0.5, 0), 12, 100)
        self.assertEqual([k for _t, k in steps], [4500, 3375, 2531, 1898, 1500])
        self.assertEqual(steps[1][0] - steps[0][0], 2)
        # Draining (still waiting, shrinking): hold.
        self.assertEqual(self.run_seconds(rate, lambda t: og.Health(4.0 - t * 0.3, 0), 10, 200), [])
        # Healthy again: 10% of the target a step, 10 s apart, the first 20 s after the last step down.
        steps = self.run_seconds(rate, lambda t: og.Health(0.0, 0), 200, 300)
        self.assertEqual(steps[0][1], 2100)
        self.assertGreaterEqual(steps[1][0] - steps[0][0], 10)
        self.assertEqual(steps[-1][1], 6000)
        self.assertEqual(rate.current, 6000)

    def test_drops_and_a_full_queue_step_down(self):
        rate = og.RateControl(6000)
        self.assertEqual(rate.update({"a": og.Health(0.0, 3)}, 0)[0], 4500)
        self.assertIsNone(rate.update({"a": og.Health(0.0, 3)}, 1))  # one step every 2 s
        self.assertEqual(rate.update({"a": og.Health(6.0, 0)}, 2)[0], 3375)  # past HIGH_S, even if not growing
        self.assertEqual(rate.update({"a": og.Health(6.0, 0)}, 4)[0], 2531)

    def test_follows_the_worst_live_row(self):
        rate = og.RateControl(6000)
        rows = {"good": og.Health(0.0, 0), "slow": og.Health(0.0, 2), "gone": og.Health(9.0, 50, live=False)}
        self.assertEqual(rate.update(rows, 0)[0], 4500)
        rows["slow"] = og.Health(0.0, 0)
        self.assertIsNone(rate.update(rows, 2))  # the reconnecting row doesn't hold the others down
        self.assertIsNone(rate.update({"gone": og.Health(9.0, 9, live=False)}, 4))

    def test_retarget(self):
        rate = og.RateControl(6000)
        rate.update({"a": og.Health(0.0, 1)}, 0)
        self.assertEqual(rate.retarget(3000, 1), 3000)
        self.assertEqual((rate.target, rate.current, rate.floor), (3000, 3000, 750))


if __name__ == "__main__":
    unittest.main()
