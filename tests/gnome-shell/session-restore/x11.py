#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
# A plain X11 client (Xwayland): one window, WM_CLASS ContinueX11.
import time

from Xlib import X, display, Xatom

d = display.Display()
screen = d.screen()
win = screen.root.create_window(0, 0, 500, 400, 0, screen.root_depth, X.InputOutput, X.CopyFromParent,
                                background_pixel=screen.white_pixel, event_mask=X.ExposureMask)
win.set_wm_name("X11 window")
win.set_wm_class("continue-x11", "ContinueX11")
win.change_property(d.intern_atom("_NET_WM_NAME"), d.intern_atom("UTF8_STRING"), 8, b"X11 window")
win.change_property(d.intern_atom("_NET_WM_PID"), Xatom.CARDINAL, 32, [__import__("os").getpid()])
win.map()
d.flush()
while True:
    while d.pending_events():
        d.next_event()
    time.sleep(0.05)
