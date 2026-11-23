# SPDX-License-Identifier: Apache-2.0
"""Sources shared between scenes: linked widgets, paste, unlink, the transition and old projects."""
import json
import unittest

from luma_streamy.model import Project


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self.project = Project.starter()
        self.one = self.project.active_scene
        self.two = self.project.add_scene("Two")
        self.cam = self.project.add_widget(self.one.id, "camera", source={"camera": "v4l2_input.cam"})
        self.cam.x, self.cam.y, self.cam.width, self.cam.height = 1400, 700, 480, 270

    def test_paste_linked_shares_the_source_not_the_placement(self):
        pasted = self.project.paste_widget(self.two.id, self.cam)
        self.assertNotEqual(pasted.id, self.cam.id)
        self.assertEqual(pasted.link, self.cam.link)
        self.assertEqual((pasted.x, pasted.y, pasted.width, pasted.height), (1400, 700, 480, 270))
        self.project.set_rect(pasted.id, 0, 0, 1920, 1080)
        pasted.effects.corner_radius = 0
        self.assertEqual((self.cam.x, self.cam.width), (1400, 480))
        self.assertGreater(self.cam.effects.corner_radius, 0)
        pasted.name = "Face"
        pasted.source["mode"] = [1280, 720, 30]
        self.assertEqual(self.cam.name, "Face")
        self.assertEqual(self.cam.source["mode"], [1280, 720, 30])
        self.assertTrue(self.project.is_linked(self.cam))
        self.assertEqual([s.name for s in self.project.other_scenes(self.cam)], ["Two"])
        self.assertEqual(len(self.project.sources), 1)

    def test_paste_as_new_has_its_own_source(self):
        display = self.project.add_widget(self.one.id, "display", source={"restore-token": "t1"})
        twin = self.project.paste_widget(self.two.id, display, linked=False)
        self.assertNotEqual(twin.link, display.link)
        self.assertNotIn("restore-token", twin.source)  # a share of its own
        twin.name = "Other"
        self.assertEqual(display.name, "Display")
        self.assertFalse(self.project.is_linked(display))

    def test_paste_in_the_same_scene_lands_beside(self):
        again = self.project.paste_widget(self.one.id, self.cam)
        self.assertEqual((again.x, again.y), (1440, 740))
        self.assertFalse(self.project.is_linked(self.cam))  # linked means used in another scene

    def test_unlink_keeps_this_widget_on_its_source(self):
        display = self.project.add_widget(self.one.id, "display", source={"restore-token": "t1"})
        pasted = self.project.paste_widget(self.two.id, display)
        self.project.unlink(display.id)
        self.assertEqual(display.source.get("restore-token"), "t1")
        self.assertNotEqual(pasted.link, display.link)
        self.assertNotIn("restore-token", pasted.source)
        pasted.name = "Slides"
        self.assertEqual(display.name, "Display")
        self.assertFalse(self.project.is_linked(display))

    def test_delete_last_use_drops_the_source(self):
        pasted = self.project.paste_widget(self.two.id, self.cam)
        self.project.remove_widget(self.cam.id)
        self.assertIn(pasted.link, self.project.sources)
        # Its source is still known by the deleted widget's id (its capture's key).
        self.assertIs(self.project.find_widget(self.cam.link)[1], pasted)
        self.project.remove_widget(pasted.id)
        self.assertEqual(self.project.sources, {})

    def test_duplicate_scene_links(self):
        copy_ = self.project.duplicate_scene(self.one.id)
        twin = copy_.widgets[0]
        self.assertEqual(twin.link, self.cam.link)
        self.assertNotEqual(twin.id, self.cam.id)
        twin.x = 0
        self.assertEqual(self.cam.x, 1400)

    def test_round_trip_keeps_links_and_transition(self):
        pasted = self.project.paste_widget(self.two.id, self.cam)
        pasted.width = 1920
        self.project.transition, self.project.transition_ms = "cut", 500
        data = json.loads(json.dumps(self.project.to_dict()))
        again = Project.from_dict(data)
        a, b = again.scene(self.one.id).widgets[0], again.scene(self.two.id).widgets[0]
        self.assertIs(a.shared, b.shared)
        self.assertEqual((a.width, b.width), (480, 1920))
        self.assertEqual((again.transition, again.transition_ms), ("cut", 500))
        # A Streamy that reads no "sources" still finds each widget's own settings.
        self.assertEqual(data["scenes"][1]["widgets"][0]["source"], {"camera": "v4l2_input.cam"})

    def test_old_projects_merge_identical_sources(self):
        mic = self.project.add_widget(self.one.id, "microphone", source={"device": "alsa_input.usb"})
        display = self.project.add_widget(self.one.id, "display")  # nothing chosen: never merged
        data = self.project.to_dict()
        copy_ = json.loads(json.dumps(data["scenes"][0]))
        copy_["id"], copy_["name"] = "scene-copy", "Copy"
        for i, raw in enumerate(copy_["widgets"]):
            raw["id"] = f"copy-{i}"
        copy_["widgets"][0]["canvases"]["landscape"]["rect"] = [0, 0, 1920, 1080]
        copy_["widgets"][1]["volume_db"] = -6.0  # not identical any more
        data["scenes"].append(copy_)
        for scene in data["scenes"]:
            for raw in scene["widgets"]:
                raw.pop("link")
        del data["sources"]
        old = Project.from_dict(data)
        cam, mic2, display2 = old.scene("scene-copy").widgets
        self.assertEqual(cam.link, self.cam.id)
        self.assertEqual(cam.width, 1920)
        self.assertEqual(old.find_widget(self.cam.id)[1].width, 480)
        self.assertNotEqual(mic2.link, mic.id)
        self.assertNotEqual(display2.link, display.id)


if __name__ == "__main__":
    unittest.main()
