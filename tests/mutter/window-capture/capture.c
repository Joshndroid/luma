/* SPDX-License-Identifier: Apache-2.0
 *
 * Screen cast consumer for run.sh: records a monitor, an area or a window
 * through org.gnome.Mutter.ScreenCast and reads it with GStreamer's
 * pipewiresrc, as a capture app does, or reads a PNG. Counts the pixels of
 * each given colour (within 6 per channel) and prints one JSON line per
 * frame; when STOPFILE appears it prints a final line and writes
 * PREFIX-last.png.
 *
 *   capture monitor CONNECTOR PREFIX STOPFILE RRGGBB...
 *   capture area X,Y,W,H PREFIX STOPFILE RRGGBB...
 *   capture window WINDOW_ID PREFIX STOPFILE RRGGBB...
 *   capture png FILE RRGGBB...
 */
#include <cairo.h>
#include <gio/gio.h>
#include <gst/app/gstappsink.h>
#include <gst/gst.h>
#include <gst/video/video.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define MAX_COLOURS 8
#define TOLERANCE 6

static guint node_id;
static guint colours[MAX_COLOURS];
static int n_colours;

static void
on_stream_added (GDBusConnection *connection, const char *sender,
                 const char *path, const char *interface, const char *signal,
                 GVariant *parameters, gpointer user_data)
{
  g_variant_get (parameters, "(u)", &node_id);
}

static GVariant *
call (GDBusConnection *connection, const char *path, const char *interface,
      const char *method, GVariant *args)
{
  g_autoptr (GError) error = NULL;
  GVariant *ret;

  ret = g_dbus_connection_call_sync (connection, "org.gnome.Mutter.ScreenCast",
                                     path, interface, method, args, NULL,
                                     G_DBUS_CALL_FLAGS_NONE, 10000, NULL, &error);
  if (!ret)
    {
      printf ("{\"error\": \"%s: %s\"}\n", method, error->message);
      exit (2);
    }
  return ret;
}

static gboolean
near (int a, int b)
{
  return ABS (a - b) <= TOLERANCE;
}

/* BGRA/BGRx in memory (cairo ARGB32/RGB24 on little-endian). */
static void
count (const guint8 *data, int stride, int width, int height, int *counts)
{
  memset (counts, 0, sizeof (int) * MAX_COLOURS);
  for (int y = 0; y < height; y++)
    {
      const guint8 *row = data + (gsize) y * stride;

      for (int x = 0; x < width; x++)
        {
          const guint8 *p = row + x * 4;

          for (int c = 0; c < n_colours; c++)
            if (near (p[2], (colours[c] >> 16) & 0xff) &&
                near (p[1], (colours[c] >> 8) & 0xff) &&
                near (p[0], colours[c] & 0xff))
              counts[c]++;
        }
    }
}

static void
print_counts (const char *head, const int *counts)
{
  printf ("{%s, \"counts\": [", head);
  for (int c = 0; c < n_colours; c++)
    printf ("%s%d", c ? ", " : "", counts[c]);
  printf ("]}\n");
}

static void
write_png (const guint8 *data, int stride, int width, int height, const char *path)
{
  cairo_surface_t *surface;
  guint8 *dst;
  int dst_stride;

  surface = cairo_image_surface_create (CAIRO_FORMAT_RGB24, width, height);
  cairo_surface_flush (surface);
  dst = cairo_image_surface_get_data (surface);
  dst_stride = cairo_image_surface_get_stride (surface);
  for (int row = 0; row < height; row++)
    memcpy (dst + row * dst_stride, data + (gsize) row * stride, width * 4);
  cairo_surface_mark_dirty (surface);
  cairo_surface_write_to_png (surface, path);
  cairo_surface_destroy (surface);
}

static void
parse_colours (char **argv, int argc)
{
  for (int i = 0; i < argc && n_colours < MAX_COLOURS; i++)
    colours[n_colours++] = (guint) g_ascii_strtoull (argv[i], NULL, 16);
}

static int
count_png (const char *path)
{
  cairo_surface_t *surface = cairo_image_surface_create_from_png (path);
  int counts[MAX_COLOURS];
  g_autofree char *head = NULL;

  if (cairo_surface_status (surface) != CAIRO_STATUS_SUCCESS)
    {
      printf ("{\"error\": \"cannot read %s\"}\n", path);
      return 2;
    }
  count (cairo_image_surface_get_data (surface),
         cairo_image_surface_get_stride (surface),
         cairo_image_surface_get_width (surface),
         cairo_image_surface_get_height (surface), counts);
  head = g_strdup_printf ("\"final\": true, \"width\": %d, \"height\": %d",
                          cairo_image_surface_get_width (surface),
                          cairo_image_surface_get_height (surface));
  print_counts (head, counts);
  cairo_surface_destroy (surface);
  return 0;
}

int
main (int argc, char **argv)
{
  g_autoptr (GDBusConnection) connection = NULL;
  g_autoptr (GVariant) session_ret = NULL, stream_ret = NULL;
  GVariantBuilder props;
  const char *session_path, *stream_path, *mode, *what, *prefix, *stopfile;
  g_autofree char *description = NULL;
  GstElement *pipeline, *sink;
  GstSample *last = NULL;
  int frames = 0;
  gint64 deadline, started;

  setvbuf (stdout, NULL, _IOLBF, 0);
  if (argc >= 4 && g_str_equal (argv[1], "png"))
    {
      parse_colours (argv + 3, argc - 3);
      return count_png (argv[2]);
    }
  if (argc < 6)
    {
      fprintf (stderr, "usage: capture monitor|area|window WHAT PREFIX STOPFILE RRGGBB...\n");
      return 2;
    }
  mode = argv[1];
  what = argv[2];
  prefix = argv[3];
  stopfile = argv[4];
  parse_colours (argv + 5, argc - 5);
  gst_init (&argc, &argv);

  connection = g_bus_get_sync (G_BUS_TYPE_SESSION, NULL, NULL);
  g_dbus_connection_signal_subscribe (connection, NULL,
                                      "org.gnome.Mutter.ScreenCast.Stream",
                                      "PipeWireStreamAdded", NULL, NULL,
                                      G_DBUS_SIGNAL_FLAGS_NONE,
                                      on_stream_added, NULL, NULL);

  session_ret = call (connection, "/org/gnome/Mutter/ScreenCast",
                      "org.gnome.Mutter.ScreenCast", "CreateSession",
                      g_variant_new_parsed ("(@a{sv} {},)"));
  g_variant_get (session_ret, "(&o)", &session_path);

  g_variant_builder_init (&props, G_VARIANT_TYPE ("a{sv}"));
  g_variant_builder_add (&props, "{sv}", "cursor-mode", g_variant_new_uint32 (0));
  if (g_str_equal (mode, "window"))
    {
      g_variant_builder_add (&props, "{sv}", "window-id",
                             g_variant_new_uint64 (g_ascii_strtoull (what, NULL, 10)));
      stream_ret = call (connection, session_path, "org.gnome.Mutter.ScreenCast.Session",
                         "RecordWindow", g_variant_new ("(a{sv})", &props));
    }
  else if (g_str_equal (mode, "area"))
    {
      int x, y, w, h;

      if (sscanf (what, "%d,%d,%d,%d", &x, &y, &w, &h) != 4)
        return 2;
      stream_ret = call (connection, session_path, "org.gnome.Mutter.ScreenCast.Session",
                         "RecordArea", g_variant_new ("(iiiia{sv})", x, y, w, h, &props));
    }
  else
    {
      stream_ret = call (connection, session_path, "org.gnome.Mutter.ScreenCast.Session",
                         "RecordMonitor", g_variant_new ("(sa{sv})", what, &props));
    }
  g_variant_get (stream_ret, "(&o)", &stream_path);
  g_variant_unref (call (connection, session_path, "org.gnome.Mutter.ScreenCast.Session",
                         "Start", NULL));

  deadline = g_get_monotonic_time () + 10 * G_USEC_PER_SEC;
  while (!node_id && g_get_monotonic_time () < deadline)
    g_main_context_iteration (NULL, TRUE);
  if (!node_id)
    {
      printf ("{\"error\": \"no PipeWireStreamAdded\"}\n");
      return 2;
    }

  description = g_strdup_printf ("pipewiresrc path=%u always-copy=true ! "
                                 "appsink name=sink caps=video/x-raw sync=false "
                                 "max-buffers=4 drop=true", node_id);
  pipeline = gst_parse_launch (description, NULL);
  sink = gst_bin_get_by_name (GST_BIN (pipeline), "sink");
  gst_element_set_state (pipeline, GST_STATE_PLAYING);
  started = g_get_monotonic_time ();

  while (!g_file_test (stopfile, G_FILE_TEST_EXISTS))
    {
      GstSample *sample;
      GstBuffer *buffer;
      GstVideoInfo info;
      GstMapInfo map;
      GstVideoCropMeta *crop;
      int counts[MAX_COLOURS];
      int cx, cy, cw, ch;
      g_autofree char *head = NULL;

      sample = gst_app_sink_try_pull_sample (GST_APP_SINK (sink), 100 * GST_MSECOND);
      while (g_main_context_iteration (NULL, FALSE));
      if (!sample)
        continue;
      buffer = gst_sample_get_buffer (sample);
      gst_video_info_from_caps (&info, gst_sample_get_caps (sample));
      crop = gst_buffer_get_video_crop_meta (buffer);
      cx = crop ? (int) crop->x : 0;
      cy = crop ? (int) crop->y : 0;
      cw = crop ? (int) crop->width : GST_VIDEO_INFO_WIDTH (&info);
      ch = crop ? (int) crop->height : GST_VIDEO_INFO_HEIGHT (&info);
      gst_buffer_map (buffer, &map, GST_MAP_READ);
      count (map.data + (gsize) cy * GST_VIDEO_INFO_PLANE_STRIDE (&info, 0) + cx * 4,
             GST_VIDEO_INFO_PLANE_STRIDE (&info, 0), cw, ch, counts);
      gst_buffer_unmap (buffer, &map);
      head = g_strdup_printf ("\"frame\": %d, \"t_ms\": %d, \"format\": \"%s\", "
                              "\"width\": %d, \"height\": %d",
                              frames++,
                              (int) ((g_get_monotonic_time () - started) / 1000),
                              gst_video_format_to_string (GST_VIDEO_INFO_FORMAT (&info)),
                              cw, ch);
      print_counts (head, counts);
      if (last)
        gst_sample_unref (last);
      last = sample;
    }

  if (last)
    {
      GstBuffer *buffer = gst_sample_get_buffer (last);
      GstVideoCropMeta *crop = gst_buffer_get_video_crop_meta (buffer);
      GstVideoInfo info;
      GstMapInfo map;
      g_autofree char *png = g_strdup_printf ("%s-last.png", prefix);
      int stride, cx, cy, cw, ch;

      gst_video_info_from_caps (&info, gst_sample_get_caps (last));
      stride = GST_VIDEO_INFO_PLANE_STRIDE (&info, 0);
      cx = crop ? (int) crop->x : 0;
      cy = crop ? (int) crop->y : 0;
      cw = crop ? (int) crop->width : GST_VIDEO_INFO_WIDTH (&info);
      ch = crop ? (int) crop->height : GST_VIDEO_INFO_HEIGHT (&info);
      gst_buffer_map (buffer, &map, GST_MAP_READ);
      write_png (map.data + (gsize) cy * stride + cx * 4, stride, cw, ch, png);
      gst_buffer_unmap (buffer, &map);
      gst_sample_unref (last);
    }
  printf ("{\"final\": true, \"frames\": %d}\n", frames);

  gst_element_set_state (pipeline, GST_STATE_NULL);
  g_dbus_connection_call_sync (connection, "org.gnome.Mutter.ScreenCast",
                               session_path, "org.gnome.Mutter.ScreenCast.Session",
                               "Stop", NULL, NULL, G_DBUS_CALL_FLAGS_NONE, 5000,
                               NULL, NULL);
  return 0;
}
