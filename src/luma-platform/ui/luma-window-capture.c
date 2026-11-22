/* SPDX-License-Identifier: Apache-2.0 */
#include "luma-window-capture.h"

/* Binds luma_window_capture_v1 (src/luma-platform/protocols), which Luma's
 * Mutter implements. Off Wayland, or on a compositor without the protocol,
 * every call is a no-op and luma_window_set_hidden_from_capture() says so.
 * X11 clients use the _LUMA_WINDOW_HIDDEN_FROM_CAPTURE property instead. */
#ifdef LUMA_HAVE_WAYLAND
#include <gdk/wayland/gdkwayland.h>
#include <wayland-client.h>

#include "luma-window-capture-v1-client-protocol.h"
#else
struct wl_registry;
struct luma_window_capture_manager_v1;
struct luma_window_capture_v1;
#endif

typedef struct {
  struct wl_registry *registry;
  struct luma_window_capture_manager_v1 *manager;
  guint32 manager_name;
} CaptureDisplay;

typedef struct {
  GtkWindow *window; /* owns this binding */
  GdkSurface *surface;
  CaptureDisplay *display;
  struct luma_window_capture_v1 *capture;
  gboolean hidden;
  /* What the compositor was last told: -1 when nothing was sent. */
  int sent;
} WindowCapture;

static GQuark display_quark;
static GQuark window_quark;

#ifdef LUMA_HAVE_WAYLAND
static void
registry_global(void *data, struct wl_registry *registry, guint32 name,
                const char *interface, guint32 version G_GNUC_UNUSED) {
  CaptureDisplay *self = data;

  if (self->manager == NULL &&
      g_str_equal(interface, luma_window_capture_manager_v1_interface.name)) {
    self->manager_name = name;
    self->manager = wl_registry_bind(
        registry, name, &luma_window_capture_manager_v1_interface, 1);
  }
}

static void
registry_global_remove(void *data, struct wl_registry *registry G_GNUC_UNUSED,
                       guint32 name) {
  CaptureDisplay *self = data;

  if (name != self->manager_name)
    return;
  /* Capture objects already made stay valid until their windows unrealize. */
  g_clear_pointer(&self->manager, luma_window_capture_manager_v1_destroy);
  self->manager_name = 0;
}

static const struct wl_registry_listener registry_listener = {
  registry_global,
  registry_global_remove,
};

static void
capture_display_free(gpointer data) {
  CaptureDisplay *self = data;

  g_clear_pointer(&self->manager, luma_window_capture_manager_v1_destroy);
  g_clear_pointer(&self->registry, wl_registry_destroy);
  g_free(self);
}
#endif

static CaptureDisplay *
capture_display_for(GdkDisplay *display) {
#ifndef LUMA_HAVE_WAYLAND
  (void)display;
  (void)display_quark;
  return NULL;
#else
  CaptureDisplay *self;
  struct wl_display *wl_display;

  if (display == NULL || !GDK_IS_WAYLAND_DISPLAY(display))
    return NULL;

  if (display_quark == 0)
    display_quark = g_quark_from_static_string("luma-window-capture-display");
  self = g_object_get_qdata(G_OBJECT(display), display_quark);
  if (self != NULL)
    return self;

  self = g_new0(CaptureDisplay, 1);
  wl_display = gdk_wayland_display_get_wl_display(display);
  self->registry = wl_display_get_registry(wl_display);
  wl_registry_add_listener(self->registry, &registry_listener, self);
  /* One discovery per display, on GDK's own connection; GDK keeps
   * dispatching it. */
  if (wl_display_roundtrip(wl_display) < 0) {
    capture_display_free(self);
    return NULL;
  }
  g_object_set_qdata_full(G_OBJECT(display), display_quark, self,
                          capture_display_free);
  return self;
#endif
}

static void
window_capture_sync(WindowCapture *self) {
#ifdef LUMA_HAVE_WAYLAND
  if (self->surface == NULL || self->display == NULL)
    return;

  if (self->capture == NULL) {
    struct wl_surface *wl_surface;

    /* A window that was never hidden needs no object. */
    if (!self->hidden || self->display->manager == NULL)
      return;
    wl_surface = gdk_wayland_surface_get_wl_surface(self->surface);
    if (wl_surface == NULL)
      return;
    self->capture = luma_window_capture_manager_v1_get_window_capture(
        self->display->manager, wl_surface);
    self->sent = -1;
  }

  if (self->sent == self->hidden)
    return;
  luma_window_capture_v1_set_visibility(
      self->capture, self->hidden ? LUMA_WINDOW_CAPTURE_V1_VISIBILITY_HIDDEN
                                  : LUMA_WINDOW_CAPTURE_V1_VISIBILITY_VISIBLE);
  self->sent = self->hidden;

  /* The request is double-buffered: make sure a commit carries it. */
  gdk_wayland_surface_force_next_commit(self->surface);
  gdk_surface_queue_render(self->surface);
#else
  (void)self;
#endif
}

static void
destroy_capture(WindowCapture *self) {
#ifdef LUMA_HAVE_WAYLAND
  g_clear_pointer(&self->capture, luma_window_capture_v1_destroy);
#endif
  self->sent = -1;
}

static void
window_realized(GtkWidget *widget G_GNUC_UNUSED, WindowCapture *self) {
  self->surface = gtk_native_get_surface(GTK_NATIVE(self->window));
  if (self->surface == NULL)
    return;
  self->display = capture_display_for(gdk_surface_get_display(self->surface));
  window_capture_sync(self);
}

static void
window_unrealized(GtkWidget *widget G_GNUC_UNUSED, WindowCapture *self) {
  destroy_capture(self);
  self->surface = NULL;
}

static void
window_capture_free(gpointer data) {
  WindowCapture *self = data;

  /* The window is being finalized: its surface is gone already. */
  destroy_capture(self);
  g_free(self);
}

static WindowCapture *
window_capture_for(GtkWindow *window, gboolean create) {
  WindowCapture *self;

  if (window_quark == 0)
    window_quark = g_quark_from_static_string("luma-window-capture");
  self = g_object_get_qdata(G_OBJECT(window), window_quark);
  if (self != NULL || !create)
    return self;

  self = g_new0(WindowCapture, 1);
  self->window = window;
  self->sent = -1;
  g_object_set_qdata_full(G_OBJECT(window), window_quark, self,
                          window_capture_free);
  g_signal_connect(window, "realize", G_CALLBACK(window_realized), self);
  g_signal_connect(window, "unrealize", G_CALLBACK(window_unrealized), self);
  if (gtk_widget_get_realized(GTK_WIDGET(window)))
    window_realized(GTK_WIDGET(window), self);
  return self;
}

gboolean
luma_window_get_capture_hiding_supported(GtkWindow *window) {
  CaptureDisplay *display;

  g_return_val_if_fail(GTK_IS_WINDOW(window), FALSE);
  display = capture_display_for(gtk_widget_get_display(GTK_WIDGET(window)));
  return display != NULL && display->manager != NULL;
}

gboolean
luma_window_get_hidden_from_capture(GtkWindow *window) {
  WindowCapture *self;

  g_return_val_if_fail(GTK_IS_WINDOW(window), FALSE);
  self = window_capture_for(window, FALSE);
  return self != NULL && self->hidden;
}

gboolean
luma_window_set_hidden_from_capture(GtkWindow *window, gboolean hidden) {
  WindowCapture *self;

  g_return_val_if_fail(GTK_IS_WINDOW(window), FALSE);

  hidden = !!hidden;
  self = window_capture_for(window, hidden);
  if (self == NULL)
    return FALSE;
  self->hidden = hidden;
  window_capture_sync(self);
  return hidden && luma_window_get_capture_hiding_supported(window);
}
