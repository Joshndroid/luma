#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# A device repository ([device_repo] in device.toml) with new packages: the
# current published repository's packages, every older build of an added
# package's name dropped, the added ones signed with the Luma OS Release key,
# createrepo_c, repomd.xml signed and the key exported, then verified with a
# keyring holding only that key. The result is a complete repository in
# OUTPUT, ready for the upload to dl (same layout as the published one).
#
# usage: publish-device-repo.sh --current DIR --output DIR RPM...
#
# Needs LUMA_OS_KEYS and LUMA_OS_SECRETS (scripts/os/lib/common.sh), rpm,
# rpmsign, gpg and createrepo_c (on PATH, else the release tools container).
set -euo pipefail

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
# shellcheck disable=SC1091
. "$repo_root/scripts/os/lib/common.sh"
fail() { printf 'publish-device-repo: %s\n' "$*" >&2; exit 1; }
usage() { sed -n 's/^# usage: /usage: /p' "$0" >&2; exit 2; }

current="" output=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --current) current=$(realpath -e "${2:?}"); shift 2 ;;
    --output) output=${2:?}; shift 2 ;;
    --) shift; break ;;
    -*) usage ;;
    *) break ;;
  esac
done
[ -n "$current" ] && [ -n "$output" ] && [ "$#" -gt 0 ] || usage
[ -d "$current/Packages" ] && [ -f "$current/repodata/repomd.xml" ] || fail "$current is not an rpm-md repository"

new="$output.new"
rm -rf "$new"
install -d -m 0755 "$new/Packages"
declare -A added=()
for rpm_file in "$@"; do
  [ -f "$rpm_file" ] || fail "no such package: $rpm_file"
  added[$(rpm -qp --nosignature --qf '%{NAME}.%{ARCH}' "$rpm_file")]=1
done
for rpm_file in "$current"/Packages/*.rpm; do
  key=$(rpm -qp --nosignature --qf '%{NAME}.%{ARCH}' "$rpm_file")
  [ -n "${added[$key]:-}" ] || cp -a "$rpm_file" "$new/Packages/"
done
cp -a "$@" "$new/Packages/"

fpr=$(luma_os_gpg_fingerprint)
home=$(luma_os_gpg_home)
luma_os_gpg_unlock
trap luma_os_gpg_lock EXIT
for rpm_file in "$@"; do
  rpmsign --define "_gpg_path $home" --define "_gpg_name $fpr" --addsign "$new/Packages/$(basename "$rpm_file")" >/dev/null
done
if command -v createrepo_c >/dev/null 2>&1; then
  createrepo_c --quiet "$new"
else
  LUMA_OS_TOOLS_MOUNTS=$new luma_os_tools createrepo_c --quiet "$new"
fi
gpg --batch --homedir "$home" --local-user "$fpr" --armor --detach-sign \
  --output "$new/repodata/repomd.xml.asc" "$new/repodata/repomd.xml"
gpg --batch --homedir "$home" --armor --export "$fpr" >"$new/RPM-GPG-KEY-luma-os-release"

# Verify as a phone would, against only the exported key: the added packages
# must carry its signature, and nothing in the repository may fail its check.
verify=$(mktemp -d)
trap 'rm -rf "$verify"; luma_os_gpg_lock' EXIT
keys=(--define '_keyring fs' --define "_keyringpath $verify/keys")
rpmkeys "${keys[@]}" --import "$new/RPM-GPG-KEY-luma-os-release"
for rpm_file in "$@"; do
  rpmkeys "${keys[@]}" -K "$new/Packages/$(basename "$rpm_file")" | grep -q 'signatures OK$' ||
    fail "$(basename "$rpm_file") is not signed by the release key"
done
for rpm_file in "$new"/Packages/*.rpm; do
  rpmkeys "${keys[@]}" -K "$rpm_file" | grep -q ' OK$' || fail "$(basename "$rpm_file") fails its check"
done
mkdir -m 0700 "$verify/gnupg"
gpg --batch --homedir "$verify/gnupg" --import "$new/RPM-GPG-KEY-luma-os-release" 2>/dev/null
gpg --batch --homedir "$verify/gnupg" --verify "$new/repodata/repomd.xml.asc" "$new/repodata/repomd.xml" 2>/dev/null ||
  fail 'repomd.xml signature does not verify'

# The web server reads these as another user: a package copied from a build
# tree with group-only modes was served as 403 (FP6, 2026-11-22).
find "$new" -type d -exec chmod 0755 {} +
find "$new" -type f -exec chmod 0644 {} +
rm -rf "$output"
mv "$new" "$output"
printf 'Device repository: %s (%d packages)\n' "$output" "$(find "$output/Packages" -name '*.rpm' | wc -l)"
