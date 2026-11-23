# SPDX-License-Identifier: Apache-2.0
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from luma_streamy.destinations import (  # noqa: E402
    DestinationStore, new, output_size, scaled, shared_encodes, size_label,
)


class DestinationTest(unittest.TestCase):
    def test_store_keeps_rows_and_a_recording(self):
        path = Path(tempfile.mkdtemp()) / "destinations.json"
        store = DestinationStore.load(path)
        self.assertEqual([r.platform for r in store.rows], ["record"])
        twitch = store.add("twitch")
        store.add("youtube", "vertical")
        again = DestinationStore.load(path)
        self.assertEqual([r.platform for r in again.rows], ["twitch", "youtube", "record"])
        self.assertEqual(again.get(twitch.id).canvas, "landscape")
        again.remove(again.rows[-1].id)  # the recording row stays
        self.assertEqual(len(again.rows), 3)

    def test_urls(self):
        self.assertEqual(new("twitch").url("abc"), "rtmps://live.twitch.tv/app/abc")
        self.assertEqual(new("restream").url("re_1"), "rtmp://live.restream.io/live/re_1")
        self.assertTrue(new("restream").is_stream)
        self.assertEqual(new("x").url("x_1"), "rtmp://va.pscp.tv:80/x/x_1")
        self.assertTrue(new("x").is_stream)
        row = new("custom")
        row.server = "srt://example.org:9000?streamid=x"
        self.assertEqual(row.url("ignored"), "srt://example.org:9000?streamid=x")
        row.server = "example.org"
        with self.assertRaises(ValueError):
            row.url("k")

    def test_one_encode_per_canvas_and_preset(self):
        a, b, c = new("twitch"), new("custom"), new("twitch", "vertical")
        off = new("kick")
        off.enabled = False
        groups = shared_encodes([a, b, c, off, new("record")])
        self.assertEqual(sorted(len(g) for g in groups.values()), [1, 2])

    def test_output_follows_the_platform_until_set(self):
        row = new("youtube")
        self.assertEqual(row.output(), (8000, 160, 0, 30, True))
        row.video_kbps, row.audio_kbps, row.size, row.fps, row.auto_bitrate = 4500, 192, 720, 60, False
        self.assertEqual(row.output(), (4500, 192, 720, 60, False))
        self.assertEqual(row.preset(), ("landscape", "h264", 4500, 192, 720, 60, False))
        row.size = 1080  # the canvas's own size is no scale
        self.assertEqual(row.output()[2], 0)
        record = new("record")
        record.auto_bitrate = True  # a recording has no network to follow
        self.assertEqual(record.output(), (12000, 192, 0, 30, False))

    def test_sizes(self):
        self.assertEqual(output_size("landscape", 0), (1920, 1080))
        self.assertEqual(output_size("landscape", 720), (1280, 720))
        self.assertEqual(output_size("landscape", 480), (854, 480))
        self.assertEqual(output_size("vertical", 720), (720, 1280))
        self.assertEqual(output_size("vertical", 2160), (1080, 1920))
        self.assertEqual(scaled(1280, 720, 480), (854, 480))
        self.assertEqual(size_label("vertical", 0), "1080p")
        self.assertEqual(size_label("landscape", 720), "720p")

    def test_a_different_picture_is_a_different_encode(self):
        a, b, c, d = new("twitch"), new("kick"), new("restream"), new("custom")
        c.size = 720
        d.fps = 60
        groups = shared_encodes([a, b, c, d])
        self.assertEqual(sorted(len(g) for g in groups.values()), [1, 1, 2])
        b.auto_bitrate = False  # a fixed-rate row never follows a slow one
        self.assertEqual(len(shared_encodes([a, b])), 2)

    def test_store_keeps_output_settings(self):
        path = Path(tempfile.mkdtemp()) / "destinations.json"
        store = DestinationStore.load(path)
        row = store.add("restream")
        row.video_kbps, row.size, row.fps, row.auto_bitrate = 3500, 720, 60, False
        store.update(row)
        again = DestinationStore.load(path).get(row.id)
        self.assertEqual((again.video_kbps, again.size, again.fps, again.auto_bitrate), (3500, 720, 60, False))
        path.write_text('{"rows": [{"id": "old1", "platform": "twitch"}]}')  # saved before these settings
        self.assertEqual(DestinationStore.load(path).get("old1").output(), (6000, 160, 0, 30, True))


if __name__ == "__main__":
    unittest.main()
