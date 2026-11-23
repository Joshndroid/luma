---
type: fix
component: Desktop
packages: [mutter, mutter-common, gnome-shell-extension-tiling-shell]
highlight: false
---
Summary: Games keep their shape in tiles
Details: A window that keeps a fixed shape, such as a game, now keeps it when you tile it: it takes the largest size with that shape in the tile, centred, instead of stretching across the tile with bands above and below. A tile smaller than the window's minimum size leaves the window at its minimum.
