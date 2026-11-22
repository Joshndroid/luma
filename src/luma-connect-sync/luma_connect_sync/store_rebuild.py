# SPDX-License-Identifier: Apache-2.0
"""Rebuild an ADR-052 store app from its record (stdin: the apps-zone value).

The same steps Valet takes for a package file (store_install.install), with
the package resolved by name from its distribution's repositories and the
ones it added for itself. Exit 3 means "not available for this device".
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    value = json.loads(sys.stdin.read())
    from luma_installer import capsule_identity, closure, closure_launcher, store_install
    from luma_installer.app_store import Store
    from luma_installer.desktop import read_record
    from luma_installer.errors import InstallerError

    tree = dict(value["tree"])
    application_id = str(value["id"])
    kind = str(tree["format"])
    if str(tree.get("userland")) not in closure.USERLANDS:
        print(f"Not available for this device · {tree.get('userland')} isn't supported here")
        return 3
    tree["id"] = application_id
    tree["architecture"] = closure.host_architecture()
    spec = closure.spec_from_record(tree)
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
