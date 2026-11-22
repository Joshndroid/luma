/* SPDX-License-Identifier: Apache-2.0 */
#pragma once

#include <gtk/gtk.h>

G_BEGIN_DECLS

/**
 * luma_window_set_hidden_from_capture:
 * @window: a toplevel window
 * @hidden: whether captures of the screen leave @window out
 *
 * Keeps @window out of captures of the screen: screen casts of a display or
 * an area, and screenshots of the screen, show what is behind the window
 * instead. The window stays on the screen as usual. Its dialogs, menus and
 * popovers are left out with it. A share of the window itself, which the
 * person picks, still shows it, and Luma's share picker marks it "Hidden
 * from screen sharing".
 *
 * For a window that would otherwise capture itself, such as a recorder's
 * controls or a streaming app's preview, which fills a capture of the
 * display it is on with copies of itself. Hiding only removes content from
 * captures, so it needs no permission.
 *
 * The call may come before or after the window is shown; the setting
 * survives hiding and showing the window. On a compositor without Luma's
 * window-capture protocol (another Wayland compositor, X11, macOS, Windows)
 * this does nothing and returns %FALSE.
 *
 * Returns: %TRUE when the compositor hides the window (with @hidden
 *   %TRUE); %FALSE when it cannot, or when showing it again
 */
gboolean luma_window_set_hidden_from_capture(GtkWindow *window,
                                             gboolean hidden);

/**
 * luma_window_get_hidden_from_capture:
 * @window: a toplevel window
 *
 * Returns: what luma_window_set_hidden_from_capture() last asked for
 *   @window
 */
gboolean luma_window_get_hidden_from_capture(GtkWindow *window);

/**
 * luma_window_get_capture_hiding_supported:
 * @window: a toplevel window
 *
 * Returns: whether the compositor of @window's display can keep a window
 *   out of captures
 */
gboolean luma_window_get_capture_hiding_supported(GtkWindow *window);

G_END_DECLS
