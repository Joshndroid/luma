#!/bin/bash
# Install one nightly ISO into base.qcow2 (heavy: run alone, through luma-heavy).
#   install-base.sh ISO WORKDIR
set -euo pipefail
iso=$1 work=$2
label=$(isoinfo -d -i "$iso" | sed -n 's/^Volume id: //p')
[ -n "$label" ] || { echo "no volume label in $iso" >&2; exit 1; }
mkdir -p "$work"
[ -f "$work/key" ] || ssh-keygen -q -t ed25519 -N '' -C connect-sync-two-vm -f "$work/key"
sed "s#@SSH_PUBLIC_KEY@#$(cat "$work/key.pub")#" "$(dirname "$0")/ks.cfg.in" > "$work/ks.cfg"
unset XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
exec flock /var/lock/luma-heavy.lock nice -n 10 ionice -c3 virt-install --connect qemu:///system --name connect-sync-base \
  --arch x86_64 --virt-type kvm --memory 6144 --vcpus 4 --cpu host-passthrough --machine q35 --boot uefi \
  --disk path="$work/base.qcow2",size=40,format=qcow2,bus=virtio,cache=unsafe,discard=unmap \
  --location "$iso",kernel=images/pxeboot/vmlinuz,initrd=images/pxeboot/initrd.img --initrd-inject "$work/ks.cfg" \
  --extra-args "inst.ks=file:/ks.cfg inst.stage2=hd:LABEL=$label inst.text console=ttyS0 inst.notmux" \
  --network none --graphics vnc,listen=127.0.0.1 --video virtio --rng /dev/urandom \
  --os-variant fedora-unknown --noreboot --noautoconsole --wait 75
