---
type: fix
component: Raspberry Pi
packages: [luma-streamy]
highlight: false
---
Summary: Raspberry Pi updates install again
Details: On ARM computers Streamy no longer pulls in an extra GStreamer plugin set that clashed with the Raspberry Pi 5's own video plugins, so Pi updates and new Pi cards install. On ARM, streaming to SRT destinations needs that plugin set installed separately.
