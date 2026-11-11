# Pixel 8 Pro (husky) boot image

- `firmware.py`: extracts Google's firmware from the husky factory image named
  in `devices/google/husky/device.toml` and checks a firmware tree against
  `firmware.manifest`. The firmware is not redistributable; it never enters
  git and stays on the machine that builds the image.
- `build-boot.sh`: builds `luma-husky-boot.img` from the kernel (SimplyLuma/linux
  branch `zuma`, config from `device.toml`), the firmware tree and the files
  below, with the AVB footer husky's ABL requires. The kernel's built-in
  command line must be `boot.toml`'s; the build refuses any other and records
  it as `luma-husky-boot.cmdline`. The device kit is then
  `scripts/devices/build-device-kit.py --device google/husky --boot-image
  luma-husky-boot.img ...`.
- `initramfs/init`: mounts LumaRoot from `userdata`, picks the OSTree
  deployment with Luma's shared resolver, grows the root on first boot and
  sets up what a device package would otherwise ship: the handheld defaults,
  the firmware copy and loader path, the AoC (sensors, Bluetooth, audio with
  speaker protection), USB-C and charging, and the Wi-Fi interface policy.
  It has no shell; on failure it shows the reason and powers off.
- `initramfs/audio/`: the AoC card's UCM profile and the speaker protection
  steps (calibration from the phone's own persist partition, read-only).
- `mesa/`: a private Mesa 26.3 until Luma's Mesa drives the Mali-G715.

The same approach as the Pixel 9: the firmware travels in the boot image's
initramfs, never in the repository.
