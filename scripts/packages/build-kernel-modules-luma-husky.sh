#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Build kernel-modules-luma-husky from a Pixel 8 Pro boot image build: the
# directory scripts/mobile/husky/build-boot.sh --out wrote, which holds
# luma-husky-boot.img and modules/lib/modules/<release> (the stripped,
# signed modules_install tree of the same kernel build). The package holds
# /usr/lib/modules/<release> without the build and source links. It carries no
# boot image (see packaging/rpm/kernel-modules-luma-husky.spec).
#
#   build-kernel-modules-luma-husky.sh --build BUILD_BOOT_OUT_DIR

set -euo pipefail
repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
usage='usage: build-kernel-modules-luma-husky.sh --build BUILD_BOOT_OUT_DIR'
[ "${1:-}" = --build ] && [ -n "${2:-}" ] || { echo "$usage" >&2; exit 2; }
build=$(realpath -e "$2")
spec="$repo_root/packaging/rpm/kernel-modules-luma-husky.spec"
kver=$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["kernel"]["release"])' \
  "$repo_root/devices/google/husky/device.toml")
grep -q "^%global kver $kver\$" "$spec" ||
  { echo "error: the spec's kver is not device.toml's kernel release $kver" >&2; exit 1; }
tree="$build/modules/lib/modules/$kver"
[ -f "$build/luma-husky-boot.img" ] ||
  { echo "error: $build has no luma-husky-boot.img (build it with scripts/mobile/husky/build-boot.sh)" >&2; exit 1; }
[ -f "$tree/modules.dep" ] || { echo "error: $build has no modules/lib/modules/$kver" >&2; exit 1; }

output_dir="$repo_root/build/packages/kernel-modules-luma-husky"
mkdir -p "$(dirname "$output_dir")"
work_dir=$(mktemp -d "$output_dir.work.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT INT TERM
rpmbuild_dir="$work_dir/rpmbuild"
mkdir -p "$rpmbuild_dir"/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS} "$work_dir/tree/usr/lib/modules"
cp -a "$tree" "$work_dir/tree/usr/lib/modules/$kver"
rm -f "$work_dir/tree/usr/lib/modules/$kver"/{build,source}
tar -C "$work_dir/tree" --sort=name --owner=0 --group=0 --numeric-owner \
  -cf "$rpmbuild_dir/SOURCES/husky-kernel-modules.tar" usr
install -m 0644 "$spec" "$rpmbuild_dir/SPECS/"
# Nothing is compiled: the aarch64 package is assembled on any host.
rpmbuild -ba --target aarch64 --define "_topdir $rpmbuild_dir" \
  "$rpmbuild_dir/SPECS/kernel-modules-luma-husky.spec"
rm -rf "$output_dir"
install -d -m 0755 "$output_dir/RPMS/aarch64" "$output_dir/SRPMS"
install -m 0644 "$rpmbuild_dir"/RPMS/aarch64/*.rpm "$output_dir/RPMS/aarch64/"
install -m 0644 "$rpmbuild_dir"/SRPMS/*.rpm "$output_dir/SRPMS/"
printf 'Pixel 8 Pro kernel modules package: %s\n' "$output_dir"
