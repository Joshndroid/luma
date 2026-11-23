# SPDX-License-Identifier: Apache-2.0
"""luma-connect-sync: Sync from a terminal, for tests and support.

  status | devices | apps | restore | conflicts      what Sync knows, as JSON
  setup [--words-file PATH]                        turn Sync on; shows the recovery key once
  join [--wait SECONDS]                            ask to join; prints the code to compare
  join-confirm yes|no                              on the joining device: the other device showed the same code
  requests                                         other devices asking to join, with codes
  approve JOIN --code NNNNNN | deny JOIN           answer a request
  recover [--words-file PATH]                      join with the recovery key (stdin otherwise)
  revoke DEVICE [--new-recovery-key FILE]          remove a device and replace the keys (and the recovery key)
  recovery-key [--words-file PATH]                 show (or write) a recovery key that was replaced, and clear the notice
  app on|off APP | app delete APP                  per-app switch; delete its synced data
  sync [--app APP]                                 one cycle now
  daemon                                           run the service (luma-connect-syncd)

Key material is never printed, except the recovery words by ``setup`` when
no --words-file is given, which is the one time a person sees them.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import appdata, crypto
from .account import SyncStateError
from .hub import HubError
from .keystore import KeystoreError


def _print(value) -> None:
    print(json.dumps(value, indent=1, sort_keys=True))


def _private_write(path: Path, text: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="luma-connect-sync")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "devices", "apps", "restore", "conflicts", "requests", "daemon"):
        commands.add_parser(name)
    setup = commands.add_parser("setup")
    setup.add_argument("--words-file")
    join = commands.add_parser("join")
    join.add_argument("--wait", type=int, default=600)
    join.add_argument("--code-file", help="also write the code here when it appears (for scripted tests)")
    confirm = commands.add_parser("join-confirm")
    confirm.add_argument("answer", choices=["yes", "no"])
    shown_key = commands.add_parser("recovery-key")
    shown_key.add_argument("--words-file")
    approve = commands.add_parser("approve")
    approve.add_argument("join")
    approve.add_argument("--code", required=True)
    deny = commands.add_parser("deny")
    deny.add_argument("join")
    recover = commands.add_parser("recover")
    recover.add_argument("--words-file")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("device")
    revoke.add_argument("--new-recovery-key", metavar="WORDS_FILE",
                        help="also replace the recovery key (when the removed device held it); the new words go to this file")
    app = commands.add_parser("app")
    app.add_argument("action", choices=["on", "off", "delete"])
    app.add_argument("app")
    wipe = commands.add_parser("delete-account", help="delete everything synced, from the Hub (needs the recovery key)")
    wipe.add_argument("--words-file")
    once = commands.add_parser("sync")
    once.add_argument("--app")
    arguments = parser.parse_args(argv)

    if arguments.command == "daemon":
        from .service import main as service_main
        return service_main()

    from .sync import Sync
    try:
        sync = Sync()
        if arguments.command == "status":
            try:
                hub = sync.account.state()
            except HubError as error:
                if error.status != 401:
                    raise
                # This device's Connect credential was revoked: it no longer syncs; everything on it stays.
                _print({"state": "attention", "attention": {"reason": "revoked", "detail": "This device no longer syncs. Everything on it stays."},
                        "account": {"standing": "revoked"}})
                return 0
            mine = sync.account.me(hub) or {}
            bundle = sync.account.bundle()
            _print({**sync.state(), "account": {"epoch": hub["epoch"], "rekey": hub["rekey"], "standing": mine.get("state", "new"),
                                                "local_epoch": bundle.epoch if bundle else None}})
        elif arguments.command == "devices":
            _print(sync.devices())
        elif arguments.command == "apps":
            _print(sync.app_list())
        elif arguments.command == "restore":
            _print(sync.restore())
        elif arguments.command == "conflicts":
            _print(sync.local.get("conflicts", []))
        elif arguments.command == "requests":
            _print(sync.account.join_requests())
        elif arguments.command == "setup":
            with sync.exclusive():
                pending = sync.account.begin_setup()
                sync.account.finish_setup(pending, {i: pending.words[i - 1] for i in pending.check})
            if arguments.words_file:
                _private_write(Path(arguments.words_file), " ".join(pending.words) + "\n")
                print("Sync is on. The recovery key was written to the file you named.")
            else:
                print("Sync is on. Your recovery key (shown once; keep it somewhere safe):")
                for index in range(0, 24, 4):
                    print("  " + "  ".join(f"{index + i + 1:2d} {pending.words[index + i]:<9}" for i in range(4)))
        elif arguments.command == "join":
            with sync.exclusive():
                view = sync.account.request_join()
            deadline = time.monotonic() + arguments.wait
            shown = ""
            while time.monotonic() < deadline:
                with sync.exclusive():
                    view = sync.account.join_view()
                if view.state == "code" and view.code != shown:
                    shown = view.code
                    print(f"Code: {view.code[:3]} {view.code[3:]}  (approve on: {', '.join(a['name'] for a in view.approvers)}; "
                          f"this device's key: {view.fingerprint})", flush=True)
                    print("When the other device shows the same code, run: luma-connect-sync join-confirm yes", flush=True)
                    if arguments.code_file:
                        Path(arguments.code_file).write_text(f"{view.join} {view.code}\n")
                if view.state in {"approved", "denied", "expired", "tampered"}:
                    print({"approved": "Approved. This device now syncs.", "denied": "Not approved",
                           "expired": "The request lapsed. Ask again.",
                           "tampered": "Someone changed this request on the way. Don't approve it; ask again."}[view.state])
                    return 0 if view.state == "approved" else 1
                time.sleep(2)
            print("No device answered yet; the request stays open. Run join again to keep waiting.")
            return 1
        elif arguments.command == "join-confirm":
            with sync.exclusive():
                view = sync.account.confirm_join(arguments.answer == "yes")
            print("Confirmed; this device joins once it is approved." if view.state in {"confirmed", "approved"}
                  else "Stopped. Ask again from the start.")
            return 0 if arguments.answer == "yes" else 1
        elif arguments.command == "recovery-key":
            words = sync.account.recovery_words()
            if arguments.words_file:
                _private_write(Path(arguments.words_file), " ".join(words) + "\n")
                print("The recovery key was written to the file you named.")
            else:
                for index in range(0, 24, 4):
                    print("  " + "  ".join(f"{index + i + 1:2d} {words[index + i]:<9}" for i in range(4)))
            sync.local.delete("recovery_replaced")
        elif arguments.command == "approve":
            with sync.exclusive():
                sync.account.approve(arguments.join, arguments.code)
            print("Approved.")
        elif arguments.command == "deny":
            sync.account.deny(arguments.join)
            print("Not approved.")
        elif arguments.command == "recover":
            text = Path(arguments.words_file).read_text() if arguments.words_file else sys.stdin.read()
            with sync.exclusive():
                sync.account.join_with_recovery(crypto.normalise_words(text))
            print("Joined with the recovery key. This device now syncs.")
        elif arguments.command == "revoke":
            with sync.exclusive():
                epoch, words = sync.account.revoke(arguments.device, new_recovery=bool(arguments.new_recovery_key))
            print(f"Removed. Your other devices have new keys (epoch {epoch}).")
            if words:
                sync.local.delete("recovery_replaced")      # the person sees them here
            if words and arguments.new_recovery_key:
                _private_write(Path(arguments.new_recovery_key), " ".join(words) + "\n")
                print("The recovery key was replaced; the new one was written to the file you named.")
            elif words:
                print("That device held the recovery key, so it was replaced. Your new recovery key (shown once):")
                for index in range(0, 24, 4):
                    print("  " + "  ".join(f"{index + i + 1:2d} {words[index + i]:<9}" for i in range(4)))
        elif arguments.command == "app":
            if arguments.action == "delete":
                target = appdata.targets(sync.hub.device).get(arguments.app)
                if target is None:
                    raise SyncStateError("unknown_app", "That app isn't on this device.")
                with sync.exclusive():
                    sync.appdata.delete_synced(target)
            else:
                sync.appdata.set_info(arguments.app, enabled=arguments.action == "on")
            print("Done.")
        elif arguments.command == "delete-account":
            text = Path(arguments.words_file).read_text() if arguments.words_file else sys.stdin.read()
            with sync.exclusive():
                sync.account.delete_everything(crypto.normalise_words(text))
            print("Everything synced was deleted from the Hub. This device keeps its files.")
        elif arguments.command == "sync":
            _print(sync.cycle(only_app=arguments.app))
    except (SyncStateError, HubError, KeystoreError, crypto.CryptoError) as error:
        print(f"luma-connect-sync: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
