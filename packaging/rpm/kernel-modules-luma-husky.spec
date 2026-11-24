# SPDX-License-Identifier: Apache-2.0
# Kernel modules of the Pixel 8 Pro (husky) kernel: SimplyLuma/linux branch
# zuma (devices/google/husky/device.toml [kernel]), arm64 defconfig with
# luma/config/luma.fragment and luma/config/zuma.fragment, built by
# scripts/mobile/husky/build-boot.sh. Source0 is that build's stripped
# modules_install tree, /usr/lib/modules/<release> without build/source links
# (scripts/packages/build-kernel-modules-luma-husky.sh). The modules are signed
# with that build's key, so the package and the boot image of the same build
# belong together.
#
# The package carries no boot image and no luma-device-boot declaration: the
# husky boot image holds Google's firmware in its initramfs, which may not be
# redistributed, and husky starts its boot image from RAM (fastboot boot),
# never from boot_a/boot_b. The boot image keeps its own copies of the modules
# it loads; this package is what device.toml [kernel] package names, so the
# Release below moves whenever the kernel inputs do
# (scripts/devices/device-kernels.py check). Husky has no device repository,
# so nothing installs it yet.
%global kver 7.2.15-zuma
%global debug_package %{nil}
%global __strip /bin/true
%global _build_id_links none

Name:           kernel-modules-luma-husky
Version:        7.2.15
Release:        1.luma.1%{?dist}
Summary:        Kernel modules for the Pixel 8 Pro kernel
License:        GPL-2.0-only
URL:            https://github.com/SimplyLuma/linux/tree/zuma
Source0:        husky-kernel-modules.tar
ExclusiveArch:  aarch64
AutoReqProv:    no

%description
Loadable modules (and their modules.* indexes) for the Google Pixel 8 Pro
(husky) kernel %{kver}, which starts from RAM with its own boot image.

%prep
%setup -q -c -T
tar -xf %{SOURCE0}
test -f usr/lib/modules/%{kver}/modules.dep

%build

%install
install -d %{buildroot}/usr/lib/modules
cp -a usr/lib/modules/%{kver} %{buildroot}/usr/lib/modules/
# cp -a and install -d keep the build host's setgid bits and ACL masks; ship 0755/0644
# (00755: GNU chmod keeps a directory's setgid bit for 0755).
find %{buildroot} -type d -exec chmod 00755 {} +
find %{buildroot} -type f -exec chmod u+rw,go-w,go+rX {} +

%check
test -z "$(find %{buildroot} -type d ! -perm 0755)"
test -z "$(find %{buildroot} -type f ! -perm -0444)"
m=%{buildroot}/usr/lib/modules/%{kver}
test ! -e $m/vmlinuz && test ! -e $m/build && test ! -e $m/source
# The modules the boot image loads after switch_root (build-boot.sh $modules).
for k in drivers/soc/google/aoc drivers/soc/google/aoc-channel \
    drivers/soc/google/aoc-usf drivers/iio/common/aoc_usf/aoc-usf-iio \
    drivers/firmware/cirrus/cs_dsp sound/soc/codecs/snd-soc-wm-adsp \
    sound/soc/codecs/snd-soc-cs35l41-lib sound/soc/codecs/snd-soc-cs35l41 \
    sound/soc/codecs/snd-soc-cs35l41-spi sound/soc/google/snd-soc-google-aoc \
    drivers/bluetooth/hci_aoc \
    drivers/input/touchscreen/touch_bus_negotiator drivers/input/misc/aoc-gesture-wake \
    drivers/usb/typec/tcpm/tcpci_maxim drivers/power/supply/max77759_charger \
    drivers/cpufreq/cpufreq-touch-boost \
    net/nfc/nfc net/nfc/nci/nci drivers/nfc/st-nci/st-nci drivers/nfc/st-nci/st-nci_i2c; do
  test -f $m/kernel/$k.ko
done
# Stripped (INSTALL_MOD_STRIP=1) and signed (CONFIG_MODULE_SIG_ALL).
! readelf -S $m/kernel/drivers/soc/google/aoc.ko | grep -q '\.debug_info'
tail -c 28 $m/kernel/drivers/soc/google/aoc.ko | grep -q 'Module signature appended'

%files
%dir /usr/lib/modules/%{kver}
/usr/lib/modules/%{kver}/kernel/
/usr/lib/modules/%{kver}/modules.*

%changelog
* Mon Nov 23 2026 Joshndroid <fmxjoshiewah@gmail.com> - 7.2.15-1.luma.1
- Modules of the Pixel 8 Pro kernel 7.2.15-zuma (SimplyLuma/linux zuma),
  from the build that makes its boot image.
