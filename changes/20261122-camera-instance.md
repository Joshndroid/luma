---
type: fix
component: Camera
packages: [libcamera, libcamera-ipa, libcamera-gstreamer]
highlight: false
---
Summary: A camera fault no longer stops sound
Details: Cameras run in a media service of their own, so a camera that fails can no longer cut sound or disconnect Bluetooth audio, and it comes back on its own. Built-in cameras on Intel IPU laptops also no longer stop when the camera hardware rejects a frame and the image is processed without the graphics card.
