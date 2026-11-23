/* SPDX-License-Identifier: Apache-2.0 */
#include "luma-restore.h"

/* The client half of org.projectluma.Session1, served by Luma's shell
 * (patches/gnome-shell 0392, ADR-055). TakeRestore registers the app and
 * hands over the state saved for it, once; SaveRequested asks for the
 * app's state, which goes back with SaveState; SetWindowKey names a window
 * before it first maps, so the shell's first-configure placement already
 * knows it. */

#define SESSION_NAME "org.projectluma.Session"
#define SESSION_PATH "/org/projectluma/Session"
#define SESSION_INTERFACE "org.projectluma.Session1"
#define TAKE_TIMEOUT_MS 1000
#define KEY_TIMEOUT_MS 500

struct _LumaRestore {
  GObject parent_instance;
  GtkApplication *application; /* not owned: the restore is its data */
  GDBusConnection *bus;
  guint save_subscription;
  gboolean taken;
  gboolean taking;
  gboolean restored;
  char *reason;
  GVariant *state;
  GList *windows; /* keyed windows in the order they got their key */
};

enum { SAVE, SAVE_WINDOW, RESTORE, RESTORE_WINDOW, N_SIGNALS };
static guint signals[N_SIGNALS];

G_DEFINE_FINAL_TYPE(LumaRestore, luma_restore, G_TYPE_OBJECT)

static GQuark key_quark;
static GQuark realize_quark;

static void
luma_restore_finalize(GObject *object) {
  LumaRestore *self = LUMA_RESTORE(object);

  if (self->bus && self->save_subscription)
    g_dbus_connection_signal_unsubscribe(self->bus, self->save_subscription);
  g_clear_object(&self->bus);
  g_clear_pointer(&self->state, g_variant_unref);
  g_free(self->reason);
  g_list_free(self->windows);
  G_OBJECT_CLASS(luma_restore_parent_class)->finalize(object);
}

static void
luma_restore_class_init(LumaRestoreClass *klass) {
  G_OBJECT_CLASS(klass)->finalize = luma_restore_finalize;

  key_quark = g_quark_from_static_string("luma-restore-key");
  realize_quark = g_quark_from_static_string("luma-restore-realize");

  /**
   * LumaRestore::save:
   * @restore: the #LumaRestore
   * @state: the app's state, to fill
   *
   * The shell is taking a snapshot. Put in @state what the app needs to be
   * the same again, beyond its windows.
   */
  signals[SAVE] = g_signal_new("save", G_TYPE_FROM_CLASS(klass),
                               G_SIGNAL_RUN_LAST, 0, NULL, NULL, NULL,
                               G_TYPE_NONE, 1, G_TYPE_VARIANT_DICT);

  /**
   * LumaRestore::save-window:
   * @restore: the #LumaRestore
   * @window: a window with a restore key
   * @state: the window's state, to fill
   *
   * Put in @state what @window needs to be the same again: its documents
   * (luma_restore_add_document()), what it shows, its scroll position.
   */
  signals[SAVE_WINDOW] = g_signal_new(
      "save-window", G_TYPE_FROM_CLASS(klass), G_SIGNAL_RUN_LAST, 0, NULL,
      NULL, NULL, G_TYPE_NONE, 2, GTK_TYPE_WINDOW, G_TYPE_VARIANT_DICT);

  /**
   * LumaRestore::restore:
   * @restore: the #LumaRestore
   * @reason: "restore", "continue" or "recover"
   * @state: the app's state as #LumaRestore::save left it
   *
   * Emitted by luma_restore_restore() before the windows are re-created.
   */
  signals[RESTORE] = g_signal_new("restore", G_TYPE_FROM_CLASS(klass),
                                  G_SIGNAL_RUN_LAST, 0, NULL, NULL, NULL,
                                  G_TYPE_NONE, 2, G_TYPE_STRING,
                                  G_TYPE_VARIANT);

  /**
   * LumaRestore::restore-window:
   * @restore: the #LumaRestore
   * @reason: "restore", "continue" or "recover"
   * @key: the window's restore key
   * @state: the window's state as #LumaRestore::save-window left it
   *
   * Re-create the window and give it @key with
   * luma_window_set_restore_key() before showing it.
   */
  signals[RESTORE_WINDOW] = g_signal_new(
      "restore-window", G_TYPE_FROM_CLASS(klass), G_SIGNAL_RUN_LAST, 0, NULL,
      NULL, NULL, G_TYPE_NONE, 3, G_TYPE_STRING, G_TYPE_STRING,
      G_TYPE_VARIANT);
}

static void
luma_restore_init(LumaRestore *self) {
  self->reason = g_strdup("none");
}

static GDBusConnection *
restore_bus(LumaRestore *self) {
  if (self->bus == NULL) {
    GDBusConnection *bus = g_application_get_dbus_connection(
        G_APPLICATION(self->application));
    if (bus)
      self->bus = g_object_ref(bus);
  }
  return self->bus;
}

static void
windows_state(LumaRestore *self, GVariantBuilder *windows) {
  g_variant_builder_init(windows, G_VARIANT_TYPE("a(sa{sv})"));
  for (GList *l = self->windows; l; l = l->next) {
    GtkWindow *window = l->data;
    const char *key = luma_window_get_restore_key(window);
    g_autoptr(GVariantDict) dict = g_variant_dict_new(NULL);

    if (key == NULL)
      continue;
    g_signal_emit(self, signals[SAVE_WINDOW], 0, window, dict);
    g_variant_builder_add(windows, "(s@a{sv})", key, g_variant_dict_end(dict));
  }
}

static void
on_save_requested(GDBusConnection *bus, const char *sender G_GNUC_UNUSED,
                  const char *path G_GNUC_UNUSED,
                  const char *interface G_GNUC_UNUSED,
                  const char *signal G_GNUC_UNUSED, GVariant *parameters,
                  gpointer data) {
  LumaRestore *self = data;
  g_autoptr(GVariantDict) app = g_variant_dict_new(NULL);
  GVariantBuilder windows;
  GVariant *state;
  guint32 serial;

  g_variant_get(parameters, "(u)", &serial);
  g_signal_emit(self, signals[SAVE], 0, app);
  windows_state(self, &windows);
  state = g_variant_new("{sv}", "app", g_variant_dict_end(app));
  GVariantBuilder all;
  g_variant_builder_init(&all, G_VARIANT_TYPE_VARDICT);
  g_variant_builder_add_value(&all, state);
  g_variant_builder_add(&all, "{sv}", "windows", g_variant_builder_end(&windows));
  g_dbus_connection_call(bus, SESSION_NAME, SESSION_PATH, SESSION_INTERFACE,
                         "SaveState",
                         g_variant_new("(uv)", serial,
                                       g_variant_builder_end(&all)),
                         NULL, G_DBUS_CALL_FLAGS_NO_AUTO_START, 1000, NULL,
                         NULL, NULL);
}

static void
take_done(GObject *source, GAsyncResult *result, gpointer data) {
  LumaRestore *self = data;
  g_autoptr(GError) error = NULL;
  g_autoptr(GVariant) reply =
      g_dbus_connection_call_finish(G_DBUS_CONNECTION(source), result, &error);

  self->taking = FALSE;
  self->taken = TRUE;
  if (reply) {
    g_autofree char *reason = NULL;
    GVariant *state = NULL;

    g_variant_get(reply, "(sv)", &reason, &state);
    g_free(self->reason);
    self->reason = g_steal_pointer(&reason);
    g_clear_pointer(&self->state, g_variant_unref);
    self->state = state;
  } else if (!g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_SERVICE_UNKNOWN) &&
             !g_error_matches(error, G_DBUS_ERROR, G_DBUS_ERROR_NAME_HAS_NO_OWNER)) {
    g_debug("LumaRestore: %s", error->message);
  }
  g_object_unref(self);
}

/* Registers with the shell and fetches the state saved for the app. */
static void
restore_take(LumaRestore *self) {
  GDBusConnection *bus = restore_bus(self);
  const char *app_id =
      g_application_get_application_id(G_APPLICATION(self->application));

  if (self->taken || self->taking || bus == NULL || app_id == NULL)
    return;
  self->taking = TRUE;
  self->save_subscription = g_dbus_connection_signal_subscribe(
      bus, SESSION_NAME, SESSION_INTERFACE, "SaveRequested", SESSION_PATH,
      NULL, G_DBUS_SIGNAL_FLAGS_NONE, on_save_requested, self, NULL);
  g_dbus_connection_call(bus, SESSION_NAME, SESSION_PATH, SESSION_INTERFACE,
                         "TakeRestore", g_variant_new("(s)", app_id),
                         G_VARIANT_TYPE("(sv)"),
                         G_DBUS_CALL_FLAGS_NO_AUTO_START, TAKE_TIMEOUT_MS,
                         NULL, take_done, g_object_ref(self));
}

static void
on_startup(GApplication *application, gpointer data G_GNUC_UNUSED) {
  restore_take(luma_restore_get_default(GTK_APPLICATION(application)));
}

LumaRestore *
luma_restore_get_default(GtkApplication *application) {
  LumaRestore *self;

  g_return_val_if_fail(GTK_IS_APPLICATION(application), NULL);

  self = g_object_get_data(G_OBJECT(application), "luma-restore");
  if (self)
    return self;
  self = g_object_new(LUMA_TYPE_RESTORE, NULL);
  self->application = application;
  g_object_set_data_full(G_OBJECT(application), "luma-restore", self,
                         g_object_unref);
  if (g_application_get_is_registered(G_APPLICATION(application)))
    restore_take(self);
  else
    g_signal_connect(application, "startup", G_CALLBACK(on_startup), NULL);
  return self;
}

gboolean
luma_restore_restore(LumaRestore *self) {
  GVariant *app = NULL;
  GVariant *windows = NULL;
  GVariantIter iter;
  const char *key;
  GVariant *window_state;
  guint made = 0;

  g_return_val_if_fail(LUMA_IS_RESTORE(self), FALSE);

  if (self->restored)
    return FALSE;
  self->restored = TRUE;
  restore_take(self);
  /* Wait for the reply here: activate must know before it opens anything. */
  gint64 deadline = g_get_monotonic_time() + TAKE_TIMEOUT_MS * 1000;
  while (self->taking && g_get_monotonic_time() < deadline)
    g_main_context_iteration(NULL, TRUE);

  if (self->state == NULL || g_str_equal(self->reason, "none"))
    return FALSE;
  if (!g_variant_is_of_type(self->state, G_VARIANT_TYPE_VARDICT))
    return FALSE;
  app = g_variant_lookup_value(self->state, "app", G_VARIANT_TYPE_VARDICT);
  windows = g_variant_lookup_value(self->state, "windows",
                                   G_VARIANT_TYPE("a(sa{sv})"));
  if (app == NULL)
    app = g_variant_ref_sink(g_variant_new_array(G_VARIANT_TYPE("{sv}"), NULL, 0));
  g_signal_emit(self, signals[RESTORE], 0, self->reason, app);
  if (windows) {
    g_variant_iter_init(&iter, windows);
    while (g_variant_iter_next(&iter, "(&s@a{sv})", &key, &window_state)) {
      guint before = g_list_length(
          gtk_application_get_windows(self->application));
      g_signal_emit(self, signals[RESTORE_WINDOW], 0, self->reason, key,
                    window_state);
      if (g_list_length(gtk_application_get_windows(self->application)) > before)
        made++;
      g_variant_unref(window_state);
    }
  }
  g_variant_unref(app);
  g_clear_pointer(&windows, g_variant_unref);
  return made > 0;
}

const char *
luma_restore_get_reason(LumaRestore *self) {
  g_return_val_if_fail(LUMA_IS_RESTORE(self), "none");
  return self->reason;
}

void
luma_restore_add_document(GVariantDict *state, const char *uri,
                          const char *title, gboolean dirty,
                          const char *unsaved) {
  GVariantBuilder documents;
  GVariantBuilder document;
  g_autoptr(GVariant) previous = NULL;

  g_return_if_fail(state != NULL);

  g_variant_builder_init(&documents, G_VARIANT_TYPE("aa{sv}"));
  previous = g_variant_dict_lookup_value(state, "documents",
                                         G_VARIANT_TYPE("aa{sv}"));
  if (previous) {
    GVariantIter iter;
    GVariant *child;
    g_variant_iter_init(&iter, previous);
    while ((child = g_variant_iter_next_value(&iter))) {
      g_variant_builder_add_value(&documents, child);
      g_variant_unref(child);
    }
  }
  g_variant_builder_init(&document, G_VARIANT_TYPE_VARDICT);
  if (uri)
    g_variant_builder_add(&document, "{sv}", "uri", g_variant_new_string(uri));
  if (title)
    g_variant_builder_add(&document, "{sv}", "title",
                          g_variant_new_string(title));
  g_variant_builder_add(&document, "{sv}", "dirty",
                        g_variant_new_boolean(dirty));
  if (unsaved)
    g_variant_builder_add(&document, "{sv}", "unsaved",
                          g_variant_new_string(unsaved));
  g_variant_builder_add_value(&documents, g_variant_builder_end(&document));
  g_variant_dict_insert_value(state, "documents",
                              g_variant_builder_end(&documents));
}

static char *
window_path(GtkWindow *window) {
  GtkApplication *application = gtk_window_get_application(window);
  const char *app_path;
  guint id;

  if (application == NULL || !GTK_IS_APPLICATION_WINDOW(window))
    return NULL;
  app_path = g_application_get_dbus_object_path(G_APPLICATION(application));
  id = gtk_application_window_get_id(GTK_APPLICATION_WINDOW(window));
  if (app_path == NULL || id == 0)
    return NULL;
  return g_strdup_printf("%s/window/%u", app_path, id);
}

/* Synchronous, so the shell has the key before the window's first commit. */
static void
send_key(GtkWindow *window) {
  GtkApplication *application = gtk_window_get_application(window);
  g_autofree char *path = window_path(window);
  GDBusConnection *bus;
  const char *key = luma_window_get_restore_key(window);
  g_autoptr(GVariant) reply = NULL;

  if (path == NULL)
    return;
  bus = restore_bus(luma_restore_get_default(application));
  if (bus == NULL)
    return;
  reply = g_dbus_connection_call_sync(
      bus, SESSION_NAME, SESSION_PATH, SESSION_INTERFACE, "SetWindowKey",
      g_variant_new("(os)", path, key ? key : ""), NULL,
      G_DBUS_CALL_FLAGS_NO_AUTO_START, KEY_TIMEOUT_MS, NULL, NULL);
}

static void
on_realize(GtkWidget *widget, gpointer data G_GNUC_UNUSED) {
  send_key(GTK_WINDOW(widget));
}

static void
forget_window(gpointer data, GObject *window) {
  LumaRestore *self = data;
  self->windows = g_list_remove(self->windows, window);
}

void
luma_window_set_restore_key(GtkWindow *window, const char *key) {
  GtkApplication *application;
  LumaRestore *self;

  g_return_if_fail(GTK_IS_WINDOW(window));

  if (key_quark == 0)
    g_type_class_unref(g_type_class_ref(LUMA_TYPE_RESTORE));
  g_object_set_qdata_full(G_OBJECT(window), key_quark,
                          key && *key ? g_strdup(key) : NULL, g_free);
  application = gtk_window_get_application(window);
  if (application) {
    self = luma_restore_get_default(application);
    if (!g_list_find(self->windows, window)) {
      self->windows = g_list_append(self->windows, window);
      g_object_weak_ref(G_OBJECT(window), forget_window, self);
    }
  }
  if (gtk_widget_get_realized(GTK_WIDGET(window)))
    send_key(window);
  else if (!g_object_get_qdata(G_OBJECT(window), realize_quark)) {
    g_signal_connect(window, "realize", G_CALLBACK(on_realize), NULL);
    g_object_set_qdata(G_OBJECT(window), realize_quark, GINT_TO_POINTER(1));
  }
}

const char *
luma_window_get_restore_key(GtkWindow *window) {
  g_return_val_if_fail(GTK_IS_WINDOW(window), NULL);
  if (key_quark == 0)
    return NULL;
  return g_object_get_qdata(G_OBJECT(window), key_quark);
}
