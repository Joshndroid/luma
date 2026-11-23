# SPDX-License-Identifier: Apache-2.0
"""The Hub's /api/sync/v1/ (website repo deploy/hub/connect-sync/API.md).

The device's Luma Connect registration (``~/.local/share/luma/connect/
device.json``, written at sign-in) supplies the Hub address and the bearer
token. Bodies and tokens are never logged.
"""
from __future__ import annotations

from typing import Callable

import json
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPErrorProcessor, HTTPRedirectHandler, OpenerDirector, Request, build_opener

PREFIX = "/api/sync/v1/"
MAX_RESPONSE = 32 * 1024 * 1024    # the largest answer read (a key rotation's zone list); more is refused (review L2)


class _NoRedirect(HTTPRedirectHandler):
    """A redirect is an error: the bearer token never follows one to another host or to plain http (review L3)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise HTTPError(req.full_url, code, "The Hub answered with a redirect, which Sync doesn't follow.", headers, fp)


_OPENER: OpenerDirector = build_opener(_NoRedirect, HTTPErrorProcessor)
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
TIMEOUT = 30
USER_AGENT = "luma-connect-sync/1"


class HubError(Exception):
    def __init__(self, status: int, message: str, code: str = "", body: dict | None = None, retry_after: int = 0) -> None:
        super().__init__(message)
        self.status, self.code, self.body, self.retry_after = status, code, body or {}, retry_after


class Offline(HubError):
    def __init__(self, message: str) -> None:
        super().__init__(0, message, "offline")


@dataclass
class Registration:
    device: str
    token: str
    hub: str
    name: str


def connect_directory() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return base / "luma" / "connect"


def load_registration(path: Path | None = None) -> Registration | None:
    path = path or connect_directory() / "device.json"
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if info.st_mode & 0o077 or path.parent.stat().st_mode & 0o077:
        raise HubError(0, f"{path} must be private to you (chmod 600, its folder 700).", "unsafe_registration")
    data = json.loads(path.read_text())
    hub = str(data.get("hub") or "").rstrip("/")
    parsed = urlparse(hub)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HubError(0, "The Luma Connect registration has no Hub address.", "no_hub")
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        raise HubError(0, "Refusing plain http to a Hub that is not on this machine.", "insecure_hub")
    if not data.get("device_id") or not data.get("token"):
        raise HubError(0, "The Luma Connect registration is incomplete. Sign in again.", "no_registration")
    return Registration(str(data["device_id"]), str(data["token"]), hub, str(data.get("name") or ""))


class Hub:
    def __init__(self, registration: Registration, timeout: float = TIMEOUT) -> None:
        self.registration = registration
        self.timeout = timeout
        self.requests = 0
        # Signs requests that change something with this device's key (set by Account); see crypto.request_message.
        self.signer: Callable[[bytes], bytes] | None = None

    @property
    def device(self) -> str:
        return self.registration.device

    def _send(self, method: str, path: str, data: bytes | None, content_type: str, timeout: float | None) -> tuple[int, bytes, str]:
        url = self.registration.hub + (path if path.startswith("/") else PREFIX + path)
        headers = {"Authorization": f"Bearer {self.registration.token}", "Accept": "application/json", "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = content_type
        relative = path if not path.startswith("/") else ""
        if method != "GET" and relative and not relative.startswith("devices/self") and self.signer is not None:
            from .crypto import b64, request_message
            at = int(time.time())
            headers["X-Luma-Sync-Time"] = str(at)
            headers["X-Luma-Sync-Signature"] = b64(self.signer(request_message(self.device, method, relative, at, data or b"")))
        request = Request(url, data=data, method=method, headers=headers)
        self.requests += 1
        try:
            with _OPENER.open(request, timeout=timeout or self.timeout) as response:
                body = response.read(MAX_RESPONSE + 1)
                if len(body) > MAX_RESPONSE:
                    raise HubError(0, "The Hub's answer was larger than Sync accepts.", "too_large")
                return response.status, body, response.headers.get("Content-Type", "")
        except HTTPError as error:
            raw = error.read(65536) if error.fp else b""
            try:
                body = json.loads(raw)
            except ValueError:
                body = {}
            message = str(body.get("error") or body.get("message") or f"The Hub answered {error.code}.")
            retry = int(error.headers.get("Retry-After") or body.get("retry_after") or 0) if error.code == 429 else 0
            raise HubError(error.code, message, str(body.get("code") or ""), body, retry) from None
        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as error:
            raise Offline(f"The Hub could not be reached ({getattr(error, 'reason', error)}).") from None

    def call(self, method: str, path: str, value: object | None = None, timeout: float | None = None) -> dict:
        data = None if value is None else json.dumps(value).encode()
        status, raw, _ = self._send(method, path, data, "application/json", timeout)
        return json.loads(raw) if raw else {}

    def get(self, path: str, timeout: float | None = None) -> dict:
        return self.call("GET", path, timeout=timeout)

    def post(self, path: str, value: object) -> dict:
        return self.call("POST", path, value)

    def put(self, path: str, value: object) -> dict:
        return self.call("PUT", path, value)

    def delete(self, path: str) -> dict:
        return self.call("DELETE", path)

    def put_blob(self, zone: str, blob: str, data: bytes) -> None:
        self._send("PUT", f"zones/{zone}/blobs/{blob}", data, "application/octet-stream", 120)

    def get_blob(self, zone: str, blob: str) -> bytes:
        return self._send("GET", f"zones/{zone}/blobs/{blob}", None, "", 120)[1]

    def wait(self, after: int, seconds: int) -> int:
        return int(self.get(f"wait?after={after}&timeout={seconds}", timeout=seconds + 30)["seq"])

    def revoke(self, device: str) -> dict:
        return self.call("POST", f"/api/hub/sync/devices/{device}/revoke", {})


def backoff(attempt: int) -> float:
    return min(300.0, 2.0 ** min(attempt, 8)) + (time.time() % 1)
