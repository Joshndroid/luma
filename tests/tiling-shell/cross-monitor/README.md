# Cross-monitor drag coverage

`run.sh OUTDIR` starts GNOME Shell headless with a 3440x1440 and a 2560x1440
monitor side by side, gives each its own Tiling Shell layout and drives real
pointer drags (`driver.js`) from one to the other and back: entering at the
height of a column's top zone, across and down the centre, re-entering at the
same point, the reverse direction, and spanning with Alt within and across
monitors. At every stop it checks the monitor under the pointer against that
monitor's layout as stored in the settings: the drawn zones (count, rectangles
with gaps, no cut or extra pieces, painted where they are), the zones that
give way, the selection (cell, gaps, corner radius) and that no other monitor
shows anything; each drag must drop into exactly the selected zones.

The matrix covers scales 1, 1.25 and 2 on either monitor, side layouts with
and without lines in common with the main one, a side monitor without a
layout of its own, drags on workspaces other than the first, and the layouts
and settings of the setup the bug was reported on (`reported`).
`LUMA_XM_THREE=1` adds a laptop panel that is then closed;
`LUMA_XM_RECONFIG=1` rearranges the monitors before the drags;
`LUMA_XM_FAST=1` moves in two steps per leg.

It runs in about 40 s per case in a Luma OS tree container, for example
`podman run --rootfs TREE:O` with this repository and the extension under test
mounted, or in Fedora 44 with the Luma packages installed.
