---
type: fix
component: Desktop
packages: [gnome-shell-extension-tiling-shell]
highlight: false
---
Summary: Dragged windows land in the zones you see
Details: Dragging a window between monitors, or on any workspace other than the first, now always shows and fills the current layout of the monitor under the pointer. Zones are no longer cut into halves without gaps, and a window is never dropped into a zone that is not in the layout. Holding Alt to fill more than one zone now takes whole zones only.
