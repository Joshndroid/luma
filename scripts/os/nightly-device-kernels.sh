#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Nightly step 4 (aarch64): device kernels. A device whose kernel package in
# this tree is newer than what its device repository serves
# (scripts/devices/device-kernels.py plan) gets it built here, once per
# release, into $LUMA_OS_ROOT/device-kernels/<vendor>-<codename>/<package>-<evr>/.
#
# A device kernel writes boot partitions on every device that installs it, so
# it is staged for the device repository only after the phone gate passed for
# that exact build: $LUMA_OS_ROOT/device-gates/<vendor>-<codename>/<package>-<evr>.pass
# (docs/kernel.md, "Device kernel updates"). With the gate passed and the
# current published repository at $LUMA_OS_ROOT/device-repos/<id>, the signed
# candidate repository is written to $LUMA_OS_ROOT/device-repos/<id>.candidate
# for the upload to dl.
#
# Writes $LUMA_OS_ROOT/status/device-kernels.json. Never fails the OS nightly:
# a device kernel that cannot be built is reported and waits.
#
# usage: nightly-device-kernels.sh --source CHECKOUT
set -uo pipefail

[ "${1:-}" = --source ] && [ -d "${2:-}" ] || { sed -n 's/^# usage: /usage: /p' "$0" >&2; exit 2; }
source_dir=$(realpath -e "$2")
root=${LUMA_OS_ROOT:?}
tool="$source_dir/scripts/devices/device-kernels.py"
status="$root/status/device-kernels.json"
mkdir -p "$root/status" "$root/device-kernels" "$root/device-gates"

plan=$(python3 "$tool" --root "$source_dir" plan --json) || { echo 'device kernels: plan failed' >&2; exit 0; }
results=()
while IFS=$'\t' read -r device package evr repo_id; do
  [ -n "$device" ] || continue
  slug=${device//\//-}
  out="$root/device-kernels/$slug/$package-$evr"
  state=built
  if [ ! -s "$out/SHA256SUMS" ]; then
    rm -rf "$out"
    if ! nice -n 19 ionice -c3 python3 "$tool" --root "$source_dir" build --device "$device" --output "$out"; then
      state=build-failed
    fi
  fi
  if [ "$state" = built ]; then
    if [ -f "$root/device-gates/$slug/$package-$evr.pass" ]; then
      if [ -d "$root/device-repos/$repo_id" ]; then
        if bash "$source_dir/scripts/devices/publish-device-repo.sh" --current "$root/device-repos/$repo_id" \
             --output "$root/device-repos/$repo_id.candidate" "$out"/"$package"-*.rpm; then
          state=candidate
        else
          state=sign-failed
        fi
      else
        state=no-current-repo
      fi
    else
      state=awaiting-phone-gate
    fi
  fi
  printf 'GATE  device kernel %s %s-%s: %s (%s)\n' "$device" "$package" "$evr" "$state" "$out"
  results+=("$(python3 -c 'import json,sys; print(json.dumps(dict(zip(("device","package","evr","state","path"), sys.argv[1:]))))' \
    "$device" "$package" "$evr" "$state" "$out")")
done < <(python3 -c '
import json, sys
for row in json.load(sys.stdin):
    if row.get("action") == "build":
        repo_id = row["repo"].rstrip("/").rsplit("/", 1)[-1]
        print(row["device"], row["package"], row["tree"], repo_id, sep="\t")
' <<<"$plan")
printf '[%s]\n' "$(IFS=,; echo "${results[*]:-}")" >"$status"
exit 0
