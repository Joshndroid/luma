#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
# An app that reopens its own last documents, the way most editors do: the
# titles of its open windows are kept in $XDG_STATE_HOME/continue-docs.json.
# CONTINUE_DOCS="A,B" opens those instead (the first run).
import json
import os
import sys

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

STATE = os.path.join(GLib.get_user_state_dir(), "continue-docs.json")


class Docs(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.projectluma.ContinueDocs",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.opened = False

    def save(self, *_args):
        titles = [w.get_title() for w in self.get_windows() if w.get_visible()]
        with open(STATE + ".tmp", "w") as f:
            json.dump(titles, f)
        os.replace(STATE + ".tmp", STATE)

    def window(self, title):
        win = Gtk.ApplicationWindow(application=self, title=title)
        win.set_default_size(640, 480)
        win.set_child(Gtk.Label(label=title))
        win.connect("close-request", lambda w: GLib.idle_add(self.save) and False)
        win.present()

    def do_activate(self):
        if self.opened:
            self.window(f"Doc {len(self.get_windows()) + 1}")
            self.save()
            return
        self.opened = True
        wanted = os.environ.get("CONTINUE_DOCS")
        if wanted:
            titles = wanted.split(",")
        else:
            try:
                titles = json.load(open(STATE))
            except (OSError, ValueError):
                titles = ["Untitled"]
        for title in titles:
            self.window(title)
        self.save()


sys.exit(Docs().run(sys.argv[:1]))
