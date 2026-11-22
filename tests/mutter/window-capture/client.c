/* SPDX-License-Identifier: Apache-2.0 */
/* Test client for luma_window_capture_v1: an undecorated GTK window painted
 * one solid colour, so a capture can count its pixels exactly. Built against
 * the kit's own luma-window-capture.c (see run.sh), so it exercises
 * luma_window_set_hidden_from_capture() end to end.
 *
 *   client --title TITLE --rgb RRGGBB [--hidden] [--size WxH] [--animate]
 *
 * --animate repaints every frame in shades of grey that match no test
 * colour, so the stage (and a monitor stream) updates at the refresh rate.
 * SIGUSR1 toggles hiding; SIGUSR2 opens a dialog transient for the window
 * (painted the complement colour) or closes it. Each change is logged on
 * stdout as one JSON line. */
#include <glib-unix.h>
#include <gtk/gtk.h>
#include <stdio.h>

#include "luma-window-capture.h"

static GtkWindow *window;
static GtkWindow *dialog;
static const char *title = "client";
static guint rgb = 0xff00ff;
static gboolean hidden;
static int width = 640, height = 400;
static gboolean animate;

static void
paint (GtkDrawingArea *area, cairo_t *cr, int w, int h, gpointer data)
{
  guint colour = GPOINTER_TO_UINT (data);

  if (animate)
    {
      double grey = 0.25 + 0.5 * (g_get_monotonic_time () / 1000 % 1000) / 1000.0;

      cairo_set_source_rgb (cr, grey, grey, grey);
      cairo_paint (cr);
      return;
    }

  cairo_set_source_rgb (cr, ((colour >> 16) & 0xff) / 255.0,
                        ((colour >> 8) & 0xff) / 255.0, (colour & 0xff) / 255.0);
  cairo_paint (cr);
}

static GtkWindow *
solid_window (const char *name, guint colour, int w, int h)
{
  GtkWindow *win = GTK_WINDOW (gtk_window_new ());
  GtkWidget *area = gtk_drawing_area_new ();

  gtk_window_set_title (win, name);
  gtk_window_set_decorated (win, FALSE);
  gtk_window_set_default_size (win, w, h);
  gtk_drawing_area_set_draw_func (GTK_DRAWING_AREA (area), paint,
                                  GUINT_TO_POINTER (colour), NULL);
  gtk_window_set_child (win, area);
  return win;
}

static void
report (const char *event)
{
  printf ("{\"event\": \"%s\", \"hidden\": %s, \"supported\": %s, \"dialog\": %s}\n",
          event, hidden ? "true" : "false",
          luma_window_get_capture_hiding_supported (window) ? "true" : "false",
          dialog ? "true" : "false");
}

static gboolean
tick (GtkWidget *widget, GdkFrameClock *clock, gpointer data)
{
  gtk_widget_queue_draw (widget);
  return G_SOURCE_CONTINUE;
}

static gboolean
toggle_hidden (gpointer data)
{
  hidden = !hidden;
  luma_window_set_hidden_from_capture (window, hidden);
  report ("toggled");
  return G_SOURCE_CONTINUE;
}

static gboolean
toggle_dialog (gpointer data)
{
  if (dialog)
    {
      gtk_window_destroy (dialog);
      dialog = NULL;
    }
  else
    {
      g_autofree char *name = g_strdup_printf ("%s-dialog", title);

      dialog = solid_window (name, 0xffffff ^ rgb, 320, 200);
      gtk_window_set_transient_for (dialog, window);
      gtk_window_present (dialog);
    }
  report ("dialog");
  return G_SOURCE_CONTINUE;
}

int
main (int argc, char **argv)
{
  for (int i = 1; i < argc; i++)
    {
      if (g_str_equal (argv[i], "--title") && i + 1 < argc)
        title = argv[++i];
      else if (g_str_equal (argv[i], "--rgb") && i + 1 < argc)
        rgb = (guint) g_ascii_strtoull (argv[++i], NULL, 16);
      else if (g_str_equal (argv[i], "--hidden"))
        hidden = TRUE;
      else if (g_str_equal (argv[i], "--size") && i + 1 < argc)
        sscanf (argv[++i], "%dx%d", &width, &height);
      else if (g_str_equal (argv[i], "--animate"))
        animate = TRUE;
    }
  setvbuf (stdout, NULL, _IOLBF, 0);
  gtk_init ();

  window = solid_window (title, rgb, width, height);
  /* Before the window exists on screen, as an app would at start-up. */
  luma_window_set_hidden_from_capture (window, hidden);
  if (animate)
    gtk_widget_add_tick_callback (gtk_window_get_child (window), tick, NULL, NULL);
  gtk_window_present (window);
  g_unix_signal_add (SIGUSR1, toggle_hidden, NULL);
  g_unix_signal_add (SIGUSR2, toggle_dialog, NULL);
  report ("started");

  while (g_list_model_get_n_items (gtk_window_get_toplevels ()) > 0)
    g_main_context_iteration (NULL, TRUE);
  return 0;
}
