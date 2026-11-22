# SPDX-License-Identifier: Apache-2.0
"""A package's own udev rules, as a device request (ADR-052, Permissions 5).

Luma never installs an application's udev rules into the system. Read from
the package, they say which hardware the application exists to talk to:
controllers, HID devices, serial ports, lighting, USB. That becomes one
request on the install sheet ("Its package's device rules ask for direct
access to N kinds of device"); granting it makes exactly those device nodes
present in the application's sandbox, and nothing else of ``/dev``.

Camera, microphone, location and screen never come this way: they go through
the portals.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

RULE_FOLDERS = ("lib/udev/rules.d", "usr/lib/udev/rules.d", "etc/udev/rules.d")

#: kind -> (plain name, Lucide icon)
KINDS = {
    "controller": ("Game controllers", "gamepad-2"),
    "hid": ("Keyboards, mice and other input devices", "usb"),
    "serial": ("Serial ports", "cable"),
    "lighting": ("Lighting", "lightbulb"),
    "usb": ("USB devices", "usb"),
}

_MATCH = re.compile(r'(ATTRS?|ENV)\{([A-Za-z_]+)\}\s*==\s*"([^"]*)"|(SUBSYSTEMS?|KERNEL|KERNELS)\s*==\s*"([^"]*)"')
_HEX = re.compile(r"[0-9a-fA-F]{4}\Z")


@dataclass(frozen=True)
class Request:
    kind: str
    vendor: str = ""
    product: str = ""
    rule: str = ""

    def token(self) -> str:
        return ":".join(part for part in (self.kind, self.vendor, self.product) if part) if self.vendor else self.kind


@dataclass
class Summary:
    requests: list[Request] = field(default_factory=list)

    @property
    def kinds(self) -> list[str]:
        return list(dict.fromkeys(request.kind for request in self.requests))

    def name(self) -> str:
        kinds = self.kinds
        if not kinds:
            return ""
        if set(kinds) <= {"lighting", "hid", "usb"} and "lighting" in kinds:
            return "USB lighting and keyboards" if "hid" in kinds else "USB lighting"
        if len(kinds) == 1:
            return KINDS[kinds[0]][0]
        names = [KINDS[kind][0] for kind in kinds]
        return names[0] + " and " + names[1].lower() + ("" if len(names) == 2 else " and more")

    def icon(self) -> str:
        kinds = self.kinds
        for kind in ("lighting", "controller", "serial", "hid", "usb"):
            if kind in kinds:
                return KINDS[kind][1]
        return "usb"

    def as_dict(self) -> dict[str, object]:
        return {"name": self.name(), "icon": self.icon(), "kinds": self.kinds, "count": len(self.kinds),
                "requests": list(dict.fromkeys(request.token() for request in self.requests)), "default": bool(self.requests)}


_LIGHTING_WORDS = ("rgb", "led", "light", "aura", "chroma", "openrgb", "razer", "corsair", "steelseries")


def classify(line: str, rule_name: str = "") -> Request | None:
    """One udev rule line as a request, or None when it asks for no device access."""
    text = line.split("#", 1)[0].strip()
    if not text or not re.search(r'MODE\s*:?=\s*"0?6[0-7][0-7]"|TAG\+=\s*"uaccess"|GROUP\s*:?=\s*"|OWNER\s*:?=\s*"', text):
        return None
    subsystems, kernels, vendor, product, environment = [], [], "", "", {}
    for match in _MATCH.finditer(text):
        if match.group(1):
            key, value = match.group(2), match.group(3)
            if match.group(1).startswith("ATTR"):
                if key == "idVendor" and _HEX.match(value):
                    vendor = value.lower()
                elif key == "idProduct" and _HEX.match(value):
                    product = value.lower()
            else:
                environment[key] = value
        else:
            (subsystems if match.group(4).startswith("SUBSYSTEM") else kernels).append(match.group(5))
    lowered = (rule_name + " " + text).lower()
    if "i2c-dev" in subsystems or "leds" in subsystems or any(word in lowered for word in _LIGHTING_WORDS) and (
            "hidraw" in subsystems or "usb" in subsystems or "i2c" in lowered):
        kind = "lighting"
    elif "hidraw" in subsystems or any(kernel.startswith("hidraw") for kernel in kernels):
        kind = "hid"
    elif {"input", "joystick"} & set(subsystems) or "ID_INPUT_JOYSTICK" in environment or any(
            kernel.startswith(("js", "uinput", "event")) for kernel in kernels) or "uinput" in subsystems or "misc" in subsystems and "uinput" in lowered:
        kind = "controller"
    elif {"tty", "usb-serial"} & set(subsystems) or any(kernel.startswith(("ttyUSB", "ttyACM", "ttyS")) for kernel in kernels):
        kind = "serial"
    elif "usb" in subsystems or vendor:
        kind = "usb"
    else:
        return None
    return Request(kind, vendor, product, rule_name)


def read_rules(files: dict[str, str]) -> Summary:
    """``files``: rule file name -> contents."""
    summary = Summary()
    for name, contents in sorted(files.items()):
        continued = ""
        for raw in contents.splitlines():
            if raw.rstrip().endswith("\\"):
                continued += raw.rstrip()[:-1] + " "
                continue
            request = classify(continued + raw, name)
            continued = ""
            if request is not None and request not in summary.requests:
                summary.requests.append(request)
    return summary


def from_tree(root: Path) -> Summary:
    files: dict[str, str] = {}
    for folder in RULE_FOLDERS:
        directory = root / folder
        if not directory.is_dir() or directory.is_symlink():
            continue
        for path in sorted(directory.glob("*.rules")):
            try:
                if path.is_file() and not path.is_symlink() and path.stat().st_size < 256 * 1024:
                    files[f"{folder}/{path.name}"] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return read_rules(files)


def parse_token(token: str) -> Request:
    parts = token.split(":")
    return Request(parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")


# -- the device nodes a grant makes present ------------------------------------

def _uevent(path: Path) -> dict[str, str]:
    values = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            key, _, value = line.partition("=")
            values[key] = value
    except OSError:
        pass
    return values


def _usb_ids(device: Path) -> tuple[str, str]:
    """The vendor and product of the USB (or HID) device a class device belongs to."""
    current = device.resolve()
    while current != current.parent:
        vendor, product = current / "idVendor", current / "idProduct"
        if vendor.is_file() and product.is_file():
            return vendor.read_text().strip().lower(), product.read_text().strip().lower()
        hid = _uevent(current / "uevent").get("HID_ID")
        if hid:
            _bus, vendor_id, product_id = (hid.split(":") + ["", "", ""])[:3]
            return vendor_id[-4:].lower(), product_id[-4:].lower()
        current = current.parent
    return "", ""


def _matches(request: Request, device: Path) -> bool:
    if not request.vendor:
        return True
    vendor, product = _usb_ids(device)
    return vendor == request.vendor and (not request.product or product == request.product)


def nodes(requests: list[Request], sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[str]:
    """Device nodes that exist now for granted requests."""
    found: list[str] = []

    def add(name: str) -> None:
        node = dev_root / name
        if node.exists() and str(node) not in found:
            found.append(str(node))

    for request in requests:
        if request.kind in {"hid", "lighting"}:
            for device in sorted((sys_root / "class/hidraw").glob("hidraw*")):
                if _matches(request, device / "device"):
                    add(device.name)
        if request.kind == "lighting":
            for device in sorted((sys_root / "class/i2c-dev").glob("i2c-*")):
                add(device.name)
        if request.kind == "serial":
            for device in sorted((sys_root / "class/tty").glob("tty*")):
                if device.name.startswith(("ttyUSB", "ttyACM")) and _matches(request, device / "device"):
                    add(device.name)
        if request.kind == "controller":
            for device in sorted((sys_root / "class/input").glob("event*")) + sorted((sys_root / "class/input").glob("js*")):
                udev = Path("/run/udev/data") / f"c{_uevent(device / 'uevent').get('MAJOR', '13')}:{_uevent(device / 'uevent').get('MINOR', '')}"
                joystick = "E:ID_INPUT_JOYSTICK=1" in (udev.read_text(errors="replace") if udev.is_file() else "")
                if (joystick or request.vendor) and _matches(request, device / "device"):
                    add(f"input/{device.name}")
            if not request.vendor and (dev_root / "uinput").exists():
                add("uinput")
        if request.kind == "usb":
            for device in sorted((sys_root / "bus/usb/devices").glob("*")):
                values = _uevent(device / "uevent")
                if values.get("DEVTYPE") == "usb_device" and values.get("DEVNAME") and _matches(request, device):
                    add(values["DEVNAME"])
    return found


def access_rules(grants: list[Request]) -> str:
    """udev rules giving the signed-in person access to granted devices (uaccess)."""
    lines = ["# Written by Luma for applications whose device requests were granted."]
    for request in grants:
        vendor, product = request.vendor.upper(), (request.product.upper() or "*")
        if request.kind in {"hid", "lighting"}:
            if not request.vendor:
                continue
            # The HID device's name carries its bus, vendor and product: USB and virtual alike.
            lines.append(f'SUBSYSTEM=="hidraw", KERNELS=="*:{vendor}:{product}.*", TAG+="uaccess"')
            continue
        ids = ""
        if request.vendor:
            ids = f', ATTRS{{idVendor}}=="{request.vendor}"' + (f', ATTRS{{idProduct}}=="{request.product}"' if request.product else "")
        elif request.kind == "usb":
            continue
        subsystem = {"serial": "tty", "controller": "input", "usb": "usb"}[request.kind]
        if request.kind == "controller" and not ids:
            ids = ', ENV{ID_INPUT_JOYSTICK}=="1"'
        if request.kind == "serial":
            ids = ', KERNEL=="ttyUSB[0-9]*|ttyACM[0-9]*"' + ids
        lines.append(f'SUBSYSTEM=="{subsystem}"{ids}, TAG+="uaccess"')
    return "\n".join(lines) + "\n"


ACCESS_RULES = Path("/etc/udev/rules.d/70-luma-app-devices.rules")
_TOKEN = re.compile(r"(controller|hid|serial|lighting|usb)(:[0-9a-f]{4}(:[0-9a-f]{4})?)?\Z")


def write_access_rules(tokens: list[str], path: Path = ACCESS_RULES, trigger: bool = True) -> None:
    """Root only (luma-installer-system device-access): the rules for every granted request."""
    import subprocess
    if any(not _TOKEN.fullmatch(token) for token in tokens):
        raise ValueError("a device request is not one Luma knows")
    text = access_rules([parse_token(token) for token in dict.fromkeys(tokens)])
    temporary = path.with_suffix(".partial")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary.write_text(text)
    temporary.chmod(0o644)
    temporary.replace(path)
    if trigger:
        subprocess.run(["udevadm", "control", "--reload"], check=False, timeout=30)
        for subsystem in ("hidraw", "tty", "input", "usb", "i2c-dev"):
            subprocess.run(["udevadm", "trigger", "--action=change", f"--subsystem-match={subsystem}"], check=False, timeout=60)


def device_arguments(requests: list[Request]) -> list[str]:
    arguments: list[str] = []
    for node in nodes(requests):
        arguments.extend(("--dev-bind", node, node))
    return arguments

