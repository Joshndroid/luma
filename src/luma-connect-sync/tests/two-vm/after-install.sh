#!/bin/bash
# after-install.sh WORKDIR: keep the firmware the installer used, then drop the libvirt domain.
set -euo pipefail
work=$1
xml=$(virsh --connect qemu:///system dumpxml connect-sync-base)
code=$(sed -n 's#.*<loader[^>]*>\(.*\)</loader>.*#\1#p' <<<"$xml")
nvram=$(sed -n 's#.*<nvram[^>]*>\(.*\)</nvram>.*#\1#p' <<<"$xml")
cp "$nvram" "$work/base.vars"
fmt() { qemu-img info --output=json "$1" | python3 -c 'import json,sys; print(json.load(sys.stdin)["format"])'; }
printf 'CODE=%s\nCODE_FORMAT=%s\nVARS=%s\nVARS_FORMAT=%s\n' "$code" "$(fmt "$code")" "$work/base.vars" "$(fmt "$work/base.vars")" > "$work/firmware.env"
virsh --connect qemu:///system undefine --nvram connect-sync-base >/dev/null
cat "$work/firmware.env"
