#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""An Xwayland window for the window-capture test: one solid colour, hidden
from capture through the _LUMA_WINDOW_HIDDEN_FROM_CAPTURE property, the X11
counterpart of luma_window_capture_v1.

  x11.py TITLE RRGGBB
"""
import ctypes
import sys

import gi

gi.require_version("GdkX11", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import GdkX11, Gtk  # noqa: E402

title, rgb = sys.argv[1], int(sys.argv[2], 16)

window = Gtk.Window(title=title)
window.set_decorated(False)
window.set_default_size(560, 360)
area = Gtk.DrawingArea()


def draw(_area, cr):
    cr.set_source_rgb(((rgb >> 16) & 255) / 255, ((rgb >> 8) & 255) / 255, (rgb & 255) / 255)
    cr.paint()


area.connect("draw", draw)
window.add(area)
window.connect("destroy", Gtk.main_quit)
window.realize()

# XChangeProperty(display, window, atom, CARDINAL, 32, PropModeReplace, [1], 1)
xlib = ctypes.CDLL("libX11.so.6")
xlib.XInternAtom.restype = ctypes.c_ulong
xlib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
xlib.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
xlib.XFlush.argtypes = [ctypes.c_void_p]
xlib.XOpenDisplay.restype = ctypes.c_void_p
xlib.XOpenDisplay.argtypes = [ctypes.c_char_p]
# A connection of its own: any client may set a property on any window.
xdisplay = xlib.XOpenDisplay(None)
xid = GdkX11.X11Window.get_xid(window.get_window())
atom = xlib.XInternAtom(xdisplay, b"_LUMA_WINDOW_HIDDEN_FROM_CAPTURE", 0)
value = (ctypes.c_long * 1)(1)
xlib.XChangeProperty(xdisplay, xid, atom, 6, 32, 0, value, 1)  # XA_CARDINAL, PropModeReplace
xlib.XFlush(xdisplay)
print("hidden-property-set", flush=True)

window.show_all()
Gtk.main()
