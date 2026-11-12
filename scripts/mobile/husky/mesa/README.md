# Mesa 26.3 for the Pixel 8 Pro (husky)

The Pixel 8 Pro's GPU is a Mali-G715 MC7 (Valhall v11), driven by panthor.
Fedora 44's Mesa 26.2 does not support it, so GNOME Shell falls back to
software rendering and cannot show Android's GPU buffers (black windows).
Mesa 26.3 supports it. Until Luma's own Mesa reaches 26.3, husky uses a
private build, the same method the Pixel 9 Pro Fold uses
(`scripts/mobile/pixel9-fold/mesa` on `devices/pixel9fold`).

## Build

On an x86_64 Linux host with dnf5 and podman:

```sh
sudo R=/path/on/a/large/disk scripts/mobile/husky/mesa/build-mesa.sh
```

The build roots come from Fedora 44 only (`fedora-44.repo`, checked with
Fedora's key), never from the host's repositories. Mesa is pinned to the
`mesa-26.3.0` commit (`MESA_COMMIT` overrides it). The script writes
`$R/out-<commit>/` and `$R/out-<commit>.tar.gz`, with `BUILD-INFO`
recording the commit, build time and libgallium hash.

Drivers: panfrost (GL, GLES, EGL, GBM on Wayland) and panvk (Vulkan). No
LLVM at runtime.

## Install

Unpack the tarball to `/var/lib/luma-husky/mesa` on the phone and install
the user environment generator next to this README:

```sh
install -D -m 0755 90-luma-husky-gpu /etc/systemd/user-environment-generators/90-luma-husky-gpu
```

It points user sessions, including GNOME Shell and the login screen, at the
prefix whenever `BUILD-INFO` is there. Restart the display manager or reboot
to apply it.

To go back to software rendering, `touch /var/lib/luma-husky/gpu-off`
(or remove the prefix) and restart the display manager.

## Flatpak apps

Flatpak apps do not see the prefix: they take Mesa from the runtime's GL
extension, Fedora 44's 26.2, and draw with llvmpipe on the CPU. The boot
image's `luma-husky-flatpak-gl.service` runs `flatpak-gl-extension` before
any session: it copies the prefix into the unmanaged extension
`/var/lib/flatpak/extension/org.projectluma.Platform.GL.luma-husky/aarch64/44`
whenever the prefix's `BUILD-INFO` changes (the prefix was built against
Fedora 44, the base of `org.projectluma.Platform//44`), and the generator
then sets `FLATPAK_GL_DRIVERS=luma-husky`. `gpu-off` removes the extension.
