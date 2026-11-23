---
type: fix
component: Sound
packages: [luma-audio-policy]
highlight: false
---
Summary: Sound captures end when their app quits
Details: A sound capture now stops when the app that asked for it leaves, and the sound service no longer logs an error each time an app connects or stops playing.
