#!/bin/bash
# vm.sh WORKDIR start NAME PORT [BASE] | stop NAME | ssh NAME CMD... | tunnel NAME HUBPORT
# Each VM is a qcow2 overlay of base.qcow2 (or BASE), with SSH on 127.0.0.1:PORT.
set -euo pipefail
work=$1 action=$2 name=$3
port() { cat "$work/$name.port"; }
case "$action" in
start)
  # libvirt, configured as the nightly's media verification boots its installs (scripts/os/verify-media.sh).
  p=$4 base=${5:-$work/base.qcow2}
  [ -f "$work/$name.qcow2" ] || qemu-img create -q -f qcow2 -b "$base" -F qcow2 "$work/$name.qcow2"
  echo "$p" > "$work/$name.port"
  virsh -q --connect qemu:///system destroy "connect-sync-$name" >/dev/null 2>&1 || true
  unset XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS
  # Keep a defined VM: its NVRAM and TPM state hold what systemd-creds sealed (a new TPM can't open them).
  if virsh -q --connect qemu:///system dominfo "connect-sync-$name" >/dev/null 2>&1 && [ ! -e "$work/$name.fresh" ]; then
    exec virsh -q --connect qemu:///system start "connect-sync-$name"
  fi
  virsh -q --connect qemu:///system undefine --nvram --tpm "connect-sync-$name" >/dev/null 2>&1 || true
  rm -f "$work/$name.fresh"
  virt-install --connect qemu:///system --name "connect-sync-$name" --import \
    --memory 4096 --vcpus 4 --cpu host-passthrough --machine q35 --boot uefi \
    --disk "path=$work/$name.qcow2,format=qcow2,bus=virtio,cache=unsafe,discard=unmap" \
    --network "passt,portForward0.proto=tcp,portForward0.range0.start=$p,portForward0.range0.to=22" \
    --graphics vnc,listen=127.0.0.1 --video virtio --rng /dev/urandom \
    --serial "file,path=/var/log/libvirt/qemu/connect-sync-$name-serial.log" \
    --os-variant fedora-unknown --noautoconsole --wait 0
  ;;
stop) systemctl stop "connect-sync-tunnel-$name.scope" "connect-sync-tunnel-$name" 2>/dev/null || true; virsh -q --connect qemu:///system destroy "connect-sync-$name" >/dev/null 2>&1 || true ;;
ssh) shift 3; exec ssh -q -p "$(port)" -i "$work/key" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR root@127.0.0.1 "$@" ;;
tunnel)
  # The VM reaches the local Hub as http://127.0.0.1:43100 (plain http only to its own loopback; the guest policy refuses sshd a listener on 3100).
  # A scope, not a service: on this host SELinux keeps transient services from running ssh.
  systemctl stop "connect-sync-tunnel-$name.scope" 2>/dev/null || true
  systemd-run --scope --unit="connect-sync-tunnel-$name" --collect -- setsid nohup bash -c "while true; do ssh -N -p $(port) -i $work/key -o BatchMode=yes \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -R 43100:127.0.0.1:$4 root@127.0.0.1; sleep 5; done" \
    >/dev/null 2>&1 </dev/null &
  ;;
esac
