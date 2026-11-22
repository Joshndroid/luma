# SPDX-License-Identifier: Apache-2.0
"""Microphones, speakers and their monitors, as GStreamer's device monitor sees them, kept current.

The monitor runs on a thread of its own (starting it asks PipeWire and can take a moment);
the window hears ``on_changed()`` on its own thread whenever a device comes, goes or becomes
the default. A device is named by its node name (what pulsesrc's ``device`` and pipewiresrc's
``target-object`` both take); None means the system default, which follows changes.
"""
from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

from gi.repository import Gio, GLib

#: A device missing from the list is unplugged only once a monitor started this long after it went
#: missing says so too: a sound service that restarts recreates every device (a second or so), and a
#: monitor whose connection died with it keeps a list that never changes.
CONFIRM_S = 1.5


@dataclass(frozen=True)
class Device:
    name: str  # node name
    label: str  # "Built-in Digital Microphone"
    kind: str  # "source" | "sink"
    default: bool = False
    internal: bool = False  # built into the computer (a laptop's speakers or microphone)
    #: The card it belongs to, shown under the label only when two devices would read the same.
    card: str = ""


def _monitor():
    """A started device monitor for microphones and outputs, or None."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    Gst.init(None)
    monitor = Gst.DeviceMonitor.new()
    monitor.add_filter("Audio/Source", None)
    monitor.add_filter("Audio/Sink", None)
    return monitor if monitor.start() else None


def _props(device) -> dict:
    props = device.get_properties()
    out = {}
    if props is None:
        return out
    for i in range(props.n_fields()):
        key = props.nth_field_name(i)
        try:
            out[key] = props.get_value(key)
        except TypeError:
            continue
    return out


def _device(gst_device) -> Device | None:
    klass = gst_device.get_device_class()
    kind = "source" if klass.startswith("Audio/Source") else "sink" if klass.startswith("Audio/Sink") else None
    if kind is None:
        return None
    props = _props(gst_device)
    name = props.get("node.name") or props.get("device.name")
    if not name:
        element = gst_device.create_element(None)
        if element is not None:
            for key in ("device", "target-object"):
                if element.find_property(key) is not None and element.get_property(key):
                    name = element.get_property(key)
                    break
    if not name or str(name).endswith(".monitor"):
        return None
    text = " ".join(str(v) for v in props.values()).lower()
    internal = props.get("device.form-factor") == "internal" or props.get("device.form_factor") == "internal" or \
        any(word in text for word in ("built-in", "internal", "sof_sdw", "sof-hda", "hda intel"))
    label, card = friendly_name(props, gst_device.get_display_name())
    return Device(str(name), label, kind, bool(props.get("is-default")), internal, card)


#: Profile words that say how, not what (the product's own name says what).
_GENERIC = ("analog stereo", "analog mono", "mono", "stereo", "digital stereo", "analog surround",
            "multichannel", "pro audio", "pro")


def friendly_name(props: dict, display: str) -> tuple[str, str]:
    """(label, card) for a sound device as people know it: "Speaker", "Headphones", "HDMI 1 – LG ULTRAWIDE",
    "DJIPocket3"; the card ("Core Ultra Processors (Series 3) HD Audio") apart, for when it's needed."""
    import re

    profile = str(props.get("device.profile.description") or "").strip()
    nick = str(props.get("node.nick") or "").strip()
    description = str(props.get("node.description") or display or "").strip()
    card = description
    if profile and profile in description:
        card = description[: description.index(profile)].strip()
    card = re.sub(r"\s*\((stereo|mono)\)$", "", card, flags=re.I).strip()
    hdmi = re.match(r"HDMI\s*/\s*DisplayPort\s*(\d+)", profile)
    if hdmi:
        screen = nick if nick and not nick.upper().startswith("HDMI") else ""
        label = f"HDMI {hdmi.group(1)}" + (f" – {screen}" if screen else "")
    elif profile and profile.lower() not in _GENERIC and not profile.lower().startswith(("analog", "digital")):
        label = profile
    else:
        label = nick or re.sub(r"\s+(analog stereo|analog mono|mono|stereo|digital stereo)$", "", description,
                                  flags=re.I)
        card = ""
    return label or display, card


class AudioDevices:
    """The machine's microphones and outputs; ``on_changed`` follows them. The monitor is started afresh
    (``restart``) when its list can't be trusted: the sound service restarted, or a chosen device is
    missing (``missing``). The fresh list replaces the old one once it is in."""

    def __init__(self, on_changed: Callable[[], None] | None = None, monitor: Callable | None = None) -> None:
        self.on_changed = on_changed
        self.devices: list[Device] = []
        self.ready = False  # listed once
        #: Devices that came back (missing from the list before this one): their streams open again.
        self.returned: set[str] = set()
        self._new_monitor = monitor or _monitor
        self._lock = threading.Lock()
        self._generation = 0
        self._started_at = float("-inf")  # when the monitor the list comes from was started (monotonic)
        self._missing_since: dict[str, float] = {}
        self._confirm = 0
        self._thread: threading.Thread | None = None
        self.restart()

    def restart(self) -> None:
        """A fresh monitor; its list replaces this one's once it is in. The old monitor stops first: a
        monitor's device providers are GStreamer's one-per-process ones, whose start and stop are
        counted, so one started beside the old would share its connection rather than open its own."""
        self._generation += 1
        self._thread = threading.Thread(target=self._start, args=(self._generation, time.monotonic(), self._thread),
                                        name="streamy-devices", daemon=True)
        self._thread.start()

    def _start(self, generation: int, started: float, before: threading.Thread | None) -> None:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst

        if before is not None:
            before.join(2.0)  # it sees it is not wanted within a quarter second, and stops its monitor
        if generation != self._generation:
            return
        monitor = self._new_monitor()
        if monitor is None:
            return
        try:
            bus = monitor.get_bus()
            self._refresh(generation, started, monitor)
            while generation == self._generation:  # the monitor's own bus, on this thread
                message = bus.timed_pop(250 * Gst.MSECOND)
                if message is not None and message.type in (Gst.MessageType.DEVICE_ADDED,
                                                             Gst.MessageType.DEVICE_REMOVED,
                                                             Gst.MessageType.DEVICE_CHANGED):
                    self._refresh(generation, started, monitor)
        finally:
            monitor.stop()

    def _refresh(self, generation: int, started: float, monitor) -> None:
        found, seen = [], set()
        for gst_device in monitor.get_devices() or []:
            device = gst_device if isinstance(gst_device, Device) else _device(gst_device)
            if device is not None and (device.kind, device.name) not in seen:
                seen.add((device.kind, device.name))
                found.append(device)
        # At the main loop's own priority: a busy window (redraws) never holds the list back.
        GLib.idle_add(lambda: self._listed(generation, started, found) and False, priority=GLib.PRIORITY_DEFAULT)

    def _listed(self, generation: int, started: float, found: list[Device]) -> None:
        if generation != self._generation:
            return
        with self._lock:
            before = {d.name for d in self.devices}
            self.devices, self.ready = found, True
        self._started_at = started
        self.returned = {d.name for d in found if d.name in self._missing_since or
                         (before and d.name not in before)}
        for name in self.returned:
            self._missing_since.pop(name, None)
        if self.on_changed is not None:
            self.on_changed()
        self.returned = set()

    def missing(self, kind: str, name: str | None) -> bool:
        """Device ``name`` is unplugged: absent from a monitor started at least CONFIRM_S after it went
        missing. Until one is, the answer is no, and such a monitor is on its way."""
        if not name or not self.ready or self.find(kind, name) is not None:
            return False
        now = time.monotonic()
        since = self._missing_since.setdefault(name, now)
        if self._started_at >= since + CONFIRM_S:
            return True
        if not self._confirm:
            def confirm() -> bool:
                self._confirm = 0
                self.restart()
                return False

            self._confirm = GLib.timeout_add(int(max(0.0, since + CONFIRM_S - now) * 1000), confirm)
        return False

    def of(self, kind: str) -> list[Device]:
        with self._lock:
            return [d for d in self.devices if d.kind == kind]

    def subtitle(self, device: Device) -> str | None:
        """The card's name, only when another device of the kind reads the same."""
        same = [d for d in self.of(device.kind) if d.label == device.label]
        return device.card or None if len(same) > 1 else None

    def default(self, kind: str) -> Device | None:
        listed = self.of(kind)
        return next((d for d in listed if d.default), listed[0] if listed else None)

    def find(self, kind: str, name: str | None) -> Device | None:
        if not name:
            return self.default(kind)
        return next((d for d in self.of(kind) if d.name == name), None)

    def words(self, kind: str, name: str | None) -> str:
        """What a strip says it hears: "System default (Built-in Digital Microphone)", or the device."""
        if not name:
            default = self.default(kind)
            return f"System default ({default.label})" if default else "System default"
        device = self.find(kind, name)
        return device.label if device else "Disconnected"


class ServiceRestarts:
    """``on_restart(unit)`` on the window's thread whenever PipeWire, its session manager or its sound
    server starts again (its main process changed): every device node is new then, and connections to
    it may have died with the old one. Nothing happens without systemd's user manager."""

    #: wireplumber@video-capture hosts the camera monitors on Luma (cameras in their own instance).
    UNITS = ("pipewire.service", "wireplumber.service", "wireplumber@video-capture.service", "pipewire-pulse.service")
    SYSTEMD = "org.freedesktop.systemd1"

    def __init__(self, on_restart: Callable[[str], None], bus=None) -> None:
        self.on_restart = on_restart
        self._pids: dict[str, int] = {}
        try:
            self.bus = bus or Gio.bus_get_sync(Gio.BusType.SESSION, None)
            # systemd sends property changes to subscribed clients.
            self.bus.call_sync(self.SYSTEMD, "/org/freedesktop/systemd1", f"{self.SYSTEMD}.Manager", "Subscribe",
                               None, None, Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error as error:
            print(f"streamy: service restarts aren't followed: {error.message}", file=sys.stderr, flush=True)
            return
        for unit in self.UNITS:
            path = "/org/freedesktop/systemd1/unit/" + "".join(c if c.isalnum() else f"_{ord(c):02x}" for c in unit)
            self._pids[unit] = self._main_pid(path)
            self.bus.signal_subscribe(self.SYSTEMD, "org.freedesktop.DBus.Properties", "PropertiesChanged", path,
                                      f"{self.SYSTEMD}.Service", Gio.DBusSignalFlags.NONE, self._changed, unit)

    def _main_pid(self, path: str) -> int:
        try:
            value = self.bus.call_sync(self.SYSTEMD, path, "org.freedesktop.DBus.Properties", "Get",
                                       GLib.Variant("(ss)", (f"{self.SYSTEMD}.Service", "MainPID")),
                                       GLib.VariantType("(v)"), Gio.DBusCallFlags.NONE, 2000, None)
            return int(value.unpack()[0])
        except GLib.Error:
            return 0

    def _changed(self, _bus, _sender, _path, _iface, _signal, params, unit) -> None:
        _interface, changed, _invalidated = params.unpack()
        pid = changed.get("MainPID")
        if not pid or pid == self._pids.get(unit):
            return
        self._pids[unit] = pid
        print(f"streamy: {unit} started again", file=sys.stderr, flush=True)
        self.on_restart(unit)
