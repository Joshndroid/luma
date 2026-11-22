---
type: fix
component: Streamy
packages: [luma-streamy]
highlight: false
---
Summary: Streamy quits cleanly
Details: Streamy waits for its video pipeline to stop before it closes, so quitting no longer crashes, and a restarted preview keeps its picture.
