/* SPDX-License-Identifier: Apache-2.0 */
#include "luma-ui.h"

/* Without Luma's shell on the bus nothing is restored, keys are kept and a
 * window's state collects its documents. The shell side is tested under
 * headless GNOME Shell by tests/gnome-shell/session-restore. */
static void
test_documents (void)
{
  g_autoptr (GVariantDict) dict = g_variant_dict_new (NULL);
  g_autoptr (GVariant) state = NULL;
  g_autoptr (GVariant) documents = NULL;
  g_autoptr (GVariant) first = NULL;
  const char *uri = NULL;
  gboolean dirty = FALSE;

  luma_restore_add_document (dict, "file:///home/a/report.txt", "report.txt", FALSE, NULL);
  luma_restore_add_document (dict, NULL, "Untitled", TRUE, "draft text");
  state = g_variant_ref_sink (g_variant_dict_end (dict));
  documents = g_variant_lookup_value (state, "documents", G_VARIANT_TYPE ("aa{sv}"));
  g_assert_nonnull (documents);
  g_assert_cmpuint (g_variant_n_children (documents), ==, 2);
  first = g_variant_get_child_value (documents, 0);
  g_assert_true (g_variant_lookup (first, "uri", "&s", &uri));
  g_assert_cmpstr (uri, ==, "file:///home/a/report.txt");
  g_assert_true (g_variant_lookup (first, "dirty", "b", &dirty));
  g_assert_false (dirty);
}

static void
on_activate (GApplication *app, gpointer data)
{
  LumaRestore *restore = luma_restore_get_default (GTK_APPLICATION (app));
  GtkWidget *window = gtk_application_window_new (GTK_APPLICATION (app));
  gboolean *ok = data;

  g_assert_false (luma_restore_restore (restore));
  g_assert_cmpstr (luma_restore_get_reason (restore), ==, "none");
  g_assert_null (luma_window_get_restore_key (GTK_WINDOW (window)));
  luma_window_set_restore_key (GTK_WINDOW (window), "main");
  g_assert_cmpstr (luma_window_get_restore_key (GTK_WINDOW (window)), ==, "main");
  luma_window_set_restore_key (GTK_WINDOW (window), NULL);
  g_assert_null (luma_window_get_restore_key (GTK_WINDOW (window)));
  g_assert_true (luma_restore_get_default (GTK_APPLICATION (app)) == restore);
  *ok = TRUE;
  gtk_window_destroy (GTK_WINDOW (window));
}

static void
test_without_shell (void)
{
  g_autoptr (GtkApplication) app =
    gtk_application_new ("org.projectluma.RestoreTest", G_APPLICATION_NON_UNIQUE);
  gboolean ok = FALSE;

  luma_restore_get_default (app);
  g_signal_connect (app, "activate", G_CALLBACK (on_activate), &ok);
  g_application_run (G_APPLICATION (app), 0, NULL);
  g_assert_true (ok);
}

int
main (int argc, char **argv)
{
  gtk_test_init (&argc, &argv, NULL);
  g_test_add_func ("/restore/documents", test_documents);
  g_test_add_func ("/restore/without-shell", test_without_shell);
  return g_test_run ();
}
