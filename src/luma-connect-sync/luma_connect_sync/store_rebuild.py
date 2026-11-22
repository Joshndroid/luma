# SPDX-License-Identifier: Apache-2.0
"""Rebuild an ADR-052 store app from its record (stdin: the apps-zone value).

The same steps Valet takes for a package file (store_install.install), with
the package resolved by name from this device's own repositories for the
named distribution. Nothing from the record reaches the build but the app
id, the package name and the distribution's name (review H5): no repository
files, keys, extra packages or commands. Exit 3 means "not available for
this device".
"""
from __future__ import annotations

import json
import re
import sys

PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,127}")
APP_ID = re.compile(r"(deb|rpm)-[A-Za-z0-9._-]{1,120}")


def main() -> int:
    value = json.loads(sys.stdin.read())
    from luma_installer import capsule_identity, closure, closure_launcher, store_install
    from luma_installer.app_store import Store
    from luma_installer.desktop import read_record
    from luma_installer.errors import InstallerError

    application_id, package, userland, kind = str(value.get("id")), str(value.get("package")), str(value.get("userland")), str(value.get("format"))
    if value.get("needs_vendor_repository"):
        print("Not available for this device · it needs a vendor's repository")
        return 3
    if kind not in {"deb", "rpm"} or not APP_ID.fullmatch(application_id) or not PACKAGE.fullmatch(package):
        print("Not available for this device · the record is not one this device can rebuild")
        return 3
    if userland not in closure.USERLANDS or closure.USERLANDS[userland].family != kind:
        print(f"Not available for this device · {userland} isn't supported here")
        return 3
    spec = closure.Spec(app_id=application_id, kind=kind, userland=userland, package=package, from_repository=True)
    store = Store()
    try:
        existing = read_record(application_id)
    except InstallerError:
        existing = {}
    sha256 = str(value.get("sha256") or application_id)
    identity = capsule_identity.ensure_level({"application_id": application_id, "sha256": sha256})
    try:
        built = store_install.build_tree(store, application_id, kind, spec=spec, record=identity)
    except store_install.Fallback as error:
        print(f"Not available for this device · {error.reason}")
        return 3
    exports = dict(dict(built["tree"]).get("exports") or {})
    record = store_install.described(application_id, kind, built, sha256, fallback_name=str(value.get("name") or application_id),
                                     kept={key: identity[key] for key in ("mcs_level", "keyring") if key in identity})
    record["comment"] = str(exports.get("comment") or "")
    record["synced_from"] = str(value.get("from") or "")
    if existing.get("data_home"):
        record["data_home"] = existing["data_home"]
    checkout, _tree = closure_launcher.deployment(store, application_id)
    store_install.publish(application_id, record, checkout, str(exports.get("icon") or ""))
    print(record["name"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
