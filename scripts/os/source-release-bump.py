#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""A package's release moves with its source.

A change to what a package is built from that leaves its Release alone never
ships: the pinned RPM of the old release stays in the image and nothing
rebuilds it. 829ce7b7 changed src/luma-platform and left
luma-developer-platform at the release already pinned.

  source-release-bump.py check --base REV [--head REV]

Packages and their sources:
- every patch series, patches/<package>/ with a 0000-luma-fedora-spec.patch:
  its files; the release is that patch's "+Release:" line;
- every config/os/desktop-build-groups.json group with a spec: its paths; the
  release is the spec's Release line;
- luma-developer-platform: src/luma-platform/.
Changes to Markdown files and to tests need no release. Each finding is one
"FAIL ..." or "PASS ..." line; the exit status is 1 on any failure.
"""

import argparse
import json
import re
import subprocess
import sys

EXTRA = {"luma-developer-platform": (["src/luma-platform/"], "packaging/rpm/luma-developer-platform.spec")}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def show(rev: str, path: str) -> str | None:
    result = subprocess.run(["git", "show", f"{rev}:{path}"], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else None


def release(text: str | None, series: bool) -> str | None:
    if text is None:
        return None
    pattern = r"^\+Release:\s*(\S+)" if series else r"^Release:\s*(\S+)"
    match = re.search(pattern, text, re.M)
    return match.group(1) if match else None


def packages(rev: str) -> dict[str, tuple[list[str], str, bool]]:
    found = {}
    for path in git("ls-tree", "-r", "--name-only", rev, "patches/").splitlines():
        parts = path.split("/")
        if len(parts) == 3 and parts[2] == "0000-luma-fedora-spec.patch":
            found[parts[1]] = ([f"patches/{parts[1]}/"], path, True)
    groups = show(rev, "config/os/desktop-build-groups.json")
    for name, group in (json.loads(groups)["groups"].items() if groups else []):
        specs = [p for p in group.get("paths", []) if re.fullmatch(r"packaging/rpm/[^/]+\.spec", p)]
        spec = group.get("spec") or (specs[0] if specs else None)
        if spec and not any(p.startswith("patches/") for p in group.get("paths", [])):
            found.setdefault(name, (group["paths"], spec, False))
    for name, (paths, spec) in EXTRA.items():
        found[name] = (paths, spec, False)
    return found


def needs_release(path: str) -> bool:
    if path.endswith(".md"):
        return False
    return not (path.startswith("tests/") or "/tests/" in path)


def check(base: str, head: str) -> int:
    changed = [p for p in git("diff", "--name-only", f"{base}...{head}").splitlines() if needs_release(p)]
    failures = 0
    for name, (paths, release_file, series) in sorted(packages(head).items()):
        sources = [p for p in changed if p != release_file and
                   any(p == s or (s.endswith("/") and p.startswith(s)) for s in paths)]
        if not sources:
            continue
        before = release(show(base, release_file), series)
        after = release(show(head, release_file), series)
        if before == after:
            failures += 1
            print(f"FAIL {name}: {len(sources)} source file(s) changed ({sources[0]}) "
                  f"but {release_file} is still at {after}")
        else:
            print(f"PASS {name}: {before} -> {after}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check")
    c.add_argument("--base", required=True)
    c.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    return check(args.base, args.head)


if __name__ == "__main__":
    sys.exit(main())
