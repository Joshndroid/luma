#!/usr/bin/env bash
# Build Mesa 26.3 (panfrost gallium + panvk, EGL/GBM/GLES/GL, Wayland) for the
# Pixel 8 Pro's Mali-G715 MC7 (v11) as a private prefix /var/lib/luma-husky/mesa.
# Fedora 44 ships Mesa 26.2, which has no G715. Same method as the Pixel 9 Pro
# Fold (scripts/mobile/pixel9-fold/mesa on devices/pixel9fold): an x86_64
# Fedora 44 build root plus an aarch64 sysroot made with dnf --installroot from
# Fedora 44 only (fedora-44.repo, never the host's repos); the build runs at
# native speed in podman --rootfs, cross-compiling with clang.
#
#   sudo R=<work dir on a large disk> scripts/mobile/husky/mesa/build-mesa.sh
#
# Output: $R/out-<commit>/ (the prefix's contents plus BUILD-INFO), owned by
# the sudo user. Needs dnf5, podman and about 4 GB in $R, 3 GB in /dev/shm.
set -euo pipefail
MESA_COMMIT=${MESA_COMMIT:-0a6fa60abb737d26ac8a316a4aad7c9ddf64f9e4}   # mesa-26.3.0
HERE=$(cd "$(dirname "$0")" && pwd)
: "${R:?work directory (build roots, output)}"
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
W=${W:-/dev/shm/luma-husky-mesa}
OUT=${OUT:-$R/out-${MESA_COMMIT:0:12}}
mkdir -p "$R"; setfacl -b -k "$R" 2>/dev/null || true
DNF="dnf -y -q --releasever=44 --setopt=reposdir=$HERE --setopt=install_weak_deps=False --nodocs"

if [ ! -e "$R/x86/.done" ]; then
  $DNF --installroot="$R/x86" install bash coreutils findutils grep sed gawk diffutils tar zstd file git-core \
    gcc gcc-c++ clang lld llvm llvm-devel clang-devel libclc libclc-devel spirv-llvm-translator-devel \
    spirv-tools-devel spirv-headers-devel glslang meson ninja-build pkgconf-pkg-config bison flex \
    python3-mako python3-pyyaml python3-packaging python3-ply \
    wayland-devel libdrm-devel zlib-ng-compat-devel expat-devel libzstd-devel elfutils-libelf-devel binutils
  touch "$R/x86/.done"
fi
if [ ! -e "$R/a64/.done" ]; then
  # filesystem first: it owns the /lib64 -> usr/lib64 symlink that libc.so's
  # linker script needs, and fails to install once anything created /lib64
  $DNF --installroot="$R/a64" --forcearch=aarch64 --setopt=tsflags=noscripts install filesystem
  $DNF --installroot="$R/a64" --forcearch=aarch64 --setopt=tsflags=noscripts install \
    glibc-devel gcc libstdc++-devel libdrm-devel wayland-devel wayland-protocols-devel libglvnd-devel expat-devel \
    zlib-ng-compat-devel libzstd-devel elfutils-libelf-devel
  touch "$R/a64/.done"
fi

mkdir -p "$W"
if [ "$(git -C "$W/src" rev-parse HEAD 2>/dev/null)" != "$MESA_COMMIT" ]; then
  rm -rf "$W/src"; git init -q "$W/src"
  git -C "$W/src" fetch -q --depth 1 https://gitlab.freedesktop.org/mesa/mesa.git "$MESA_COMMIT"
  git -C "$W/src" checkout -q FETCH_HEAD
fi
rm -rf "$W/native" "$W/native-inst" "$W/cross" "$W/dest"
cp "$HERE/inside.sh" "$HERE/aarch64.cross" "$W/"
podman run --rm --network none --security-opt label=disable -v "$W:/w" -v "$R/a64:/sysroot:ro" -e JOBS="${JOBS:-$(nproc)}" \
  --rootfs "$R/x86" /bin/bash /w/inside.sh
rm -rf "$OUT"; mkdir -p "$OUT"
cp -a "$W/dest/var/lib/luma-husky/mesa/." "$OUT/"
gallium=$(find "$OUT/lib64" -name 'libgallium-*.so' -type f | head -1)
{ echo "MESA_COMMIT=$MESA_COMMIT"
  echo "BUILT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "GALLIUM_SHA256=$(sha256sum "$gallium" | cut -d' ' -f1)"; } > "$OUT/BUILD-INFO"
tar -C "$OUT" -czf "$OUT.tar.gz" .
[ -n "${SUDO_USER:-}" ] && chown -R "$SUDO_USER:" "$OUT" "$OUT.tar.gz"
du -sh "$OUT"; cat "$OUT/BUILD-INFO"; sha256sum "$OUT.tar.gz"
