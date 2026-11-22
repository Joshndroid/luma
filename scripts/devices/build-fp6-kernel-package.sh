#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# devices/fairphone/fp6/device.toml [kernel] build: the FP6 kernel package
# from this tree. The kernel (scripts/kernel/build-fp6.sh, device.toml [kernel]
# tag and patches), its boot image (scripts/mobile/build-fp6-boot-image.sh) and
# kernel-modules-luma-fp6 carrying both (scripts/packages/
# build-kernel-modules-luma-fp6.sh), so luma-device-boot writes that image to
# the inactive slot on every phone that installs the package.
#
# usage: build-fp6-kernel-package.sh --output DIR
#
# FP6_KIT (default /srv/dev-storage/projects/luma-os/build/fp6-kit) holds
# tools/{mkbootimg,unpack_bootimg,avbtool}.py and alpha1/luma-mobile-alpha1-fp6-boot.bin.
# DIR receives kernel/ (the build-fp6.sh output), boot.img, the RPMs and SHA256SUMS.
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
[ "${1:-}" = --output ] && [ -n "${2:-}" ] || { sed -n 's/^# usage: /usage: /p' "$0" >&2; exit 2; }
mkdir -p "$2"
out=$(realpath -e "$2")
kit=${FP6_KIT:-/srv/dev-storage/projects/luma-os/build/fp6-kit}
for f in tools/mkbootimg.py tools/unpack_bootimg.py tools/avbtool.py alpha1/luma-mobile-alpha1-fp6-boot.bin; do
  [ -f "$kit/$f" ] || { printf 'build-fp6-kernel-package: %s is missing\n' "$kit/$f" >&2; exit 1; }
done

cd "$repo_root"
bash scripts/kernel/build-fp6.sh "$out/kernel"
MKBOOTIMG=$kit/tools/mkbootimg.py UNPACK_BOOTIMG=$kit/tools/unpack_bootimg.py AVBTOOL=$kit/tools/avbtool.py \
  bash scripts/mobile/build-fp6-boot-image.sh --alpha-boot "$kit/alpha1/luma-mobile-alpha1-fp6-boot.bin" \
  --kernel "$out/kernel/Image.gz" --dtb "$out/kernel/dtb" --output "$out/boot.img"
bash scripts/packages/build-kernel-modules-luma-fp6.sh --boot-image "$out/boot.img" --tree "$out/kernel/modules"
cp -a build/packages/kernel-modules-luma-fp6/RPMS/aarch64/*.rpm "$out/"
(cd "$out" && sha256sum boot.img ./*.rpm >SHA256SUMS)
printf 'FP6 kernel package: %s\n' "$out"
