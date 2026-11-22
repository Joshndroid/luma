/* SPDX-License-Identifier: Apache-2.0 */
#include "luma-ui.h"

/* Without Luma's window-capture protocol (X11 here, or another Wayland
 * compositor) every call is a silent no-op that says the window is not
 * hidden, and the setting is still kept for a later realize. The compositor
 * side is tested under headless GNOME Shell by tests/mutter/window-capture. */
static void
test_no_op_without_protocol (void)
{
  GtkWidget *window = gtk_window_new ();

  g_assert_false (luma_window_get_capture_hiding_supported (GTK_WINDOW (window)));
  g_assert_false (luma_window_get_hidden_from_capture (GTK_WINDOW (window)));
  g_assert_false (luma_window_set_hidden_from_capture (GTK_WINDOW (window), TRUE));
  g_assert_true (luma_window_get_hidden_from_capture (GTK_WINDOW (window)));

  gtk_window_set_default_size (GTK_WINDOW (window), 300, 200);
  gtk_window_present (GTK_WINDOW (window));
  while (g_main_context_iteration (NULL, FALSE))
    ;
  g_assert_true (luma_window_get_hidden_from_capture (GTK_WINDOW (window)));

  g_assert_false (luma_window_set_hidden_from_capture (GTK_WINDOW (window), FALSE));
  g_assert_false (luma_window_get_hidden_from_capture (GTK_WINDOW (window)));

  gtk_window_destroy (GTK_WINDOW (window));
  while (g_main_context_iteration (NULL, FALSE))
    ;
}

/* Showing a window that was never hidden makes no binding; hiding survives
 * unrealize and realize. */
static void
test_survives_realize_cycles (void)
{
  GtkWidget *window = gtk_window_new ();

  g_assert_false (luma_window_set_hidden_from_capture (GTK_WINDOW (window), FALSE));
  g_assert_null (g_object_get_data (G_OBJECT (window), "luma-window-capture"));
  luma_window_set_hidden_from_capture (GTK_WINDOW (window), TRUE);
  gtk_window_present (GTK_WINDOW (window));
  while (g_main_context_iteration (NULL, FALSE))
    ;
  gtk_widget_unrealize (window);
  gtk_widget_realize (window);
  g_assert_true (luma_window_get_hidden_from_capture (GTK_WINDOW (window)));
  gtk_window_destroy (GTK_WINDOW (window));
  while (g_main_context_iteration (NULL, FALSE))
    ;
}

int
main (int argc, char **argv)
{
  gtk_test_init (&argc, &argv, NULL);
  g_test_add_func ("/window-capture/no-op-without-protocol",
                   test_no_op_without_protocol);
  g_test_add_func ("/window-capture/survives-realize-cycles",
                   test_survives_realize_cycles);
  return g_test_run ();
}
