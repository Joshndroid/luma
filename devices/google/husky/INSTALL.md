# Luma Mobile on the Pixel 8 Pro (experimental)

Support for the Google Pixel 8 Pro (`husky`) is experimental. There is no
published release yet: this guide is for developers testing builds from
source.
Installing Luma **erases everything on the phone**.

## What works

The screen (120 Hz), touch (double-tap to wake), buttons, graphics
acceleration, CPU frequency scaling with thermal limits, the battery level,
charging from USB, vibration, the speakers and microphones, the sensors
(motion, compass, pressure, light, proximity), Bluetooth and Wi-Fi scanning
(with the phone's own MAC address and its region's settings) work, and the
phone boots to Luma's desktop and runs Android apps. Joining a Wi-Fi network
is untested.
Not yet: the modem, cameras, fingerprint, suspend, USB host (OTG) and fast
charging.

Wi-Fi, Bluetooth, audio, sensors and vibration need firmware that Google
does not allow others to redistribute. It is on the phone's vendor
partition; Luma does not ship it (see `device.toml`).

Graphics acceleration needs Mesa 26.3 or newer (the Mali-G715 is not in
older releases). Until Luma's Mesa is that new, build a private copy with
`scripts/mobile/husky/mesa/build-mesa.sh`; its README explains how it is
installed and switched off.

## Before you start

- A Google Pixel 8 Pro (`husky`, not the Pixel 8 or 8a) that can be unlocked.
- A USB-C **data** cable and a computer with `fastboot` (Android platform
  tools, version 35 or newer).
- The generic Luma phone system image, built from the ARM nightly with
  `scripts/mobile/build-phone-userdata.sh`, and a husky boot image built from
  the kernel tag named in `device.toml`.
- A backup of everything on the phone.

## Install

### Step 1. Unlock the bootloader

In Android, turn on Developer options, then **OEM unlocking**. Turn the phone
off, hold **Volume Down** and press **Power** until the Fastboot screen
appears, connect it and run:

```
fastboot flashing unlock
```

Confirm on the phone. Unlocking erases the phone.

### Step 2. Check that it's the right phone

```
fastboot devices
fastboot getvar product
```

Exactly one phone must be listed, and `product` must say `husky`.

### Step 3. Install the system and start Luma

```
fastboot flash userdata luma-phone-userdata.sparse
fastboot boot luma-husky-boot.img
```

The boot image runs from memory and is never written to the phone, so a
restart returns to Google's bootloader and Android's boot partition. Run
`fastboot boot` again to start Luma.

Sign in as **luma** with the password **1234**, then change the password in
Settings right away.

Luma writes only `userdata`. The modem, radio calibration and identity
partitions (`efs`, `efs_backup`, `persist`, `modem_userdata`, `devinfo`), the
bootloader, `boot`, `vbmeta`, `dtbo`, `vendor_boot`, `init_boot` and `super`
are never written, so Google's factory image restores the phone fully.

## If something goes wrong

- Hold **Power** and **Volume Down** to return to the Fastboot screen.
- If the phone starts Android recovery and offers to wipe data, choose no
  and return to the Fastboot screen.
- If Luma stops responding, the phone's watchdog restarts it after a short
  while, usually into Android recovery. With the phone still on USB,
  `adb reboot bootloader` returns it to the Fastboot screen without
  touching the buttons; then run `fastboot boot` again.
- To go back to Android, flash
  [Google's factory image for the Pixel 8 Pro](https://developers.google.com/android/images#husky)
  with its `flash-all` script.
- Never flash partition tables, relock the bootloader while Luma is installed,
  erase partitions other than `userdata`, or change `vbmeta`.
