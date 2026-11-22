# SPDX-License-Identifier: Apache-2.0
"""See an application's refused folder access without blocking it (ADR-052, Permissions 3).

A folder an application may not use is covered by an empty folder of type
``luma_application.refused`` (``permission_views``). The kernel refuses every
read, entry or write there with EACCES at once, in SELinux, and logs an AVC
naming the folder (``name="Documents"``) and the application (the category
pair in ``scontext``). Nothing waits for an answer: the refusal is complete
before anyone hears of it.

This service (``luma-refusal-watch.service``, root, CAP_AUDIT_READ only)
reads AVCs from the kernel's audit multicast group, keeps those on the stub
type, and hands each one to the person's session (the socket
``AppPermissions1`` listens on), which names the application and offers the
folder once. It reads only what the kernel already logs, so it works with or
without auditd, and sees nothing of an application but the AVC.
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import sys
import time
from pathlib import Path

NETLINK_AUDIT = 9
AUDIT_NLGRP_READLOG = 1
AUDIT_AVC = 1400
STUB_TYPE = "luma_application.refused"

_FIELD = re.compile(r'(\w+)=("([^"]*)"|\S+)')


def parse(text: str) -> dict[str, str] | None:
    """An AVC on a refusal stub as {pid, folder, level, permissions}; None for anything else."""
    if "avc:" not in text or "denied" not in text or f":{STUB_TYPE}:" not in text:
        return None
    fields = {match.group(1): (match.group(3) if match.group(3) is not None else match.group(2))
              for match in _FIELD.finditer(text)}
    scontext, tcontext = fields.get("scontext", ""), fields.get("tcontext", "")
    if ":luma_application.process:" not in scontext or f":{STUB_TYPE}:" not in tcontext:
        return None
    if fields.get("tclass") != "dir":
        return None
    name = fields.get("name", "")
    if not name or name.startswith(".") or "/" in name:
        return None
    permissions = text.split("{", 1)[1].split("}", 1)[0].split() if "{" in text else []
    return {"pid": fields.get("pid", ""), "folder": name, "level": scontext.split(":luma_application.process:", 1)[1],
            "permissions": " ".join(permissions)}


def owner(pid: str) -> int | None:
    try:
        for line in Path(f"/proc/{int(pid)}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def deliver(uid: int, event: dict[str, str]) -> bool:
    path = Path(f"/run/user/{uid}/luma-permissions/refusals.sock")
    try:
        info = path.lstat()
    except OSError:
        return False
    if info.st_uid != uid:
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(2)
        try:
            connection.connect(str(path))
            connection.sendall((json.dumps(event) + "\n").encode())
        except OSError:
            return False
    return True


def records(sock: socket.socket):
    while True:
        data = sock.recv(65536)
        offset = 0
        while offset + 16 <= len(data):
            length, kind, _flags, _seq, _pid = struct.unpack_from("=IHHII", data, offset)
            if length < 16:
                break
            payload = data[offset + 16:offset + length]
            offset += (length + 3) & ~3
            if kind == AUDIT_AVC:
                yield payload.split(b"\0", 1)[0].decode("utf-8", "replace")


def main(argv: list[str] | None = None) -> int:
    sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_AUDIT)
    sock.bind((0, 1 << (AUDIT_NLGRP_READLOG - 1)))
    seen: dict[tuple[str, str], float] = {}
    for text in records(sock):
        event = parse(text)
        if event is None:
            continue
        key = (event["level"], event["folder"])
        now = time.monotonic()
        # The session offers once; this only keeps a burst of probes from
        # becoming a burst of messages.
        if now - seen.get(key, -60.0) < 5:
            continue
        uid = owner(event["pid"])
        if uid is None or uid < 1000:
            continue
        if deliver(uid, {**event, "time": str(int(time.time()))}):
            seen[key] = now
        if len(seen) > 4096:
            seen.clear()
    return 0


if __name__ == "__main__":
    sys.exit(main())
