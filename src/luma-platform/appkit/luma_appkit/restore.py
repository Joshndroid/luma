# SPDX-License-Identifier: Apache-2.0
"""Windows and app state that survive log-out, restarts and crashes, and
continue on the person's other devices (ADR-055; C `luma-restore.h`).

Every app gets its windows back in place without doing anything. An app
that also wants its documents and views back gives each window a key and
its state::

    class Notes(Adw.Application):
        def do_startup(self):
            Adw.Application.do_startup(self)
            restore.enable(self, self.open_window)

        def do_activate(self):
            if restore.restore(self):
                return
            self.open_window("main", {})

        def open_window(self, key, state):
            window = NotesWindow(application=self)
            window.restore_key = key
            window.restore_state(state)
            window.present()
            return window

`AppWindow.save_state()` returns a dict of JSON values for the window;
`AppWindow.documents()` lists its open documents. Without Luma's shell, or
on a platform older than API level 15, nothing is saved and `restore()`
returns False.
"""
import json

import gi
gi.require_version("LumaUI", "1")
from gi.repository import GLib, LumaUI  # noqa: E402

_PY_KEY = "py"


def _unpack(state) -> dict:
    if state is None:
        return {}
    try:
        text = state.lookup_value(_PY_KEY, GLib.VariantType.new("s"))
        return json.loads(text.get_string()) if text is not None else {}
    except (ValueError, TypeError):
        return {}


def _save_window(_restore, window, dict_):
    save = getattr(window, "save_state", None)
    state = save() if callable(save) else {}
    dict_.insert_value(_PY_KEY, GLib.Variant("s", json.dumps(state or {})))
    documents = getattr(window, "documents", None)
    for document in (documents() if callable(documents) else []) or []:
        LumaUI.Restore.add_document(dict_, document.get("uri"), document.get("title"),
                                    bool(document.get("dirty")), document.get("unsaved"))


def available() -> bool:
    """Whether the loaded LumaUI has LumaRestore (API level 15)."""
    return hasattr(LumaUI, "Restore")


def enable(application, open_window, save=None, restore_app=None):
    """Take part in session snapshots. `open_window(key, state)` re-creates a
    window from its key and the dict its `save_state()` returned; `save()`
    may return a dict of app state, which `restore_app(reason, state)` gets
    back before the windows."""
    if not available():
        return None
    handle = LumaUI.Restore.get_default(application)
    if getattr(handle, "_luma_enabled", False):
        return handle
    handle._luma_enabled = True
    handle.connect("save-window", _save_window)
    handle.connect("restore-window", lambda _r, _reason, key, state: open_window(key, _unpack(state)))
    if save is not None:
        handle.connect("save", lambda _r, dict_: dict_.insert_value(
            _PY_KEY, GLib.Variant("s", json.dumps(save() or {}))))
    if restore_app is not None:
        handle.connect("restore", lambda _r, reason, state: restore_app(reason, _unpack(state)))
    return handle


def restore(application) -> bool:
    """In `activate`: re-create the windows the shell saved for this app.
    True when it did, so `activate` opens nothing else."""
    return available() and bool(LumaUI.Restore.get_default(application).restore())


def reason(application) -> str:
    """"restore", "continue", "recover" or "none"."""
    return LumaUI.Restore.get_default(application).get_reason() if available() else "none"
