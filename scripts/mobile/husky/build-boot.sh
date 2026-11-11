#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Build the Luma boot image for the Pixel 8 Pro (husky).
#
#   scripts/mobile/husky/build-boot.sh --kernel <SimplyLuma/linux, branch zuma> \
#       --firmware DIR --busybox-rpm busybox-1.37.0-7.fc44.aarch64.rpm \
#       --mali-fw /usr/lib/firmware/arm/mali/arch11.8/mali_csffw.bin.xz \
#       --avb-key testkey_rsa4096.pem [--out DIR]
#
# --firmware is the tree firmware.py extracts from Google's factory image; it
# must match devices/google/husky/firmware.manifest exactly. The result,
# DIR/luma-husky-boot.img, is an Android boot image v4 holding only the kernel
# (DTB, initramfs and command line are built in) with an AVB hash footer
# signed with the public AOSP test key, which husky's ABL requires even when
# unlocked. Start it with `fastboot boot`.
#
# The image carries Google's firmware: it is for your own phone. Whether it may
# be published is a separate question; it is never committed.
#
# Needs: clang/LLVM, mkbootimg, avbtool, rpm2cpio, cpio, python3.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
luma=$(cd "$here/../../.." && pwd)
device=$luma/devices/google/husky
busybox_sha256=5149092cf1238d65959b76df72d981f9825d09760f7ace9f56f570ea4c73d4b8
busybox_rpm_sha256=11aee899410a6b301ee332a0f2937c912454d9ccdc52290cfc19ad8e3d92a655
# linux-firmware 20261019 arm/mali/arch11.8/mali_csffw.bin.xz (Mali-G715).
mali_fw_sha256=48db344cc3502fc464eb0df90b72024f11d99d15d3f1c36faa689542880e8655
# Modular drivers, loaded after switch_root by the init's units: the AoC stack
# needs its firmware after the built-in drivers have probed, and the USB-C
# port controller and charger load once the system is up.
modules="drivers/soc/google/aoc drivers/soc/google/aoc-channel
drivers/soc/google/aoc-usf drivers/iio/common/aoc_usf/aoc-usf-iio
drivers/firmware/cirrus/cs_dsp sound/soc/codecs/snd-soc-wm-adsp
sound/soc/codecs/snd-soc-cs35l41-lib sound/soc/codecs/snd-soc-cs35l41
sound/soc/codecs/snd-soc-cs35l41-spi sound/soc/google/snd-soc-google-aoc
drivers/bluetooth/hci_aoc
drivers/usb/typec/tcpm/tcpci_maxim drivers/power/supply/max77759_charger"

kernel="" firmware="" busybox_rpm="" mali_fw="" avb_key="" out=$PWD/out-husky
while [ $# -gt 0 ]; do
	case $1 in
		--kernel) kernel=$2; shift 2 ;;
		--firmware) firmware=$2; shift 2 ;;
		--busybox-rpm) busybox_rpm=$2; shift 2 ;;
		--mali-fw) mali_fw=$2; shift 2 ;;
		--avb-key) avb_key=$2; shift 2 ;;
		--out) out=$2; shift 2 ;;
		*) echo "unknown argument $1" >&2; exit 2 ;;
	esac
done
[ -n "$kernel" ] && [ -n "$firmware" ] && [ -n "$busybox_rpm" ] && [ -n "$mali_fw" ] &&
	[ -n "$avb_key" ] || { sed -n '5,8p' "$0" >&2; exit 2; }
mkdir -p "$out"
kernel=$(realpath "$kernel") firmware=$(realpath "$firmware") out=$(realpath "$out")
busybox_rpm=$(realpath "$busybox_rpm") mali_fw=$(realpath "$mali_fw") avb_key=$(realpath "$avb_key")

toml() {  # toml FILE KEY [KEY...]
	python3 -c 'import sys, tomllib
v = tomllib.load(open(sys.argv[1], "rb"))
for k in sys.argv[2:]:
    v = v[k]
print(v)' "$@"
}
release=$(toml "$device/device.toml" kernel release)
config=$kernel/$(toml "$device/device.toml" kernel config)
os_version=$(toml "$device/boot.toml" os_version)
patch_level=$(toml "$device/boot.toml" os_patch_level | cut -c1-7)
partition_size=$(toml "$device/boot.toml" partition_size)
rollback_index=$(toml "$device/boot.toml" avb rollback_index)
[ -f "$config" ] || { echo "no kernel config at $config (is --kernel the zuma branch?)" >&2; exit 1; }
# The kernel forces its built-in command line: boot.toml's fixed arguments,
# then the generic phone userdata image's filesystems (the UUIDs the device
# kit builder checks for) and ostree=aboot. The config must carry exactly that.
cmdline=$(python3 - "$device/boot.toml" "$luma/scripts/devices/build-device-kit.py" <<'EOF'
import importlib.util, sys, tomllib
spec = importlib.util.spec_from_file_location("kit", sys.argv[2])
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)
boot = tomllib.load(open(sys.argv[1], "rb"))["cmdline"]
values = {"pmos_root_uuid": kit.ROOT_UUID, "pmos_boot_uuid": kit.BOOT_UUID, "ostree": "aboot"}
print(" ".join(boot["args"] + [f"{k}={values[k]}" for k in boot["generated"]]))
EOF
)
[ "$(sed -n 's/^CONFIG_CMDLINE="\(.*\)"$/\1/p' "$config")" = "$cmdline" ] || {
	echo "CONFIG_CMDLINE in $config is not boot.toml's command line:" >&2
	echo "  $cmdline" >&2
	exit 1
}
grep -qx 'CONFIG_CMDLINE_FORCE=y' "$config" || { echo "$config does not force its command line" >&2; exit 1; }

echo "$busybox_rpm_sha256  $busybox_rpm" | sha256sum -c --quiet
echo "$mali_fw_sha256  $mali_fw" | sha256sum -c --quiet
python3 "$here/firmware.py" check "$firmware" "$device/firmware.manifest"

obj=$out/build
work=$out/initramfs
rm -rf "$work"
mkdir -p "$work/root"/{proc,sys,dev,tmp,run,sysroot,luma/bin,luma/lib,luma/modules,luma/handheld,luma/mesa} "$obj"

# --- initramfs ----------------------------------------------------------------
(cd "$work" && rpm2cpio "$busybox_rpm" | cpio -idm --quiet ./usr/bin/busybox)
echo "$busybox_sha256  $work/usr/bin/busybox" | sha256sum -c --quiet
install -m 0755 "$work/usr/bin/busybox" "$work/root/luma/bin/busybox"
install -m 0755 "$here/initramfs/init" "$work/root/init"
# Luma's shared OSTree resolver, unchanged.
install -m 0644 "$luma/scripts/mobile/common/initramfs/luma-ostree-boot.sh" \
	"$work/root/luma/lib/ostree-boot.sh"
# Google's firmware (checked above) and the GPU's CSF firmware, which panthor
# (built in) requests before the root is mounted.
mkdir -p "$work/root/lib/firmware"
cp -r "$firmware/." "$work/root/lib/firmware/"
install -D -m 0644 "$mali_fw" "$work/root/lib/firmware/arm/mali/arch11.8/mali_csffw.bin.xz"
# Luma's shared handheld defaults, the panel outline and the Mesa generator.
install -m 0644 "$luma/config/mobile/luma-shell-handheld.dconf" \
	"$luma/config/mobile/logind-handheld.conf" "$work/root/luma/handheld/"
install -m 0644 "$luma/config/mobile/display-geometry/google-husky.json" \
	"$work/root/luma/handheld/display-geometry.json"
install -m 0755 "$here/mesa/90-luma-husky-gpu" "$work/root/luma/mesa/"
install -D -m 0755 "$here/initramfs/audio/speaker-calibrate" "$work/root/luma/audio/speaker-calibrate"
install -D -m 0755 "$here/initramfs/audio/speaker-ready" "$work/root/luma/audio/speaker-ready"
install -D -m 0644 "$here/initramfs/audio/aoc-audio.conf" "$work/root/luma/audio/aoc-audio.conf"
install -D -m 0644 "$here/initramfs/audio/HiFi.conf" "$work/root/luma/audio/HiFi.conf"
cat > "$work/devnodes.list" <<'EOF'
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
nod /dev/null 0666 0 0 c 1 3
EOF

# --- kernel -------------------------------------------------------------------
# LOCALVERSION= (empty) keeps scripts/setlocalversion from appending "+", so
# the release is exactly the one device.toml names.
k() { make -C "$kernel" O="$obj" ARCH=arm64 LLVM=1 LOCALVERSION= "$@"; }
cp "$config" "$obj/.config"
"$kernel/scripts/config" --file "$obj/.config" --set-str INITRAMFS_SOURCE \
	"$work/root $work/devnodes.list"
k olddefconfig
[ "$(k -s kernelrelease)" = "$release" ] || { echo "kernel release is not $release" >&2; exit 1; }
# The embedded DTB first (vmlinux links it in, and no rule builds it), then
# vmlinux, which modpost needs; the Image link below repacks the initramfs once
# the modules are in it.
k -j"$(nproc)" exynos/google/zuma-husky.dtb
k -j"$(nproc)" vmlinux
k -j"$(nproc)" modules
for m in $modules; do
	install -m 0644 "$obj/$m.ko" "$work/root/luma/modules/"
done
k -j"$(nproc)" Image

# --- boot image (devices/google/husky/boot.toml) ------------------------------
img=$out/luma-husky-boot.img
mkbootimg --header_version 4 --kernel "$obj/arch/arm64/boot/Image" \
	--os_version "$os_version" --os_patch_level "$patch_level" --cmdline "" -o "$img"
avbtool add_hash_footer --image "$img" --partition_name boot \
	--partition_size "$partition_size" --key "$avb_key" --algorithm SHA256_RSA4096 \
	--rollback_index "$rollback_index"
# The command line is inside the kernel; build-device-kit.py --boot-cmdline
# reads this record of it.
printf '%s\n' "$cmdline" > "$out/luma-husky-boot.cmdline"
sha256sum "$img"
