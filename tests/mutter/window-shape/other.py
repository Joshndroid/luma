#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""An ordinary GTK 3, GTK 4 or libadwaita window for the window-shape test.

It logs its size after every layout so the driver can check that windows
which never asked for an aspect ratio resize exactly as before.
gtk3-x11-aspect is a GTK 3 window on X11 with a single aspect ratio (4:3)
in its WM_NORMAL_HINTS.
"""
import json
import sys

toolkit, name, log_path = sys.argv[1:4]
log = open(log_path, "a")


def emit(width, height):
    log.write(json.dumps({"ev": "size", "w": width, "h": height}) + "\n")
    log.flush()


if toolkit in ("gtk3", "gtk3-x11-aspect"):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, Gtk
    window = Gtk.Window(title=name)
    if toolkit == "gtk3-x11-aspect":
        # An X11 client with one aspect ratio in WM_NORMAL_HINTS.
        geometry = Gdk.Geometry()
        geometry.min_aspect = geometry.max_aspect = 4 / 3
        window.set_geometry_hints(None, geometry, Gdk.WindowHints.ASPECT)
    header = Gtk.HeaderBar(show_close_button=True, title=name)
    window.set_titlebar(header)
    window.set_default_size(520, 360)
    window.add(Gtk.Label(label=name))
    window.connect("size-allocate", lambda w, a: emit(w.get_allocated_width(), w.get_allocated_height()))
    window.connect("destroy", Gtk.main_quit)
    window.show_all()
    log.write(json.dumps({"ev": "ready"}) + "\n")
    log.flush()
    Gtk.main()
else:
    import gi
    gi.require_version("Gtk", "4.0")
    if toolkit == "adw":
        gi.require_version("Adw", "1")
        from gi.repository import Adw as Toolkit
    else:
        from gi.repository import Gtk as Toolkit
    from gi.repository import Gtk

    def activate(app):
        if toolkit == "adw":
            window = Toolkit.ApplicationWindow(application=app, title=name)
            view = Toolkit.ToolbarView()
            view.add_top_bar(Toolkit.HeaderBar())
            view.set_content(Gtk.Label(label=name))
            window.set_content(view)
        else:
            window = Gtk.ApplicationWindow(application=app, title=name)
            window.set_titlebar(Gtk.HeaderBar())
            window.set_child(Gtk.Label(label=name))
        window.set_default_size(520, 360)
        last = [None]

        def tick(widget, _clock):
            size = (widget.get_width(), widget.get_height())
            if size != last[0]:
                last[0] = size
                emit(*size)
            return True

        window.add_tick_callback(tick)
        window.present()
        log.write(json.dumps({"ev": "ready"}) + "\n")
        log.flush()

    from gi.repository import Gio
    app = (Toolkit.Application if toolkit == "adw" else Gtk.Application)(
        application_id=f"org.projectluma.WindowShapeOther.{name}",
        flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.connect("activate", activate)
    app.run([])
