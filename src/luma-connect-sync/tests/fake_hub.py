# SPDX-License-Identifier: Apache-2.0
"""A local stand-in for the Hub's /api/sync/v1/ (website repo, deploy/hub/connect-sync).

It keeps the contract the devices rely on (versions and conflicts, the change
feed, epochs, envelopes, join steps, zone-key completeness, membership) and
stores only what it is sent, so tests can search everything it holds for a
canary. Rate limits are left out. Devices are enrolled with ``POST
/__test/devices`` {"account", "name"} -> {"device_id", "token"}.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Refused(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.status, self.code = status, code


class FakeHub:
    def __init__(self, quota: int = 1 << 40) -> None:
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.tokens: dict[str, dict] = {}          # token -> {device, account, name, revoked}
        self.accounts: dict[str, dict] = {}
        self.quota = quota
        self.requests = 0

    # -- storage helpers
    def acct(self, account: str) -> dict:
        return self.accounts.setdefault(account, {"seq": 0, "epoch": 0, "rekey": False, "recovery": None, "devices": {},
                                                  "envelopes": {}, "joins": {}, "zones": {}, "bytes": 0})

    def bump(self, a: dict) -> int:
        a["seq"] += 1
        self.changed.notify_all()
        return a["seq"]

    def everything(self) -> bytes:
        """Every byte the fake holds, for the canary search."""
        with self.lock:
            return json.dumps(self.accounts, default=lambda v: b64(v) if isinstance(v, bytes) else str(v)).encode()

    def member(self, a: dict, device: str) -> dict:
        row = a["devices"].get(device)
        if not row or row["state"] != "member":
            raise Refused(403, "not_member")
        return row

    # -- the routes
    def handle(self, method: str, path: str, query: dict, token: str, body: bytes):
        with self.lock:
            self.requests += 1
            if path == "/__test/devices" and method == "POST":
                data = json.loads(body)
                device, new = str(data.get("device_id") or __import__("uuid").uuid4()), secrets.token_urlsafe(32)
                self.tokens = {t: v for t, v in self.tokens.items() if v["device"] != device}
                self.tokens[new] = {"device": device, "account": data["account"], "name": data.get("name", ""), "revoked": False}
                return 201, {"device_id": device, "token": new}
            who = self.tokens.get(token)
            if not who or who["revoked"]:
                raise Refused(401, "unauthorized")
            a = self.acct(who["account"])
            device = who["device"]
            revoke = path.startswith("/api/hub/sync/devices/") and path.endswith("/revoke")
            if revoke:
                target = path.split("/")[5]
                for t in self.tokens.values():
                    if t["device"] == target and t["account"] == who["account"]:
                        t["revoked"] = True
                row = a["devices"].get(target)
                if row and row["state"] != "revoked":
                    if row["state"] == "member":
                        a["rekey"] = True
                    row["state"] = "revoked"
                    a["envelopes"].pop(target, None)
                    a["joins"] = {k: v for k, v in a["joins"].items() if v["device"] != target}
                    self.bump(a)
                return 200, {"revoked": True}
            if not path.startswith("/api/sync/v1/"):
                raise Refused(404, "not_found")
            rest = path[len("/api/sync/v1/"):]
            data = json.loads(body) if body and not re.fullmatch(r"zones/[0-9a-f]{32}/blobs/[0-9a-f]{64}", rest) else {}
            names = {t["device"]: t["name"] for t in self.tokens.values()}
            if rest == "account" and method == "GET":
                return 200, {"account": who["account"], "device": device, "seq": a["seq"], "epoch": a["epoch"], "rekey": a["rekey"],
                             "recovery": a["recovery"] is not None,
                             "devices": [{**{k: v for k, v in d.items()}, "name": names.get(d["device"], "")} for d in a["devices"].values()],
                             "usage": {"bytes": a["bytes"], "quota_bytes": self.quota}}
            if rest == "devices/self" and method == "PUT":
                message = f"luma-sync-device-v1\0{who['account']}\0{device}\0{data['box_pub']}".encode()
                try:
                    Ed25519PublicKey.from_public_bytes(unb64(data["sign_pub"])).verify(unb64(data["proof"]), message)
                except Exception:  # noqa: BLE001
                    raise Refused(400, "bad_proof") from None
                row = a["devices"].get(device)
                if row and row["state"] != "revoked":
                    if (row["sign_pub"], row["box_pub"]) != (data["sign_pub"], data["box_pub"]):
                        raise Refused(409, "keys_conflict")
                    row["class"] = data.get("class", "computer")
                    return 200, {"state": row["state"], "created": False}
                if row and (row["sign_pub"] == data["sign_pub"] or row["box_pub"] == data["box_pub"]):
                    raise Refused(409, "old_keys")
                a["devices"][device] = {"device": device, "sign_pub": data["sign_pub"], "box_pub": data["box_pub"], "state": "pending",
                                        "class": data.get("class", "computer"), "cert": None, "cert_epoch": None, "last_seen_at": int(time.time() * 1000),
                                        "joined_via": None, "joined_at": None, "was_revoked": bool(row)}
                self.bump(a)
                return 201, {"state": "pending", "created": True}
            if rest == "wait" and method == "GET":
                after, timeout = int(query.get("after", ["0"])[0]), int(query.get("timeout", ["0"])[0])
                end = time.monotonic() + timeout
                while a["seq"] <= after and time.monotonic() < end:
                    self.changed.wait(max(0.05, end - time.monotonic()))
                return 200, {"seq": a["seq"]}
            if rest == "keys/epoch" and method == "POST":
                if data["epoch"] != a["epoch"] + 1:
                    raise Refused(409, "epoch_conflict")
                me = a["devices"].get(device)
                if not me or me["state"] == "revoked" or (a["epoch"] and me["state"] != "member"):
                    raise Refused(403, "not_member")
                if a["epoch"] == 0 and not data.get("recovery"):
                    raise Refused(400, "fields")
                live = {d for d, row in a["devices"].items() if row["state"] != "revoked"}
                if any(e["device"] not in live for e in data["envelopes"]):
                    raise Refused(400, "fields")
                for zone_key in data.get("zone_keys", []):
                    zone = a["zones"].get(zone_key["zone"])
                    if zone is None:
                        raise Refused(400, "fields")
                    zone["keys"][zone_key["generation"]] = {"generation": zone_key["generation"], "epoch": data["epoch"], "envelope": zone_key["envelope"]}
                    zone["generation"] = max(zone["generation"], zone_key["generation"])
                if any(k["epoch"] != data["epoch"] for z in a["zones"].values() for k in z["keys"].values()):
                    raise Refused(400, "zone_keys_incomplete")
                wrapped = {e["device"] for e in data["envelopes"]}
                a["envelopes"] = {e["device"]: {"epoch": data["epoch"], "wrapper": device, "ciphertext": e["ciphertext"], "signature": e["signature"]}
                                  for e in data["envelopes"]}
                for d in live:
                    row = a["devices"][d]
                    if d in wrapped:
                        row.update(state="member", cert=data.get("certs", {}).get(d), cert_epoch=data["epoch"] if data.get("certs", {}).get(d) else None,
                                   joined_via=row.get("joined_via") or ("setup" if a["epoch"] == 0 else "approval"))
                    elif row["state"] == "member":
                        row.update(state="pending", cert=None, cert_epoch=None)
                if data.get("recovery"):
                    a["recovery"] = data["recovery"]
                a.update(epoch=data["epoch"], rekey=False, joins={})
                return 201, {"epoch": a["epoch"], "seq": self.bump(a)}
            if rest == "keys/envelope" and method == "POST":
                if data["epoch"] != a["epoch"]:
                    raise Refused(409, "epoch_conflict")
                target = data["device"]
                if target != device:
                    self.member(a, device)
                    join = a["joins"].get(data.get("join", ""))
                    if not join or join["device"] != target or join.get("answer"):
                        raise Refused(404, "join_unknown")
                    if not join.get("device_nonce"):
                        raise Refused(409, "join_incomplete")
                    del a["joins"][join["id"]]
                elif a["recovery"] is None:
                    raise Refused(403, "no_recovery")
                elif a["devices"][target].get("was_revoked"):
                    raise Refused(403, "approval_required")
                a["envelopes"][target] = {"epoch": data["epoch"], "wrapper": device, "ciphertext": data["ciphertext"], "signature": data["signature"]}
                a["devices"][target].update(state="member", cert=data["cert"], cert_epoch=data["epoch"],
                                            joined_via="recovery" if target == device else "approval", joined_at=int(time.time() * 1000))
                return 201, {"epoch": a["epoch"], "seq": self.bump(a)}
            if rest == "keys/envelope" and method == "GET":
                if device not in a["envelopes"]:
                    raise Refused(404, "no_envelope")
                return 200, a["envelopes"][device]
            if rest == "keys/recovery" and method == "GET":
                if a["recovery"] is None:
                    raise Refused(404, "no_recovery")
                return 200, {"epoch": a["epoch"], "ciphertext": a["recovery"]}
            if rest == "joins" and method == "POST":
                if not a["epoch"]:
                    raise Refused(409, "not_set_up")
                a["joins"] = {k: v for k, v in a["joins"].items() if v["device"] != device}
                join = {"id": secrets.token_urlsafe(16)[:22], "device": device, "commitment": data["commitment"], "approver": None,
                        "approver_nonce": None, "device_nonce": None, "answer": None, "answered_by": None, "created_at": int(time.time() * 1000)}
                a["joins"][join["id"]] = join
                self.bump(a)
                return 201, {"id": join["id"]}
            if rest == "joins" and method == "GET":
                me = a["devices"].get(device)
                rows = [j for j in a["joins"].values() if (me and me["state"] == "member") or j["device"] == device]
                return 200, {"joins": [{**j, "name": names.get(j["device"], ""), "sign_pub": a["devices"][j["device"]]["sign_pub"],
                                        "box_pub": a["devices"][j["device"]]["box_pub"], "class": a["devices"][j["device"]]["class"]} for j in rows]}
            if rest.startswith("joins/"):
                parts = rest.split("/")
                join = a["joins"].get(parts[1])
                if method == "DELETE":
                    if join and join["device"] == device:
                        del a["joins"][parts[1]]
                        self.bump(a)
                    return 200, {"deleted": bool(join)}
                if not join or join.get("answer"):
                    raise Refused(404, "join_unknown")
                if parts[2] == "nonce":
                    self.member(a, device)
                    if join["approver_nonce"] and join["approver_nonce"] != data["nonce"]:
                        raise Refused(409, "join_mismatch")
                    join.update(approver=join["approver"] or device, approver_nonce=data["nonce"])
                elif parts[2] == "reveal":
                    if join["device"] != device:
                        raise Refused(403, "not_yours")
                    if hashlib.sha256(b"luma-sync-join-commit-v1\0" + unb64(data["nonce"])).digest() != unb64(join["commitment"]):
                        raise Refused(400, "bad_reveal")
                    join["device_nonce"] = data["nonce"]
                elif parts[2] == "deny":
                    self.member(a, device)
                    join.update(answer="denied", answered_by=device)
                self.bump(a)
                return 200, {"id": join["id"]}
            # zones and below need membership
            self.member(a, device)
            if rest == "zones" and method == "GET":
                return 200, {"zones": [{"zone": z, "owner": v["owner"], "generation": v["generation"], "records": len(v["records"]),
                                        "bytes": 0, "seq": v["seq"], "keys": sorted(v["keys"].values(), key=lambda k: k["generation"])}
                                       for z, v in sorted(a["zones"].items())]}
            parts = rest.split("/")
            zone_id = parts[1]
            if len(parts) == 2 and method == "PUT":
                if a["rekey"]:
                    raise Refused(409, "rekey_pending")
                if zone_id in a["zones"]:
                    raise Refused(409, "zone_exists")
                a["zones"][zone_id] = {"owner": data.get("owner"), "generation": 1, "seq": self.bump(a), "records": {}, "blobs": {},
                                       "keys": {1: {"generation": 1, "epoch": data["epoch"], "envelope": data["envelope"]}}}
                return 201, {"zone": zone_id}
            zone = a["zones"].get(zone_id)
            if zone is None:
                raise Refused(404, "zone_unknown")
            if len(parts) == 2 and method == "DELETE":
                a["bytes"] -= sum(len(b) for b in zone["blobs"].values())
                del a["zones"][zone_id]
                self.bump(a)
                return 200, {"deleted": True}
            sub = "/".join(parts[2:])
            if sub == "changes":
                after, limit = int(query.get("after", ["0"])[0]), int(query.get("limit", ["200"])[0])
                rows = sorted((r for r in zone["records"].values() if r["seq"] > after), key=lambda r: r["seq"])
                more = len(rows) > limit
                rows = rows[:limit]
                return 200, {"records": rows, "seq": rows[-1]["seq"] if more else max(after, a["seq"]), "more": more}
            if sub == "records":
                if a["rekey"]:
                    raise Refused(409, "rekey_pending")
                saved, conflicts = [], []
                for record in data["records"]:
                    current = zone["records"].get(record["name"])
                    if (current["version"] if current else 0) != record["base"] or (not current and record.get("deleted")):
                        conflicts.append(current or {"name": record["name"], "version": 0, "deleted": True})
                        continue
                    row = {"name": record["name"], "version": record["base"] + 1, "seq": self.bump(a), "generation": record["generation"],
                           "deleted": bool(record.get("deleted")), "ciphertext": None if record.get("deleted") else record["ciphertext"],
                           "writer": device, "updated_at": int(time.time() * 1000)}
                    zone["records"][record["name"]] = row
                    saved.append({"name": row["name"], "version": row["version"], "seq": row["seq"]})
                return 200, {"saved": saved, "conflicts": conflicts}
            if sub == "blobs/check":
                return 200, {"missing": [i for i in data["ids"] if i not in zone["blobs"]]}
            if sub == "blobs/delete":
                for i in data["ids"]:
                    a["bytes"] -= len(zone["blobs"].pop(i, b""))
                return 200, {"deleted": len(data["ids"])}
            if sub == "blobs" and method == "GET":
                return 200, {"blobs": [{"id": i, "size": len(b)} for i, b in sorted(zone["blobs"].items())]}
            if sub.startswith("blobs/"):
                blob = sub.split("/")[1]
                if method == "PUT":
                    if a["bytes"] + len(body) > self.quota:
                        raise Refused(413, "quota")
                    if blob not in zone["blobs"]:
                        zone["blobs"][blob] = body
                        a["bytes"] += len(body)
                    return 201, {"stored": True}
                if blob not in zone["blobs"]:
                    raise Refused(404, "blob_unknown")
                return 200, zone["blobs"][blob]
            raise Refused(404, "not_found")


def serve(hub: FakeHub, port: int = 0) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            pass

        def _do(self) -> None:
            url = urlparse(self.path)
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            try:
                status, value = hub.handle(self.command, url.path, parse_qs(url.query), token, body)
            except Refused as error:
                status, value = error.status, {"error": str(error), "code": error.code}
            except Exception as error:  # noqa: BLE001 - a fake bug shows as a 500 with its reason
                import traceback
                traceback.print_exc()
                status, value = 500, {"error": repr(error), "code": "fake_hub_bug"}
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream" if isinstance(value, bytes) else "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_DELETE = _do

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
