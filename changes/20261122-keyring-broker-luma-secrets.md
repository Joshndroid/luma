---
type: security
component: Installer
packages: [luma-application-installer]
highlight: false
---
Summary: Apps can no longer reach Luma's own saved keys
Details: The keyring broker that apps use refuses every entry that belongs to Luma itself, such as Connect Sync and Messages keys, whatever the app asks for.
