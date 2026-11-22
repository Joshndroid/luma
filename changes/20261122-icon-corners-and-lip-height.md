---
type: fix
component: Shell
packages: [gnome-shell, gnome-shell-common]
highlight: false
---
Summary: App icons in notifications and the dock get their proper rounded corners
Details: Rounded corners on app icons in notifications, the dock and the top bar were drawn slightly too large and too far out, so some icons kept square-ish corners. Phone notifications are also a pixel taller, so their buttons are no longer clipped at the bottom.
