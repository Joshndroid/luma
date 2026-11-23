#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
# An app that remembers nothing: every activation opens "Counter N".
import sys

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, Gtk  # noqa: E402


class Counter(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.projectluma.ContinueCounter",
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.count = 0

    def do_activate(self):
        self.count += 1
        win = Gtk.ApplicationWindow(application=self, title=f"Counter {self.count}")
        win.set_default_size(560, 420)
        win.present()


sys.exit(Counter().run(sys.argv[:1]))
