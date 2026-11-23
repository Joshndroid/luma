# SPDX-License-Identifier: Apache-2.0
"""The ScreenCast and Camera portals, and app sound through luma_appkit.audio_capture.

A display or window widget owns one ScreenCast session. The session stays open while
the widget exists, even when its scene isn't shown, so showing it again needs no
picker: only its PipeWire node is stopped. An app-audio widget owns one capture
(luma_appkit's AudioCapture) while its scene is shown; off-scene it is closed and
comes back from its restore token. Restore tokens are single-use: every share
returns a new one, which the widget keeps.

Restoring never strands a widget: a token the portal refuses (malformed, forgotten,
from an older portal) or a restore that fails is retried once as a fresh pick, and
every way a pick can end reaches ``done`` or ``failed`` with a reason the window can
put into words ("cancelled", "timeout", "failed", "unavailable").
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from gi.repository import Gio, GLib

BUS = "org.freedesktop.portal.Desktop"
PATH = "/org/freedesktop/portal/desktop"
SCREENCAST = "org.freedesktop.portal.ScreenCast"
CAMERA = "org.freedesktop.portal.Camera"
REQUEST = "org.freedesktop.portal.Request"
REGISTRY = "org.freedesktop.host.portal.Registry"

MONITOR, WINDOW = 1, 2
CURSOR_HIDDEN, CURSOR_EMBEDDED = 1, 2
PERSIST_UNTIL_REVOKED = 2
#: An older luma-portal gave up on its picker after D-Bus's default 25 s and answered
#: "cancelled". A cancel that lands right then is that timeout, not the person.
_DBUS_DEFAULT_TIMEOUT = 25.0


def log(text: str) -> None:
    """One line for the journal (the app runs as a systemd unit; stderr is its journal)."""
    print(f"streamy: {text}", file=sys.stderr, flush=True)


def register_host_app(app_id: str) -> None:
    """Tell xdg-desktop-portal who this host app is, before any other portal call on the
    session bus: the picker and the Shell's sharing pill then name Streamy, and restore
    tokens are kept for it rather than for "a host application". A portal without the
    registry (older than 1.19) is fine: nothing changes."""
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        bus.call_sync(BUS, PATH, REGISTRY, "Register", GLib.Variant("(sa{sv})", (app_id, {})), None,
                      Gio.DBusCallFlags.NONE, 2000, None)
    except GLib.Error:
        pass


@dataclass
class Capture:
    """An open capture: the PipeWire fd, the node to target, and what the portal said."""

    fd: int  # unused: each stream takes a connection of its own from ``pool``
    node: str | None
    size: tuple[int, int] | None
    restore_token: str | None
    session: str | None = None
    path: str | None = None
    name: str | None = None
    #: App audio: what is heard, in words ("Spotify, Firefox", "All sound"), and the
    #: luma_appkit capture that owns the session and the remote.
    description: str | None = None
    owner: object = None
    opener: Callable | None = None
    opening: bool = False
    pool: list = field(default_factory=list)
    watch: int = 0
    camera: object = None

    @property
    def aspect(self) -> float | None:
        if self.size and self.size[1]:
            return self.size[0] / self.size[1]
        return None


class PortalError(RuntimeError):
    pass


def pipewire_provider():
    """A PipeWire device provider of our own, or None. The factory's own (``factory.get()``) is one per
    process, shared with every GstDeviceMonitor: its start and stop are counted, so a second start with
    another fd connects nothing (the list comes from whichever remote started it first), and anyone's
    stop can end the connection a camera list depends on."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GObject, Gst

    Gst.init(None)
    factory = Gst.DeviceProviderFactory.find("pipewiredeviceprovider")
    factory = factory.load() if factory is not None else None
    if factory is None or not factory.get_device_provider_type():
        return None
    return GObject.new(factory.get_device_provider_type())


def _devices(fd: int, wanted: str):
    """The PipeWire devices a remote shows, through a provider of our own. The provider copies ``fd``
    and never closes it: the caller closes it once the provider has stopped."""
    provider = pipewire_provider()
    if provider is None:
        return None, []
    provider.set_property("fd", fd)
    if not provider.start():
        return None, []
    return provider, [d for d in provider.get_devices() if d.get_device_class().startswith(wanted)]


def _prop(device, key: str):
    props = device.get_properties()
    return str(props.get_value(key)) if props is not None and props.has_field(key) else None


def camera_nodes(fd: int) -> list[tuple[str, str, tuple[int, int] | None]]:
    """(target, name, size) for each camera on a camera-portal PipeWire remote.

    The target is the node's object.serial (or its node.name), which is what
    pipewiresrc's target-object wants on that remote; a global node id is not.
    """
    provider = pipewire_provider()
    if provider is None:
        os.close(fd)
        return []
    provider.set_property("fd", fd)  # copied, never closed by the provider: closed after stop()
    if not provider.start():
        os.close(fd)
        return []
    try:
        nodes = []
        for device in provider.get_devices():
            if not device.get_device_class().startswith("Video/Source"):
                continue
            props = device.get_properties()
            target = None
            for key in ("object.serial", "node.name"):
                if props is not None and props.has_field(key):
                    target = str(props.get_value(key))
                    break
            if target is None:
                continue
            size = None
            caps = device.get_caps()
            if caps is not None and caps.get_size():
                first = caps.get_structure(0)
                ok_w, width = first.get_int("width")
                ok_h, height = first.get_int("height")
                if ok_w and ok_h:
                    size = (width, height)
            nodes.append((target, device.get_display_name(), size))
        return nodes
    finally:
        provider.stop()
        os.close(fd)


def node_serial(fd: int, node_id: int) -> str | None:
    """The object.serial of node ``node_id`` on a portal's PipeWire remote (``fd``, closed here), or None.

    A portal older than ScreenCast v6 gives only the node id; pipewiresrc's target-object
    wants the serial, which the remote's own registry knows."""
    provider, devices = _devices(fd, "Video/Source")
    if provider is None:
        os.close(fd)
        return None
    try:
        for device in devices:
            ids = {_prop(device, "object.id"), _prop(device, "node.id")}
            if device.find_property("id") is not None:
                ids.add(str(device.get_property("id")))
            if str(node_id) in ids:
                serial = _prop(device, "object.serial")
                if serial is None and device.find_property("serial") is not None:
                    serial = str(device.get_property("serial"))
                return serial
        return None
    finally:
        provider.stop()
        os.close(fd)


@dataclass(frozen=True)
class Camera:
    """A camera on the camera portal's remote."""

    target: str  # object.serial (what this remote's pipewiresrc connects to)
    name: str  # node.name, stable across boots: what a widget remembers
    label: str  # "Built-in Front Camera"
    modes: tuple = ()  # ((width, height, fps), ...) best first
    api: str = ""  # "libcamera" | "v4l2"

    @property
    def size(self) -> tuple[int, int] | None:
        return self.modes[0][:2] if self.modes else None


def _rates(structure: str) -> list[int]:
    """Whole frame rates in a caps structure's text: one fraction, a list { 30/1, 25/1 } or a range
    [ 5/1, 30/1 ] (its ends). Read from the text: PyGObject can't hold a GstValueList."""
    import re

    found = re.search(r"framerate=\(fraction\)(\{[^}]*\}|\[[^\]]*\]|[0-9]+/[0-9]+)", structure)
    if found is None:
        return []
    rates = []
    for num, den in re.findall(r"([0-9]+)/([0-9]+)", found.group(1)):
        if int(den):
            rates.append(round(int(num) / int(den)))
    return rates


#: Picture sizes people choose by name (height): offered scaled when the camera lacks them.
STANDARD_SIZES = ((3840, 2160), (2560, 1440), (1920, 1080), (1280, 720), (854, 480))


def size_label(width: int, height: int) -> str:
    return f"{height}p" if (width, height) in STANDARD_SIZES or width * 9 == height * 16 else f"{width}×{height}"


def default_mode(modes) -> tuple[int, int, int] | None:
    """A new camera's mode: 1080p30 when it has it, else the largest size up to 1080p at the rate
    nearest 30 (4K costs far more to decode and composite than a stream shows)."""
    if not modes:
        return None
    fits = [m for m in modes if m[1] <= 1080] or list(modes)
    best = max(m[0] * m[1] for m in fits)
    return min((m for m in fits if m[0] * m[1] == best), key=lambda m: abs(m[2] - 30))


def resolutions(modes) -> list[tuple[str, str]]:
    """(key, label) per picture size: the camera's own (largest first), then standard sizes it lacks below
    its largest, made by scaling on the GPU ("720p, scaled"). Key "WxH" or "WxH:scaled"."""
    sizes = sorted({(m[0], m[1]) for m in modes}, key=lambda s: s[0] * s[1], reverse=True)
    out = [(f"{w}x{h}", size_label(w, h)) for w, h in sizes]
    if sizes:
        top = sizes[0][0] * sizes[0][1]
        for w, h in STANDARD_SIZES:
            if (w, h) not in sizes and w * h < top:
                out.append((f"{w}x{h}:scaled", f"{h}p, scaled"))
    return out


def rates(modes, size: tuple[int, int]) -> list[int]:
    """Frame rates the camera offers at ``size`` (a scaled size: the rates of the size it is made from)."""
    found = sorted({m[2] for m in modes if (m[0], m[1]) == tuple(size)}, reverse=True)
    return found


def source_for_scaled(modes, size: tuple[int, int]) -> tuple[int, int] | None:
    """The camera's own size a scaled size is made from: the smallest at least as large, same shape first."""
    w, h = size
    larger = [(m[0], m[1]) for m in modes if m[0] >= w and m[1] >= h]
    if not larger:
        return None
    return min(larger, key=lambda s: (abs(s[0] / s[1] - w / h) > 0.01, s[0] * s[1]))


def camera_label(description: str) -> str:
    """A camera's name as people know it: "DJIPocket3", not "DJIPocket3 (V4L2)"."""
    for suffix in (" (V4L2)", " (libcamera)"):
        if description.endswith(suffix):
            return description[: -len(suffix)]
    return description


def _modes(caps) -> tuple:
    """(width, height, fps) a camera offers, largest first, whole frame rates."""
    found = set()
    if caps is None:
        return ()
    for i in range(caps.get_size()):
        st = caps.get_structure(i)
        ok_w, width = st.get_int("width")
        ok_h, height = st.get_int("height")
        if not (ok_w and ok_h):
            continue
        rates = _rates(st.to_string()) if st.has_field("framerate") else []
        for fps in rates or [30]:
            if fps > 0:
                found.add((width, height, fps))
    return tuple(sorted(found, key=lambda m: (m[0] * m[1], m[2]), reverse=True))


def _camera(device) -> Camera | None:
    target = _prop(device, "object.serial") or _prop(device, "node.name")
    if target is None:
        return None
    label = camera_label(_prop(device, "node.description") or device.get_display_name())
    return Camera(target, _prop(device, "node.name") or target, label, _modes(device.get_caps()),
                  _prop(device, "device.api") or "")


def unique_cameras(cameras: list) -> list:
    """One row per physical camera: a camera both libcamera and V4L2 show (same name) once, libcamera's."""
    by_label: dict[str, object] = {}
    for camera in cameras:
        kept = by_label.get(camera.label)
        if kept is None or (getattr(kept, "api", "") != "libcamera" and getattr(camera, "api", "") == "libcamera"):
            by_label[camera.label] = camera
    return [c for c in cameras if by_label.get(c.label) is c]


#: A camera missing from the list is gone only once a connection opened this long after it went missing
#: says so too. The camera service recreates every camera node when it restarts (a second or so), and a
#: connection that died with it, or that was opened while it was down, keeps a list that never changes.
CONFIRM_S = 1.5
#: A connection whose list hasn't come in this long (the service never let it in) is given up for a new one.
READY_S = 4.0
#: While a chosen camera stays missing, a fresh connection looks again: first after this long, then less often.
RECHECK_S = 20.0
RECHECK_MAX_S = 120.0


def _fd_add(fd: int, condition: GLib.IOCondition, callback: Callable) -> int:
    """A main-loop watch on ``fd`` (GLibUnix where GLib has it)."""
    import gi

    try:
        gi.require_version("GLibUnix", "2.0")
        from gi.repository import GLibUnix

        return GLibUnix.fd_add_full(GLib.PRIORITY_DEFAULT, fd, condition, callback)
    except (ImportError, ValueError):
        return GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, fd, condition, callback)


class CameraWatch:
    """The cameras Streamy may use, kept current on a PipeWire connection of its own on the camera portal's
    remote (``open_remote(done)`` opens one: ``done(fd)`` or ``done(None)``, on the window's thread).
    ``on_changed`` runs on the window's thread whenever the list changes.

    The connection is replaced by a fresh one (the portal asks nothing again) when it can't be trusted:
    the service closed it, its list never came, the camera service restarted (``reopen``), a camera
    stream ended, or a chosen camera is missing (``missing``). A fresh list replaces the old one only
    once it is in: until then the old one stays, so nothing is declared gone from a connection that
    died."""

    def __init__(self, open_remote: Callable[[Callable[[int | None], None]], None], on_changed: Callable[[], None],
                 provider: Callable = None) -> None:
        self.cameras: list[Camera] = []
        #: A list is known (from a live connection, or empty with ``error`` when none could be opened).
        self.ready = False
        #: Why there is no list (the device provider failed), in words; None when it is fine.
        self.error: str | None = None
        self.on_changed = on_changed
        self._open_remote = open_remote
        self._provider = provider or (lambda fd: _devices(fd, "Video/Source")[0])  # the caller closes fd
        self._lock = threading.Lock()
        self._generation = 0  # the newest connection asked for
        self._live = 0  # the connection the list comes from
        self._opened_at = float("-inf")  # when the live connection was asked for (monotonic)
        self._reopening = False
        self._retry_s = 1.0
        self._hangup: tuple[int, int] | None = None  # (GLib source, our copy of the live connection's fd)
        self._missing_since: dict[str, float] = {}
        self._recheck_s = RECHECK_S
        self._timers: dict[str, int] = {}
        self.reopen("start")

    # ── Connections ──

    def reopen(self, reason: str) -> None:
        """Open a fresh connection; its list replaces this one once it is in. One at a time."""
        if self._reopening:
            return
        self._reopening = True
        self._generation += 1
        generation, asked = self._generation, time.monotonic()
        if reason != "start":
            log(f"cameras: {reason}; listing them on a fresh connection")

        def opened(fd: int | None) -> None:
            if generation != self._generation:
                if fd is not None:
                    os.close(fd)
                return
            if fd is None:
                self._failed(generation, "The camera service did not answer")
                return
            # Our own copy of the connection hears the service close it (the provider tells no one).
            watch_fd = os.dup(fd)
            threading.Thread(target=self._run, args=(fd, watch_fd, generation, asked), name="streamy-cameras",
                             daemon=True).start()

        self._open_remote(opened)
        self._after("late", READY_S, lambda: self._failed(generation, "The camera list did not come"))

    def _run(self, fd: int, watch_fd: int, generation: int, asked: float) -> None:
        """One connection's thread: its first list, then its changes, until it is no longer wanted."""
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        try:
            provider = self._provider(fd)
        except Exception as error:  # noqa: BLE001 - the list must come back, whatever failed
            log(f"cameras: {error}")
            provider = None
        if provider is None:
            os.close(watch_fd)
            os.close(fd)
            GLib.idle_add(lambda: self._failed(generation, "The camera list could not be read") and False,
                          priority=GLib.PRIORITY_DEFAULT)
            return
        bus = provider.get_bus()
        try:
            # A fresh client hears of the nodes as it starts (a list taken at once can be empty): the
            # first list waits until the provider has been quiet for a moment, a second at most.
            settled = time.monotonic() + 1.0
            while time.monotonic() < settled and bus.timed_pop(100 * Gst.MSECOND) is not None:
                pass
            # At the main loop's own priority: a busy window (redraws) never holds the list back.
            GLib.idle_add(lambda cameras=self._list(provider): self._listed(generation, asked, watch_fd, cameras)
                          and False, priority=GLib.PRIORITY_DEFAULT)
            while generation in (self._generation, self._live):
                message = bus.timed_pop(250 * Gst.MSECOND)
                if message is not None and message.type in (Gst.MessageType.DEVICE_ADDED,
                                                             Gst.MessageType.DEVICE_REMOVED,
                                                             Gst.MessageType.DEVICE_CHANGED):
                    GLib.idle_add(lambda cameras=self._list(provider): self._changed(generation, cameras) and False,
                                  priority=GLib.PRIORITY_DEFAULT)
        finally:
            provider.stop()
            os.close(fd)  # the provider copied it: closed, the service sees this client go

    @staticmethod
    def _list(provider) -> list[Camera]:
        cameras = []
        for device in provider.get_devices():
            if device.get_device_class().startswith("Video/Source"):
                try:
                    camera = _camera(device)
                except Exception as error:  # noqa: BLE001 - one odd device never hides the others
                    log(f"camera {device.get_display_name()}: {error}")
                    camera = None
                if camera is not None:
                    cameras.append(camera)
        return unique_cameras(cameras)

    def _listed(self, generation: int, asked: float, watch_fd: int, cameras: list[Camera]) -> None:
        """A fresh connection's first list: it is the live one from now on (the old one's thread ends)."""
        if generation != self._generation:
            os.close(watch_fd)
            return
        self._cancel("late")
        self._reopening, self._retry_s = False, 1.0
        self._drop_hangup()
        self._live, self._opened_at, self.error = generation, asked, None

        def hung_up(*_args) -> bool:
            if self._hangup is not None and self._hangup[1] == watch_fd:
                self._hangup = None
                os.close(watch_fd)
                self.reopen("the camera service closed the connection")
            return False

        source = _fd_add(watch_fd, GLib.IOCondition.HUP | GLib.IOCondition.ERR, hung_up)
        self._hangup = (source, watch_fd)
        self._set(cameras)

    def _changed(self, generation: int, cameras: list[Camera]) -> None:
        with self._lock:
            same = cameras == self.cameras
        if generation == self._live and not same:  # the provider reports every node it hears of
            self._set(cameras)

    def _failed(self, generation: int, error: str) -> None:
        """A fresh connection failed or never listed: the old list stays (or none, said in words) and it
        is tried again, less often each time."""
        if generation != self._generation or not self._reopening:
            return
        self._cancel("late")
        self._reopening = False
        self._generation += 1  # a list that comes in late is not wanted any more
        log(f"cameras: {error}; trying again in {self._retry_s:.0f} s")
        self._after("retry", self._retry_s, lambda: self.reopen("trying again"))
        self._retry_s = min(self._retry_s * 2, 30.0)
        if not self.ready:
            self.error = error
            self._set([])

    def _set(self, cameras: list[Camera]) -> None:
        with self._lock:
            self.cameras, self.ready = cameras, True
        self.on_changed()

    def _drop_hangup(self) -> None:
        if self._hangup is not None:
            source, fd = self._hangup
            self._hangup = None
            GLib.source_remove(source)
            os.close(fd)

    def _after(self, key: str, seconds: float, then: Callable[[], None]) -> None:
        self._cancel(key)

        def fire() -> bool:
            self._timers.pop(key, None)
            then()
            return False

        self._timers[key] = GLib.timeout_add(int(seconds * 1000), fire)

    def _cancel(self, key: str) -> None:
        timer = self._timers.pop(key, None)
        if timer:
            GLib.source_remove(timer)

    def close(self) -> None:
        for key in list(self._timers):
            self._cancel(key)
        self._drop_hangup()
        self._generation += 1
        self._live = -1

    # ── Questions ──

    def find(self, name: str | None) -> Camera | None:
        with self._lock:
            if name:
                return next((c for c in self.cameras if c.name == name), None)
            return self.cameras[0] if self.cameras else None

    def missing(self, name: str) -> bool:
        """Camera ``name`` is gone: absent from a list whose connection was opened at least CONFIRM_S after
        it went missing. Until one is, the answer is no, and such a connection is on its way."""
        if not name or not self.ready or self.find(name) is not None:
            if self._missing_since.pop(name, None) is not None and not self._missing_since:
                self._recheck_s = RECHECK_S
                self._cancel("recheck")
                self._cancel("confirm")  # it is back: nothing to confirm
            return False
        now = time.monotonic()
        since = self._missing_since.setdefault(name, now)
        if self._opened_at >= since + CONFIRM_S:
            if "recheck" not in self._timers:
                # Gone, as far as a live connection knows; a fresh one looks again now and then.
                self._after("recheck", self._recheck_s, lambda: self.reopen("a chosen camera is still missing"))
                self._recheck_s = min(self._recheck_s * 2, RECHECK_MAX_S)
            return True
        if "confirm" not in self._timers:
            self._after("confirm", max(0.0, since + CONFIRM_S - now),
                        lambda: self.reopen(f"camera {name} is missing"))
        return False


class Portals:
    def __init__(self) -> None:
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.captures: dict[str, Capture] = {}  # widget id -> open capture
        self.on_ready: Callable[[str], None] | None = None
        #: A capture the portal ended by itself (widget id).
        self.on_ended: Callable[[str], None] | None = None
        #: The cameras, once the camera portal said yes (CameraWatch); ``on_cameras()`` follows them.
        self.cameras: CameraWatch | None = None
        #: The node each closed camera capture was on (widget id -> target): a camera that comes back on
        #: another node (its service restarted, or it was plugged in again) is worth opening again.
        self.last_node: dict[str, str | None] = {}
        self.on_cameras: Callable[[], None] | None = None
        self._camera_waiters: list[Callable[[], None]] = []

    # ── Requests ──

    def _token(self) -> str:
        return f"streamy_{secrets.token_hex(6)}"

    def _request(self, interface: str, method: str, args: list, options: dict, done: Callable[[dict], None],
                 failed: Callable[[str], None], fd_list: Gio.UnixFDList | None = None) -> None:
        """Call a portal method that answers through a Request object's Response signal."""
        token = self._token()
        options = dict(options)
        options["handle_token"] = GLib.Variant("s", token)
        handle = f"/org/freedesktop/portal/desktop/request/{self.sender}/{token}"
        subscription = 0

        def response(_bus, _sender, _path, _iface, _signal, params) -> None:
            self.bus.signal_unsubscribe(subscription)
            code, results = params.unpack()
            if code == 0:
                done(results)
            else:
                failed("cancelled" if code == 1 else "failed")

        subscription = self.bus.signal_subscribe(BUS, REQUEST, "Response", handle, None,
                                                 Gio.DBusSignalFlags.NO_MATCH_RULE, response)
        def called(bus, result) -> None:
            try:
                bus.call_finish(result)
            except GLib.Error as error:
                self.bus.signal_unsubscribe(subscription)
                failed(error.message)

        self.bus.call(BUS, PATH, interface, method, GLib.Variant.new_tuple(*args, GLib.Variant("a{sv}", options)),
                      None, Gio.DBusCallFlags.NONE, -1, None, called)

    def _open_remote_async(self, interface: str, args: list, done: Callable[[int | None], None]) -> None:
        """OpenPipeWireRemote without waiting on the portal: ``done(fd)``, or ``done(None)``."""
        def opened(bus, result) -> None:
            try:
                value, fds = bus.call_with_unix_fd_list_finish(result)
                done(fds.get(value.unpack()[0]))
            except GLib.Error:
                done(None)

        self.bus.call_with_unix_fd_list(BUS, PATH, interface, "OpenPipeWireRemote",
                                        GLib.Variant.new_tuple(*args, GLib.Variant("a{sv}", {})),
                                        GLib.VariantType("(h)"), Gio.DBusCallFlags.NONE, 10000, None, None, opened)

    def _property(self, interface: str, name: str):
        try:
            value = self.bus.call_sync(BUS, PATH, "org.freedesktop.DBus.Properties", "Get",
                                       GLib.Variant("(ss)", (interface, name)), GLib.VariantType("(v)"),
                                       Gio.DBusCallFlags.NONE, 2000, None)
            return value.unpack()[0]
        except GLib.Error:
            return None

    # ── Screen, window and application sound ──

    def screencast(self, widget_id: str, *, window: bool, restore_token: str | None,
                   done: Callable[[Capture], None], failed: Callable[[str], None], parent: str = "",
                   cursor: bool = True) -> None:
        """Pick (or restore) a display or a window; ``done`` gets the open capture. ``cursor``: the
        pointer drawn into the picture, or left out."""
        cursor_modes = self._property(SCREENCAST, "AvailableCursorModes") or 0
        options = {"types": GLib.Variant("u", WINDOW if window else MONITOR),
                   "multiple": GLib.Variant("b", False)}
        if cursor and cursor_modes & CURSOR_EMBEDDED:
            options["cursor_mode"] = GLib.Variant("u", CURSOR_EMBEDDED)
        elif not cursor and cursor_modes & CURSOR_HIDDEN:
            options["cursor_mode"] = GLib.Variant("u", CURSOR_HIDDEN)
        self._share(SCREENCAST, widget_id, options, restore_token, done, failed, parent)

    def audio_capture_available(self) -> bool:
        """Whether Luma's Audio Capture portal is here (the Share sound picker chooses what is heard)."""
        from luma_appkit import audio_capture

        return audio_capture.is_available(self.bus)

    def app_audio(self, widget_id: str, *, restore_token: str | None, done: Callable[[Capture], None],
                  failed: Callable[[str], None], parent: str = "", retried: bool = False) -> None:
        """Pick (or restore) the sound a widget carries: chosen apps, or all sound but Streamy's own.
        A token the portal no longer honours falls back to the picker once, as screen shares do."""
        from luma_appkit import audio_capture

        if widget_id in self.captures:
            done(self.captures[widget_id])
            return
        if not self.audio_capture_available():
            failed("unavailable")
            return

        def ready(granted, error) -> None:
            if error is not None:
                if error.cancelled:
                    failed("cancelled")
                elif restore_token and not retried:
                    self.app_audio(widget_id, restore_token=None, done=done, failed=failed, parent=parent,
                                   retried=True)
                else:
                    failed("failed")
                return
            capture = Capture(-1, granted.target, None, granted.restore_token, granted.session_handle)
            capture.description = granted.description
            capture.owner = granted
            # The capture owns its remote; each pipewiresrc gets a duplicate of it to own and close.
            capture.opener = lambda give: give(os.dup(granted.fd) if granted.fd >= 0 else None)
            granted.on_closed = lambda _c: self._ended(widget_id, capture)
            capture.pool.append(os.dup(granted.fd))
            self.captures[widget_id] = capture
            done(capture)

        audio_capture.request_audio_capture(parent or None, ready, types=audio_capture.APPLICATIONS |
                                            audio_capture.ALL_SOUND, multiple=True, include_self=False,
                                            persist=True, restore_token=restore_token, connection=self.bus)

    def _ended(self, widget_id: str, capture: "Capture") -> None:
        """The portal ended a capture (the person stopped sharing): the widget hears so."""
        if self.captures.get(widget_id) is capture:
            self.close(widget_id)
            if self.on_ended is not None:
                self.on_ended(widget_id)

    def _share(self, interface: str, widget_id: str, options: dict, restore_token: str | None,
               done: Callable[[Capture], None], failed: Callable[[str], None], parent: str,
               *, retried: bool = False) -> None:
        """CreateSession → SelectSources → Start → OpenPipeWireRemote, the ScreenCast way.

        A restore token is only a shortcut: if the portal refuses it, or the restored
        share fails, the same widget is asked once more with no token (the picker)."""
        if widget_id in self.captures:
            done(self.captures[widget_id])
            return
        version = self._property(interface, "version") or 1
        token = restore_token if version >= 4 and not retried else None
        state = {"session": None, "started": 0.0}
        reasons = ("cancelled", "failed", "timeout")
        given_up = failed
        failed = lambda reason: given_up(reason if reason in reasons else "failed")  # noqa: E731

        def again(reason: str) -> None:
            self._close_session(state["session"])
            if token and not retried and reason != "cancelled":
                self._share(interface, widget_id, options, None, done, failed, parent, retried=True)
                return
            if reason == "cancelled" and state["started"] and \
                    abs(time.monotonic() - state["started"] - _DBUS_DEFAULT_TIMEOUT) < 1.5:
                reason = "timeout"  # the picker's own D-Bus call ran out, not the person
            failed(reason)

        def created(results: dict) -> None:
            session = state["session"] = results["session_handle"]
            select = dict(options)
            if version >= 4:
                select["persist_mode"] = GLib.Variant("u", PERSIST_UNTIL_REVOKED)
                if token:
                    select["restore_token"] = GLib.Variant("s", token)
            self._request(interface, "SelectSources", [GLib.Variant("o", session)], select,
                          lambda _r: start(session), again)

        def start(session: str) -> None:
            state["started"] = time.monotonic()
            self._request(interface, "Start", [GLib.Variant("o", session), GLib.Variant("s", parent)], {},
                          lambda results: started(session, results), again)

        def started(session: str, results: dict) -> None:
            streams = results.get("streams") or []
            if not streams:
                again("failed")
                return
            node_id, props = streams[0]
            size = tuple(props["size"]) if "size" in props else None
            # pipewiresrc's target-object is an object.serial, never a node id: pipewire-serial
            # (portal v6) is that serial; an older portal gives only the id, which goes to "path"
            # (pw_stream_connect's target id, as OBS connects).
            serial = props.get("pipewire-serial")
            capture = Capture(-1, str(serial) if serial is not None else None, size,
                              results.get("restore_token"), session)
            capture.path = None if serial is not None else str(node_id)
            capture.opener = lambda ready: self._open_remote_async(interface, [GLib.Variant("o", session)], ready)

            def first(fd: int | None) -> None:
                if fd is None:
                    again("failed")
                    return
                capture.pool.append(fd)
                self.captures[widget_id] = capture
                self._watch_session(widget_id, capture)
                if capture.node is not None:
                    done(capture)
                    return
                # No pipewire-serial (a portal before ScreenCast v6): ask the remote's registry
                # for the node's serial, off the window's thread; the node id is the fallback.
                def resolved(look_fd: int | None) -> None:
                    if look_fd is None:
                        done(capture)
                        return

                    def look() -> None:
                        serial = node_serial(look_fd, node_id)
                        GLib.idle_add(found, serial)

                    def found(serial) -> bool:
                        if serial is not None:
                            capture.node, capture.path = serial, None
                        log(f"portal stream {node_id}: " + (f"object.serial {serial}" if serial is not None
                                                             else "no serial on the remote, connecting by id"))
                        done(capture)
                        return False

                    threading.Thread(target=look, name="streamy-node-serial", daemon=True).start()

                capture.opener(resolved)

            capture.opener(first)

        self._request(interface, "CreateSession", [], {"session_handle_token": GLib.Variant("s", self._token())},
                      created, failed)

    def _watch_session(self, widget_id: str, capture: "Capture") -> None:
        """The portal closing a share (the person stopped it in the Shell, the window went) ends the widget."""
        if not capture.session:
            return

        def closed(*_args) -> None:
            self.bus.signal_unsubscribe(capture.watch)
            capture.watch = 0
            log(f"portal session for {widget_id} closed")
            self._ended(widget_id, capture)

        capture.watch = self.bus.signal_subscribe(BUS, "org.freedesktop.portal.Session", "Closed", capture.session,
                                                  None, Gio.DBusSignalFlags.NONE, closed)

    def _close_session(self, session: str | None) -> None:
        if session:
            self.bus.call(BUS, session, "org.freedesktop.portal.Session", "Close", None, None,
                          Gio.DBusCallFlags.NONE, -1, None, None)

    # ── Cameras ──

    def camera_present(self) -> bool:
        return bool(self._property(CAMERA, "IsCameraPresent"))

    def _cameras_changed(self) -> None:
        waiters, self._camera_waiters = self._camera_waiters, []
        for then in waiters:
            then()
        if self.on_cameras is not None:
            self.on_cameras()

    def watch_cameras(self, then: Callable[[], None], failed: Callable[[str], None]) -> None:
        """Ask the camera portal once, then keep the list of cameras current; ``then()`` once it is known."""
        if self.cameras is not None:
            if self.cameras.ready:
                then()
            else:
                self._camera_waiters.append(then)
            return

        def allowed(_results: dict) -> None:
            self._camera_waiters.append(then)
            # The remote shows only the cameras Streamy may use; one connection is kept for watching
            # them (opened again whenever it can't be trusted), each stream opens its own.
            self.cameras = CameraWatch(lambda done: self._open_remote_async(CAMERA, [], done), self._cameras_changed)

        def refused(reason: str) -> None:
            failed("denied" if reason == "cancelled" else reason)

        self._request(CAMERA, "AccessCamera", [], {}, allowed, refused)

    def camera(self, widget_id: str, *, done: Callable[[Capture], None], failed: Callable[[str], None],
               name: str | None = None) -> None:
        """Open camera ``name`` (a node name; None: the first). "no camera" when there is none,
        "missing" when that one is gone, "denied" when the portal says no."""
        if widget_id in self.captures:
            done(self.captures[widget_id])
            return

        def known() -> None:
            camera = self.cameras.find(name)
            if camera is None:
                failed("missing" if name and self.cameras.cameras else "no camera")
                return
            capture = Capture(-1, camera.target, camera.size, None)
            capture.opener = lambda ready: self._open_remote_async(CAMERA, [], ready)
            capture.name = camera.label
            capture.camera = camera
            self.captures[widget_id] = capture
            done(capture)

        self.watch_cameras(known, failed)

    # ── Closing ──

    def close(self, widget_id: str) -> None:
        """The widget is gone: close its session (its restore token stays with the widget)."""
        capture = self.captures.pop(widget_id, None)
        if capture is None:
            return
        if capture.camera is not None:
            self.last_node[widget_id] = capture.node
        for fd in capture.pool:
            os.close(fd)
        capture.pool.clear()
        if capture.owner is not None:
            capture.owner.on_closed = None
            capture.owner.close()  # ends its session and closes its remote
        else:
            if capture.watch:
                self.bus.signal_unsubscribe(capture.watch)
                capture.watch = 0
            self._close_session(capture.session)

    def source_for(self, widget) -> tuple[int | None, str | None]:
        """The engine's question: a PipeWire connection of its own (pipewiresrc owns it) and the node
        (an object.serial, or "path:<id>"). Connections are opened ahead without waiting on the portal;
        when none is ready this says (None, None) and ``on_ready(widget_id)`` follows once one is."""
        key = getattr(widget, "link", widget.id)  # captures are a source's, shared by its widgets
        capture = self.captures.get(key)
        if capture is None:
            return None, None
        target = capture.node or (f"path:{capture.path}" if capture.path else None)
        if capture.pool:
            fd = capture.pool.pop()
            self._refill(key, capture)
            return fd, target
        self._refill(key, capture, notify=True)
        return None, None

    def _refill(self, widget_id: str, capture: "Capture", notify: bool = False) -> None:
        if capture.opening or capture.opener is None or capture.pool:
            return
        capture.opening = True

        def ready(fd: int | None) -> None:
            capture.opening = False
            if self.captures.get(widget_id) is not capture:
                if fd is not None:
                    os.close(fd)
                return
            if fd is not None:
                capture.pool.append(fd)
                if notify and self.on_ready is not None:
                    self.on_ready(widget_id)

        capture.opener(ready)
