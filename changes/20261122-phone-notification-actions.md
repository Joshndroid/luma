---
type: fix
component: Phone
packages: [gnome-shell, gnome-shell-common]
highlight: false
---
Summary: Notification buttons no longer cut off on phones
Details: When a notification's text wrapped onto a second line only on a phone's narrower banner, the banner kept its one-line height and cut off its buttons. It now grows to fit.
