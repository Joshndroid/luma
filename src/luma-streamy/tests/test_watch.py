# SPDX-License-Identifier: Apache-2.0
"""The camera and sound device lists across a service that goes away and comes back, without PipeWire:
a scripted service whose connections die (silently, or closing their socket) when it restarts, and
whose nodes come back under new serials. Nothing is declared gone from a connection that died, and
the list follows the new nodes within a couple of seconds."""
from __future__ import annotations

import os
import queue
import socket
import threading
import time
import unittest

import gi

try:
    gi.require_version("Gst", "1.0")
except ValueError:
    raise unittest.SkipTest("GStreamer isn't installed")
from gi.repository import GLib, Gst  # noqa: E402

from luma_streamy import devices, portals  # noqa: E402
from luma_streamy.devices import AudioDevices, Device  # noqa: E402
from luma_streamy.portals import CameraWatch  # noqa: E402

Gst.init(None)

NAME = "v4l2_input.pci-0000_3c_00.0-usb-0_1.1_1.0"


def pump(until, seconds: float) -> bool:
    """Run the main loop until ``until()`` or ``seconds`` passed; whether it came true."""
    context = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if until():
            return True
        context.iteration(False)
        time.sleep(0.005)
    return until()


class Message:
    def __init__(self, kind):
        self.type = kind


class Bus:
    def __init__(self):
        self.queue: queue.Queue = queue.Queue()

    def timed_pop(self, timeout):
        try:
            return self.queue.get(timeout=timeout / Gst.SECOND)
        except queue.Empty:
            return None


class CameraDevice:
    def __init__(self, name: str, serial: int):
        self.props = Gst.Structure.new_from_string(
            f'props, object.serial=(string)"{serial}", node.name=(string)"{name}", '
            f'node.description=(string)"DJIPocket3 (V4L2)", device.api=(string)v4l2')

    def get_device_class(self):
        return "Video/Source"

    def get_properties(self):
        return self.props

    def get_display_name(self):
        return "DJIPocket3"

    def get_caps(self):
        return Gst.Caps.from_string("image/jpeg,width=1920,height=1080,framerate=30/1")


class Connection:
    """One client of the service: it sees the nodes and their changes until it dies."""

    def __init__(self, service: "Service", sock: socket.socket):
        self.service, self.sock, self.bus = service, sock, Bus()
        self.dead = False
        self.frozen: list | None = None
        self.stopped = False
        self.fd = -1  # the client's end, which the provider owns

    def get_devices(self):
        nodes = self.frozen if self.frozen is not None else list(self.service.nodes.items())
        return [CameraDevice(name, serial) for name, serial in nodes]

    def get_bus(self):
        return self.bus

    def stop(self):
        self.stopped = True  # like GStreamer's, it leaves the fd to the caller


class Service:
    """The camera service: nodes by name -> serial; every connection opened on it."""

    def __init__(self):
        self.nodes = {NAME: 1}
        self.serial = 1
        self.connections: list[Connection] = []
        self.by_fd: dict[int, Connection] = {}
        self.hold = threading.Event()
        self.hold.set()

    def open_remote(self, done):
        ours, theirs = socket.socketpair()
        connection = Connection(self, ours)
        self.connections.append(connection)
        fd = connection.fd = theirs.detach()
        self.by_fd[fd] = connection
        GLib.idle_add(lambda: done(fd) and False)

    def provider(self, fd: int):
        """What the device provider does with the fd: connect, list (a gated client waits here)."""
        connection = self.by_fd.pop(fd)
        self.hold.wait()
        return connection

    def change(self):
        for connection in self.connections:
            if not connection.dead:
                connection.bus.queue.put(Message(Gst.MessageType.DEVICE_ADDED))

    def unplug(self):
        self.nodes.pop(NAME, None)
        self.change()

    def plug(self):
        self.serial += 1
        self.nodes[NAME] = self.serial
        self.change()

    def restart(self, *, hang_up: bool, last_seen: list | None):
        """Every connection dies: it keeps ``last_seen`` (None: what it had) and hears nothing more; with
        ``hang_up`` its socket closes. The nodes come back under new serials."""
        for connection in self.connections:
            if connection.dead:
                continue
            connection.frozen = list(self.nodes.items()) if last_seen is None else last_seen
            if last_seen is not None:
                connection.bus.queue.put(Message(Gst.MessageType.DEVICE_REMOVED))
            connection.dead = True
            if hang_up:
                connection.sock.close()
        self.serial += 1
        self.nodes = {NAME: self.serial}


class CameraWatchTest(unittest.TestCase):
    def setUp(self):
        self.saved = (portals.CONFIRM_S, portals.READY_S, portals.RECHECK_S)
        portals.CONFIRM_S, portals.READY_S, portals.RECHECK_S = 0.4, 0.6, 1.0
        self.service = Service()
        self.missing_seen: list[bool] = []
        self.watch = CameraWatch(self.service.open_remote, self.changed, provider=self.service.provider)
        self.assertTrue(pump(lambda: self.watch.ready, 2), "no first list")
        self.assertEqual(self.target(), "1")

    def tearDown(self):
        self.watch.close()
        for connection in self.service.connections:
            connection.sock.close()
        portals.CONFIRM_S, portals.READY_S, portals.RECHECK_S = self.saved

    def changed(self):
        # What the window does on every change: is the chosen camera unplugged?
        self.missing_seen.append(self.watch.missing(NAME))

    def target(self):
        camera = self.watch.find(NAME)
        return camera.target if camera is not None else None

    def test_a_connection_that_died_with_the_service_never_says_unplugged(self):
        # It saw the camera go and nothing after: the list it keeps lacks the camera for good.
        self.service.restart(hang_up=False, last_seen=[])
        self.assertTrue(pump(lambda: self.target() == "2", 3), f"still {self.target()}")
        self.assertNotIn(True, self.missing_seen)
        self.assertTrue(pump(lambda: self.service.connections[0].stopped, 1), "the dead connection was kept")

    def test_a_connection_the_service_closed_is_replaced(self):
        self.service.restart(hang_up=True, last_seen=None)
        self.assertTrue(pump(lambda: self.target() == "2", 2), f"still {self.target()}")
        self.assertNotIn(True, self.missing_seen)

    def test_a_service_restart_lists_again(self):
        # It died keeping the old node, which is gone: only the restart says so.
        self.service.restart(hang_up=False, last_seen=None)
        self.assertEqual(self.target(), "1")
        self.watch.reopen("wireplumber.service started again")
        self.assertTrue(pump(lambda: self.target() == "2", 2), f"still {self.target()}")

    def test_an_unplugged_camera_is_unplugged_once_confirmed_and_back_when_plugged(self):
        self.service.unplug()
        self.assertTrue(pump(lambda: True in self.missing_seen, 3), "never unplugged")
        self.assertEqual(self.missing_seen[0], False, "unplugged before a fresh connection said so")
        self.service.plug()
        self.assertTrue(pump(lambda: self.target() == "2", 2), "plugged in again, not seen")
        self.assertFalse(self.watch.missing(NAME))

    def test_a_camera_plugged_while_the_connection_was_dead_is_found(self):
        self.service.unplug()
        self.assertTrue(pump(lambda: True in self.missing_seen, 3), "never unplugged")
        self.service.restart(hang_up=False, last_seen=[])  # the live connection dies silently too
        self.assertTrue(pump(lambda: self.target() == "2", 5), "the camera back was never seen")

    def test_a_connection_that_never_lists_is_given_up(self):
        self.service.hold.clear()  # the service doesn't let the next client in
        self.watch.reopen("check")
        pump(lambda: False, portals.READY_S + 0.2)  # given up: the old list stays meanwhile
        self.assertEqual(self.target(), "1")
        self.service.restart(hang_up=False, last_seen=None)
        self.service.hold.set()  # the late client lists now, but it isn't wanted any more; a new one is
        self.assertTrue(pump(lambda: self.target() == "2", 3), f"still {self.target()}")


class Monitor:
    """A device monitor on a sound service: what it lists, frozen when its connection died."""

    def __init__(self, service: "SoundService"):
        self.service, self.bus, self.frozen = service, Bus(), None

    def get_bus(self):
        return self.bus

    def get_devices(self):
        return list(self.frozen if self.frozen is not None else self.service.devices)

    def stop(self):
        pass


class SoundService:
    def __init__(self):
        self.mic = Device("alsa_input.usb-pocket", "Pocket Mic", "source")
        self.devices = [Device("alsa_input.builtin", "Built-in Microphone", "source", default=True), self.mic]
        self.monitors: list[Monitor] = []

    def monitor(self):
        monitor = Monitor(self)
        self.monitors.append(monitor)
        return monitor


class AudioDevicesTest(unittest.TestCase):
    def setUp(self):
        self.saved = devices.CONFIRM_S
        devices.CONFIRM_S = 0.4
        self.service = SoundService()
        self.returned: list[set] = []
        self.missing_seen: list[bool] = []
        self.list = AudioDevices(on_changed=self.changed, monitor=self.service.monitor)
        self.assertTrue(pump(lambda: self.list.ready, 2))

    def tearDown(self):
        self.list._generation += 1
        devices.CONFIRM_S = self.saved

    def changed(self):
        self.returned.append(set(self.list.returned))
        self.missing_seen.append(self.list.missing("source", self.service.mic.name))

    def test_a_dead_monitor_never_says_unplugged_and_the_device_comes_back(self):
        old = self.service.monitors[-1]
        old.frozen = self.service.devices[:1]  # it saw the mic go when the service stopped, then nothing
        old.bus.queue.put(Message(Gst.MessageType.DEVICE_REMOVED))
        self.assertTrue(pump(lambda: any(self.service.mic.name in r for r in self.returned), 3), self.returned)
        self.assertNotIn(True, self.missing_seen)

    def test_an_unplugged_microphone_is_unplugged_once_confirmed(self):
        self.service.devices = self.service.devices[:1]
        self.service.monitors[-1].bus.queue.put(Message(Gst.MessageType.DEVICE_REMOVED))
        self.assertTrue(pump(lambda: True in self.missing_seen, 3), "never unplugged")
        self.assertEqual(self.missing_seen[0], False, "unplugged before a fresh monitor said so")


if __name__ == "__main__":
    os.environ.setdefault("G_DEBUG", "")
    unittest.main()
