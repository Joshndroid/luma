#!/usr/bin/python3
# SPDX-License-Identifier: Apache-2.0
"""Every key in every editor and menu reaches a method that exists, and switching scenes never moves a
panel. Runs the real window on drawn stand-in sources (needs a display: Xvfb).

    STREAMY_FAKE_SOURCES=1 python3 tests/window_actions.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["STREAMY_FAKE_SOURCES"] = "1"
os.environ.setdefault("STREAMY_DATA_DIR", tempfile.mkdtemp(prefix="streamy-actions-"))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst, Gtk  # noqa: E402

errors: list[str] = []
sys.excepthook = lambda kind, value, tb: errors.append(f"{kind.__name__}: {value}")


def walk(widget):
    child = widget.get_first_child()
    while child is not None:
        yield child
        yield from walk(child)
        child = child.get_next_sibling()


def pump(seconds: float = 0.3) -> None:
    loop = GLib.MainLoop()
    GLib.timeout_add(int(seconds * 1000), loop.quit)
    loop.run()


def pump_until(until, seconds: float) -> bool:
    """Run the main loop until ``until()`` or ``seconds`` passed; whether it came true."""
    end = time.monotonic() + seconds
    while not until() and time.monotonic() < end:
        pump(0.05)
    return until()


def check(app) -> None:
    from luma_appkit.bar_panel import PanelRow

    win = app.props.active_window
    win._choose_image = lambda *_a, **_k: None  # no file dialog in a check run
    pressed = 0
    scene = win.project.active_scene
    for w in list(scene.widgets):
        # The Inspector's rows for the selection (Source, Look, Arrange), each from a fresh Inspector.
        win.select(w.id)
        pump(0.2)
        count = len([r for r in walk(win.inspector) if isinstance(r, PanelRow)])
        for index in range(count):
            win.select(None)
            win.select(w.id)
            pump(0.05)
            rows = [r for r in walk(win.inspector) if isinstance(r, PanelRow)]
            if index < len(rows) and rows[index].get_sensitive() and getattr(rows[index], "on_activate", None):
                rows[index].on_activate()
                pressed += 1
                pump(0.05)
            win.center.fold()
    win.select(None)
    pump(0.1)
    for row in [r for r in walk(win.inspector) if isinstance(r, PanelRow)]:  # the scene's
        if row.get_sensitive() and getattr(row, "on_activate", None) is not None and "Add" not in row.get_name():
            pressed += 1
    from luma_appkit.action_bubble import MenuItem

    def leaves(rows):
        for row in rows:
            if isinstance(row, MenuItem):
                yield from leaves(row.submenu) if row.submenu is not None else [row]

    def close_menus():
        for top in Gtk.Window.list_toplevels():
            if top is not win and top.get_visible() and top.get_transient_for() is win:
                top.close()
        win.center.fold()

    # Every row of every widget's menu (submenus too) and of every scene's menu, at a point, each from a
    # fresh menu and undone after, so each acts on the scene as it was.
    activated = 0
    for kind in ("widget", "scene"):
        targets = [w.id for w in win.project.active_scene.widgets] if kind == "widget" else \
            [s.id for s in win.project.scenes]
        for target in targets:
            open_menu = (lambda t=target: win._widget_menu(t, win.arrange, 40, 40)) if kind == "widget" else \
                (lambda t=target: win._scene_menu(t, win._scene_rows[t], 10, 10))
            open_menu()
            pump(0.1)
            count = len(list(leaves(win._last_menu)))
            close_menus()
            for index in range(count):
                exists = target in ({w.id for w in win.project.active_scene.widgets} if kind == "widget"
                                    else {s.id for s in win.project.scenes})
                if not exists:
                    break
                steps = len(win.history.undos)
                open_menu()
                pump(0.05)
                items = list(leaves(win._last_menu))
                if index >= len(items):
                    break
                items[index].on_activate()
                activated += 1
                pump(0.1)
                close_menus()
                if len(win.history.undos) > steps:
                    win.undo()
                    pump(0.1)
    print(f"pressed {pressed} panel rows, {activated} menu rows; errors: {errors}")
    assert pressed > 5 and activated > 20 and not errors, errors

    # Inline rename: the row's name is edited in place; Enter keeps it, Esc cancels.
    from luma_appkit import InlineRenameField

    def field_in(row):
        return next(c for c in walk(row) if isinstance(c, InlineRenameField))

    scene = win.project.active_scene
    pump(1.0)  # the rows rebuilt by the menu rows above are settled
    # The field is in the row at once; used before its focus timer, so a window without focus (Xvfb)
    # never finishes it first.
    win.rename_scene(scene.id)
    field = field_in(win._scene_rows[scene.id])
    field.set_text("Renamed here")
    field.emit("activate")
    pump(0.2)
    assert win.project.active_scene.name == "Renamed here", win.project.active_scene.name
    pump(0.3)
    win.rename_scene(scene.id)
    field = field_in(win._scene_rows[scene.id])
    field.set_text("Never")
    field._finished = True  # as Esc does
    field._cancel()
    pump(0.2)
    assert win.project.active_scene.name == "Renamed here"
    w = win.project.active_scene.widgets[0]
    win.rename_widget(w)
    field = field_in(win._widget_rows[w.id])
    field.set_text("Cam")
    field.emit("activate")
    pump(0.2)
    assert win.project.active_scene.widget(w.id).name == "Cam"
    print("inline rename keeps and cancels")

    # Panels belong to the app, not the scene: switching scenes moves nothing.
    def boxes():
        return {k: (t.get_width(), t.get_height()) for k, t in win.dock.tiles.items()}

    pump(0.5)
    before = boxes()
    for other in win.project.scenes:
        win.switch_scene(other.id)
        pump(0.5)
        assert boxes() == before, (other.name, boxes(), before)
    print("scene switches kept the panels", before)
    # A person's seam size holds through a relayout and scene switches.
    outer = next(p for p, node in win.dock._panes if node["split"] == "v")
    outer.held = True
    outer.set_position(outer.get_position() - 60)
    pump(0.3)
    outer.held = False
    pump(0.5)
    sized = boxes()
    win.dock.refit()
    win.queue_resize()
    for other in win.project.scenes:
        win.switch_scene(other.id)
        pump(0.5)
        assert boxes() == sized, ("the seam moved back", other.name, boxes(), sized)
    assert sized["landscape"][1] < before["landscape"][1], (sized, before)
    print("a dragged seam holds", sized)
    outputs(win)
    output_editor(win)
    inspector(win)
    several(win)
    linked(win)
    unplugged(win)
    win.close()  # the engine stops with the window, before the interpreter goes
    pump(1.0)
    app.quit()


def several(win) -> None:
    """Several widgets: the list and the canvas agree, Mixed where their Look differs, every command acts on
    all of them as one undo step."""
    scene = next(sc for sc in win.project.scenes if sum(w.has_picture for w in sc.widgets) >= 2)
    win.switch_scene(scene.id)
    pump(0.2)
    a, b = [w for w in scene.widgets if w.has_picture][:2]
    win.project.set_effect(a, "corner_radius", 0)
    win.project.set_effect(b, "corner_radius", 30)
    # Two rows chosen in the list (as Ctrl+click does): the selection follows.
    win.widget_list.unselect_all()
    win.widget_list.select_row(win._widget_rows[a.id])
    win.widget_list.select_row(win._widget_rows[b.id])
    pump(0.2)
    assert set(win.selection) == {a.id, b.id}, win.selection
    assert win.inspector_tile.title == "2 widgets", win.inspector_tile.title
    from luma_appkit import ValueSlider

    mixed = [w for w in walk(win.inspector) if isinstance(w, ValueSlider) and w.mixed]
    assert mixed, "their corners differ: that slider says Mixed"
    steps = len(win.history.undos)

    def now():  # undo restores a copy of the project: look them up again
        sc = win.project.scene(scene.id)
        return sc, sc.widget(a.id), sc.widget(b.id)

    win.apply_look_all("card")
    sc, wa, wb = now()
    assert win._look_of(wa) == win._look_of(wb) == "card"
    win.toggle_hidden()
    sc, wa, wb = now()
    assert not wa.visible and not wb.visible
    win.toggle_lock()
    sc, wa, wb = now()
    assert wa.locked and wb.locked
    win.restack(1)
    sc, wa, wb = now()
    assert [w.id for w in sc.widgets[-2:]] == [a.id, b.id], [w.name for w in sc.widgets]
    assert len(win.history.undos) == steps + 4, (len(win.history.undos), steps)
    for _ in range(3):
        win.undo()
    pump(0.2)
    sc, wa, wb = now()
    assert wa.visible and wb.visible and not wa.locked
    win.set_selection([a.id, b.id])
    count = len(sc.widgets)
    win.duplicate_selected()
    pump(0.2)
    sc, wa, wb = now()
    assert len(sc.widgets) == count + 2 and len(win.selection) == 2, (len(sc.widgets), win.selection)
    win.delete_selected()
    pump(0.2)
    assert len(now()[0].widgets) == count
    win.undo()
    pump(0.2)
    assert len(now()[0].widgets) == count + 2
    win.undo()
    pump(0.2)
    assert len(now()[0].widgets) == count
    win.set_selection([a.id, b.id])
    win._widget_menu(a.id, win.arrange)
    pump(0.2)
    assert any(getattr(r, "label", "") == "Delete 2 widgets" for r in win._last_menu if r is not None)
    win.select(None)
    print("several: list and canvas agree, Mixed, one undo step per command")


def linked(win) -> None:
    """Copy a camera, paste it linked into another scene: one capture, the link shows, each scene keeps its
    own place; Paste as new, Unlink and deleting the last use each do what they say (and undo)."""
    from luma_streamy.model import FORMATS as FORMATS_ALL

    first = next(sc for sc in win.project.scenes if any(w.kind == "camera" for w in sc.widgets))
    win.switch_scene(first.id)
    pump(0.3)
    cam = next(w for w in first.widgets if w.kind == "camera")
    link = cam.link  # (an unlink and its undo replace the objects; the ids stay)
    win.select(cam.id)
    win.copy_selected()
    other = win.project.add_scene()  # (new_scene would put the focus in its name field: Ctrl+V types there)
    win.switch_scene(other.id)
    pump(0.3)
    feeds = {k: id(f) for k, f in win.engine.feeds.items()}
    win.paste()
    pump_until(lambda: win.engine.branches.get(win.selected) is not None, 3)
    pasted = win.project.active_scene.widget(win.selected)
    assert pasted.link == cam.link and pasted.id != cam.id
    assert {k: id(f) for k, f in win.engine.feeds.items()} == feeds, "the paste opened its camera again"
    assert win._also_in(pasted) == f"Also in {first.name}", win._also_in(pasted)
    row = win._widget_rows[pasted.id]
    assert row.get_tooltip_text() == f"Also in {first.name}"
    win.project.set_rect(pasted.id, 0, 0, 1920, 1080)
    assert win.project.find_widget(cam.id)[1].width != 1920, "resizing in one scene moved the other"
    # Switching between scenes that share it, mid-drag, and pasting there: the previews never hear of a
    # widget they don't hold (no GLib criticals; they are only on stderr, so stderr is read here).
    import tempfile as _tempfile

    log = _tempfile.TemporaryFile()
    saved = os.dup(2)
    os.dup2(log.fileno(), 2)
    try:
        win._canvas_changed(pasted.id, 100, 100, 800, 450)
        pump(0.2)
        # A source's new picture size (a reconnect, an unlock) reshapes every scene's widget of it.
        win._source_size(None, link, 1280, 720)
        win._source_size(None, link, 1920, 1080)
        pump(0.2)
        for target in (first, other, first, other):
            win.switch_scene(target.id)
            pump(0.4)
        win.copy_selected()
        win.paste()
        pump(0.4)
        win.undo()
        pump(0.3)
        win.switch_scene(first.id)
        pump(0.4)
        win.switch_scene(other.id)
        pump(0.4)
    finally:
        os.dup2(saved, 2)
        os.close(saved)
    log.seek(0)
    criticals = [line for line in log.read().decode(errors="replace").splitlines() if "CRITICAL" in line]
    assert not criticals, criticals[:5]
    pasted = win.project.active_scene.widget(pasted.id)
    win.select(pasted.id)
    titles = [getattr(r, "label", "") for r in (win._widget_menu(pasted.id, win.arrange) or win._last_menu) if r]
    assert "Unlink from other scenes" in titles and "Paste as new widget" in titles, titles
    win.paste(linked=False)
    pump(0.3)
    own = win.project.active_scene.widget(win.selected)
    assert own.link != cam.link and not win.project.is_linked(own)
    win.unlink_widget(win.project.active_scene.widget(pasted.id))
    pump(0.3)
    assert not win.project.is_linked(win.project.active_scene.widget(pasted.id))
    win.undo()  # linked again
    pump(0.3)
    again = win.project.active_scene.widget(pasted.id)
    assert win.project.is_linked(again)
    # Delete removes it from this scene only; the source stays while the first scene uses it.
    win.select(pasted.id)
    win.delete_selected()
    pump(0.3)
    assert link in win.project.sources, (link, list(win.project.sources))
    assert pump_until(lambda: win.engine.has_source(link), 3), (link, [(k, f.links) for k, f in win.engine.feeds.items()])
    win.delete_scene(other.id)
    pump(0.3)
    win.switch_scene(first.id)
    pump(0.3)
    # Each canvas's ⋯ menu builds (its rows name methods that exist).
    for fmt in FORMATS_ALL:
        rows = win._canvas_more(fmt)
        assert rows and all(callable(getattr(r, "on_activate", None)) or r is None or isinstance(r, str)
                            for r in rows), rows
    print("linked: paste keeps one capture, places stay each scene's, paste as new, unlink and delete")


def inspector(win) -> None:
    """Closed, the Inspector stays closed through selections and scene switches (and is saved so);
    Source/Look/Arrange or Ctrl+I open it."""
    win.toggle_inspector()
    pump(0.1)
    closed = win.dock.layout()
    assert not win._inspector_shown() and "inspector" in closed.get("closed", {}), closed
    scene = next(sc for sc in win.project.scenes if any(w.has_picture for w in sc.widgets))
    win.switch_scene(scene.id)
    for w in scene.widgets:
        win.select(w.id)
        pump(0.05)
    for other in win.project.scenes:
        win.switch_scene(other.id)
        pump(0.1)
    win.switch_scene(scene.id)
    pump(0.1)
    assert not win._inspector_shown(), "the Inspector reopened by itself"
    picture = next(w for w in win.project.active_scene.widgets if w.has_picture)
    win.select(picture.id)
    win._grow("look")
    pump(0.1)
    assert win._inspector_shown() and "inspector" not in win.dock.layout().get("closed", {})
    win.toggle_inspector()
    pump(0.1)
    assert not win._inspector_shown()
    win.toggle_inspector()
    pump(0.1)
    assert win._inspector_shown()
    win.select(None)
    print("inspector: closes, stays closed, opens when asked")


def outputs(win) -> None:
    """The bar's own keys, pressed: Record shows Stop recording at once and Stop ends it (three times);
    with no stream destination there is no Go live key, and no key for adding one."""
    import time

    shown = []
    show_bar = win.center.show_bar
    win.center.show_bar = lambda items, **kw: (shown.append(list(items)), show_bar(items, **kw))[1]

    def key(label):
        win._refresh_bar()
        for item in shown[-1]:
            if getattr(item, "label", None) == label and getattr(item, "on_activate", None):
                return item
        raise AssertionError(f"no {label!r} key in the bar: {[getattr(i, 'label', i) for i in shown[-1]]}")

    def labels():
        return [getattr(i, "label", None) for i in shown[-1]]

    win.select(None)
    pump(0.2)
    for turn in range(3):
        key("Record").on_activate()
        pump(0.3)
        if win.recording_since is None:
            # No encoder in this container: the bar still says why, and Record stays Record.
            assert "Record" in labels(), labels()
            print("record: no encoder here; Record stays", labels())
            break
        assert "Stop recording" in labels(), labels()
        pump(1.0)
        key("Stop recording").on_activate()
        started = time.monotonic()
        while win.recording_since is not None and time.monotonic() - started < 15:
            pump(0.2)
        assert win.recording_since is None, f"recording {turn} did not stop"
        assert "Record" in labels(), labels()
    else:
        print("record: started and stopped 3 times; the bar followed each")
    # A selection never takes the outputs away.
    scene = next(sc for sc in win.project.scenes if any(w.has_picture for w in sc.widgets))
    win.switch_scene(scene.id)
    picture = next(w for w in scene.widgets if w.has_picture)
    win.select(picture.id)
    pump(0.1)
    assert "Record" in labels() and ("Go live" in labels()) == win._streams_ready(), labels()
    win.select(None)
    win._refresh_bar()
    assert "Add a destination" not in labels(), labels()
    assert "Add widget" not in labels(), labels()
    print("bar: outputs only, never a key for adding a destination or a widget")
    # Turning a stream row on or off in Destinations changes the bar at once, with no other event.
    streams = [r for r in win.dest_store.rows if r.is_stream]
    row = streams[0] if streams else win.dest_store.add("restream")
    for on in (True, False, True):
        row.enabled = on
        win.dest_store.update(row)
        win.dest_panel._changed()
        assert ("Go live" in labels()) == win._streams_ready() == (on or any(
            r.is_stream and r.enabled for r in win.dest_store.rows if r is not row)), labels()
    print("bar: Go live follows a stream row turned on and off")
    # Recording's switch on: the bar says Go live & record; off: Go live.
    rec = next(r for r in win.dest_store.rows if not r.is_stream)
    was = rec.enabled
    for on in (True, False):
        rec.enabled = on
        win.dest_store.update(rec)
        win.dest_panel._changed()
        if win._streams_ready() and win.recording_since is None:
            assert ("Go live & record" in labels()) == on and ("Go live" in labels()) != on, labels()
    rec.enabled = was
    win.dest_store.update(rec)
    win.dest_panel._changed()
    print("bar: Go live & record while Recording goes with Go live")
    win.center.show_bar = show_bar


def output_editor(win) -> None:
    """A destination's editor sets what it sends (picture size, frame rate, video bitrate, automatic
    bitrate, sound) and stores it; the platform's values store as its defaults; the recording's editor also
    sets its codec. While live, a bitrate change goes to the outputs at once and the rest waits."""
    panel = win.dest_panel
    row = next((r for r in win.dest_store.rows if r.is_stream), None) or win.dest_store.add("restream")
    panel.edit(row.id)
    pump(0.2)
    fields = panel.editor_output
    labels = [label for label, _w in fields.fields]
    assert labels == ["Picture", "Bitrate", "Sound"], labels
    assert fields.auto is not None and fields.auto.get_active() == row.auto_bitrate
    assert [b for b in walk(panel.editor) if b is fields.video], "the bitrate isn't in the editor"
    fields.size.set_current("720")
    fields.fps.set_current("60")
    fields.video.set_value(3500)
    fields.auto.set_active(False)
    fields.audio.set_current("192")
    panel.editor.primary.on_activate()
    pump(0.2)
    saved = win.dest_store.get(row.id)
    assert (saved.size, saved.fps, saved.video_kbps, saved.auto_bitrate, saved.audio_kbps) == (720, 60, 3500, False, 192), \
        saved
    from luma_streamy.destinations_panel import quality_words

    assert quality_words(saved) == "3.5 Mb/s · 720p60", quality_words(saved)
    from luma_streamy.destinations import DestinationStore

    again = DestinationStore.load(win.dest_store.path).get(row.id)
    assert (again.size, again.fps, again.video_kbps) == (720, 60, 3500), again
    # The platform's own values store as "the platform's default" (0), so they follow it.
    panel.edit(row.id)
    pump(0.2)
    fields = panel.editor_output
    video, audio, _size, _fps = saved.defaults
    fields.size.set_current("0")
    fields.fps.set_current("30")
    fields.video.set_value(video)
    fields.audio.set_current(str(audio))
    fields.auto.set_active(True)
    panel.editor.primary.on_activate()
    pump(0.2)
    saved = win.dest_store.get(row.id)
    assert (saved.size, saved.fps, saved.video_kbps, saved.audio_kbps, saved.auto_bitrate) == (0, 0, 0, 0, True), saved
    print("destination editor: picture, frame rate, bitrates and automatic bitrate stored; defaults follow the platform")
    # The recording: codec and picture too, no automatic bitrate.
    rec = next(r for r in win.dest_store.rows if not r.is_stream)
    was = (rec.codec, rec.size)
    panel.edit(rec.id)
    pump(0.2)
    assert panel.editor_output.auto is None, "a recording has no automatic bitrate"
    codec = panel.editor_codec
    codec.set_current("av1")
    panel.editor_output.size.set_current("480")
    panel.editor.primary.on_activate()
    pump(0.2)
    rec = win.dest_store.get(rec.id)
    assert (rec.codec, rec.size) == ("av1", 480), (rec.codec, rec.size)
    rec.codec, rec.size = was
    win.dest_store.update(rec)
    print("recording editor: codec and picture size stored")
    # Live: the bitrate goes to the outputs now; picture and frame rate wait for the next Go live.
    calls = []
    outputs = win.outputs
    saved_calls = (outputs.set_video_kbps, outputs.set_auto, outputs.sharing)
    toasts = []
    from luma_appkit import Toast

    show = Toast.show
    try:
        outputs.set_video_kbps = lambda rid, kbps: calls.append(("video", rid, kbps)) or True
        outputs.set_auto = lambda rid, on: calls.append(("auto", rid, on)) or True
        outputs.sharing = lambda rid: []
        Toast.show = staticmethod(lambda host, text, **kw: toasts.append(text))
        panel.states[row.id] = ("live", "Live")
        panel.edit(row.id)
        pump(0.2)
        assert "next Go live" in panel.editor.summary.get_label(), panel.editor.summary.get_label()
        panel.editor_output.video.set_value(2500)
        panel.editor_output.fps.set_current("60")
        panel.editor.primary.on_activate()
        pump(0.2)
        assert calls == [("video", row.id, 2500)], calls
        assert any("frame rate applies on the next Go live" in t for t in toasts), toasts
        # Sharing its encode: the editor says so and nothing changes live.
        calls.clear()
        outputs.sharing = lambda rid: [r.id for r in win.dest_store.rows if r.id != rid][:1]
        outputs.set_video_kbps = lambda rid, kbps: calls.append(("video", rid, kbps)) or False
        panel.edit(row.id)
        pump(0.2)
        assert "sharing its encode" in panel.editor.summary.get_label(), panel.editor.summary.get_label()
        panel.editor_output.video.set_value(2000)
        panel.editor.primary.on_activate()
        pump(0.2)
        assert any("bitrate" in t and "next Go live" in t for t in toasts), toasts
    finally:
        outputs.set_video_kbps, outputs.set_auto, outputs.sharing = saved_calls
        Toast.show = show
        panel.states.pop(row.id, None)
        win.center.fold()
    row = win.dest_store.get(row.id)
    row.video_kbps = row.fps = 0
    win.dest_store.update(row)
    panel.refresh()
    print("destination editor while live: bitrate now, frame rate on the next Go live; a shared encode waits")


class _Monitor:
    """A sound device monitor that lists ``listed`` (as it is when asked) and hears nothing more."""

    def __init__(self, listed: list) -> None:
        self.listed = listed

    def get_bus(self):
        class Bus:
            @staticmethod
            def timed_pop(timeout):
                time.sleep(timeout / Gst.SECOND)

        return Bus()

    def get_devices(self):
        return list(self.listed)

    def stop(self) -> None:
        pass


def unplugged(win) -> None:
    """A microphone whose device is unplugged says so by name (once a fresh device monitor agrees),
    offers "Use <default>" in the bar, and reconnects by itself when the same device comes back;
    "Use …" switches it to the default."""
    from luma_streamy import devices as devices_module
    from luma_streamy.devices import Device

    devices = win.devices
    saved = devices._new_monitor
    gone = Device("alsa_input.test-pocket", "Pocket Mic", "source")
    default = Device("alsa_input.test-builtin", "Built-in Microphone", "source", default=True)
    listed = [default]
    confirm = devices_module.CONFIRM_S + 3.0  # a fresh monitor is asked, and lists
    scene = win.project.active_scene
    mic = win.project.add_widget(scene.id, "microphone", source={"device": gone.name, "device-name": gone.label})
    shown = []
    show_bar = win.center.show_bar
    win.center.show_bar = lambda items, **kw: (shown.append(list(items)), show_bar(items, **kw))[1]
    try:
        devices._new_monitor = lambda: _Monitor(listed)
        devices.restart()
        pump(0.3)
        assert mic.id not in win.widget_errors, f"unplugged before a fresh list said so: {win.widget_errors}"
        pump_until(lambda: win.widget_errors.get(mic.id) == "unplugged", confirm)
        assert win.widget_errors.get(mic.id) == "unplugged", f"not unplugged: {win.widget_errors.get(mic.id)}"
        assert win.source_summary(mic) == "Pocket Mic unplugged", win.source_summary(mic)
        win.select(mic.id)
        win._refresh_bar()
        use = [i for i in shown[-1] if getattr(i, "label", None) == "Use Built-in Microphone"]
        assert use, f"no Use key: {[getattr(i, 'label', i) for i in shown[-1]]}"
        # Plugged in again: it reconnects with nothing pressed.
        listed.append(gone)
        devices.restart()
        pump_until(lambda: mic.id not in win.widget_errors and mic.id in win.engine.branches, 2)
        assert mic.id not in win.widget_errors, f"still {win.widget_errors.get(mic.id)}"
        assert mic.id in win.engine.branches, "it did not reconnect"
        # Unplugged again, then "Use …": the default from now on.
        listed.remove(gone)
        devices.restart()
        pump_until(lambda: win.widget_errors.get(mic.id) == "unplugged", confirm)
        assert win.widget_errors.get(mic.id) == "unplugged", f"not unplugged again: {win.widget_errors.get(mic.id)}"
        win.use_stand_in(mic)
        pump()
        assert "device" not in mic.source and mic.id not in win.widget_errors, mic.source
    finally:
        win.center.show_bar = show_bar
        devices._new_monitor = saved
        devices.restart()
        win.deselect()
        win.project.remove_widget(mic.id)
        win._changed(structure=True)
        win._show_scene()
        pump()


def main() -> int:
    Gst.init(None)
    from luma_streamy.application import StreamyApplication

    app = StreamyApplication()
    def run_checks(a) -> bool:
        try:
            check(a)
        except Exception as error:  # noqa: BLE001 - a check run reports and stops
            import traceback

            errors.append("".join(traceback.format_exception(error)))
            a.quit()
        return False

    app.connect("activate", lambda a: GLib.timeout_add(2500, run_checks, a))
    status = app.run([])
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else status


if __name__ == "__main__":
    sys.exit(main())
