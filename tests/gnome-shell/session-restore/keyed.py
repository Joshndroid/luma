#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
# A LumaUI app (luma_appkit.restore): two windows with the same title,
# told apart only by their restore keys, each with its own state. The keys
# are given in the opposite order to the windows, so matching by order
# would swap them. What a restore hands back goes to keyed-restored.json.
import json
import os
import sys

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402
from luma_appkit import restore  # noqa: E402

OUT = os.environ["LUMA_SESSION_OUT"]


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, key, text):
        super().__init__(application=app, title="Notes")
        self.set_default_size(500, 360)
        self.text = text
        self.set_child(Gtk.Label(label=text))
        self.key = key

    def save_state(self):
        return {"text": self.text}

    def documents(self):
        return [{"uri": f"file:///notes/{self.key}.txt", "title": self.key,
                 "dirty": self.key == "note:2", "unsaved": "draft" if self.key == "note:2" else None}]


class Keyed(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.projectluma.ContinueKeyed",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.restored = []

    def do_startup(self):
        Gtk.Application.do_startup(self)
        restore.enable(self, self.open_window)

    def open_window(self, key, state):
        self.restored.append([key, state])
        window = Window(self, key, state.get("text", ""))
        restore.LumaUI.window_set_restore_key(window, key)
        window.present()
        with open(os.path.join(OUT, "keyed-restored.json"), "w") as f:
            json.dump({"reason": restore.reason(self), "windows": self.restored}, f)
        return window

    def do_activate(self):
        if restore.restore(self):
            return
        one = Window(self, "note:1", "first note")
        two = Window(self, "note:2", "second note")
        for window in (two, one):
            restore.LumaUI.window_set_restore_key(window, window.key)
        one.present()
        two.present()


sys.exit(Keyed().run(sys.argv[:1]))
