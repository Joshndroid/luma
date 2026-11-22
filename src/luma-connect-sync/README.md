# luma-connect-sync

Phase 1 of ADR-054 (`docs/decisions/054-connect-sync.md`): two computers keep
the same apps and the same closed-app state, end-to-end encrypted, through
the Luma Connect Hub's `/api/sync/v1/` (website repo,
`deploy/hub/connect-sync/`). It runs as a user service.

## What it syncs

- **Apps installed** (zone `apps`): one record per Flatpak (remote name and
  URL, branch, hold) and per ADR-052 store app (the store record's origin:
  package, userland, repositories it added). The receiving computer installs
  Flatpaks with `--user` from a remote it already trusts at the same URL, and
  rebuilds store apps from their repositories with its own installer
  (`store_rebuild.py`); no binary is copied. The set is a union: removing an
  app on one device doesn't remove it elsewhere.
- **App data** (zone `appdata:<app>`): the app's data folder
  (`~/.var/app/<id>`, or the ADR-052 data folder), its host dconf path, and,
  through the `keychain` zone, the secret portal's per-app secret, so
  `data/keyrings/` opens on the other device. Snapshots are taken only when
  the app has been closed for 20 s; an incoming snapshot is built beside the
  folder and swapped in by rename while the app is closed. Caches, `tmp/`,
  locks, sockets, `Singleton*`, `*.pid`, `.flatpak-info`, shader caches and
  files over 1 GB never sync (`data/sync/exclude.d`). Apps that sync
  themselves are on `data/sync/deny.d`. 2 GB per folder unless opted in.
  Each app has a switch; turning it off keeps local data.

Not in Phase 1: settings, appearance, keyboard, Wi-Fi, passwords, browser
(their switches are stored), the third-party API, phones and TVs.

## Keys

`crypto.py` and `account.py` carry the full description. In short:

| Key | Where | Primitive |
| --- | --- | --- |
| Device identity and envelope keys | Secret Service (`keystore.py`) | Ed25519, X25519 |
| Account key bundle (ASK, naming key, recovery public key, members) | Secret Service; on the Hub only as HPKE envelopes, one per device and one for the recovery key | HPKE X25519 / HKDF-SHA256 / ChaCha20-Poly1305 (python-cryptography) |
| Zone keys, one per zone and key generation | Hub, wrapped by the ASK | XChaCha20-Poly1305 (libsodium) |
| Records and blobs | Hub | XChaCha20-Poly1305, associated data binding account, zone, name, generation, version |
| Names, zone ids, blob ids | | HMAC-SHA256 under the naming key |
| Recovery key | the person; also this device's keyring, for Settings > Show… | 24 BIP-39 words, Argon2id (64 MiB, 3 passes) |

Joining: the new device commits to its half of the code; the first member
online posts the approvers' half (the same on every member, derived from the
ASK); the new device reveals; both show six digits over the account, the
device's keys and both halves. Revoking a device makes a new ASK chained to
the old one and new zone key generations for new writes.

## org.projectluma.ConnectSync1

Session bus, `/org/projectluma/ConnectSync1`; the full interface with the
JSON shapes is `data/org.projectluma.ConnectSync1.xml`. Settings, the shell
(approval card, status island), Valet and the launcher use it:

- state: `GetState`, `SetEnabled`, `SetCategory`, `SyncNow`, signal `StateChanged`
- turning on: `BeginSetup` (words and three positions to check), `FinishSetup`, `CancelSetup`, `RevealRecoveryKey` (polkit `org.projectluma.connect-sync.reveal-recovery-key`)
- this device joining: `RequestJoin`, `GetJoin`, `CancelJoin`, `JoinWithRecoveryKey`, signal `JoinChanged`
- other devices joining: `ListJoinRequests`, `ApproveJoin`, `DenyJoin`, signals `JoinRequested`, `JoinCleared`
- app data: `ListApps`, `SetAppEnabled`, `DeleteAppData`, `ApplyPending`, `Quiesce`, `ListConflicts`, signal `AppsChanged`
- devices: `ListDevices`, `RemoveDevice`
- restore: `GetRestore`, `GetAppState`, signal `RestoreChanged`

Errors are `org.projectluma.ConnectSync1.Error.<Code>` with a plain message
(`WrongKey`: "That key doesn't match this account").

Callers in a sandbox (Flatpak, ADR-052 store apps and capsules, snaps) are
refused on every method. `ApproveJoin`, `RemoveDevice`, `DeleteAppData` and
`SetEnabled(false)` need polkit `org.projectluma.connect-sync.manage`
(`auth_self_keep`), `RevealRecoveryKey` needs
`org.projectluma.connect-sync.reveal-recovery-key`.

## Hardening

- Incoming manifests: every path relative and normalised, nothing created
  below or through a symlink, symlinks that leave the folder dropped, file
  modes stripped of set-id bits; snapshots never follow a symlink.
- dconf: only `/<app id as path>/`, and never an app whose path is or holds
  `/org/gnome/desktop/`, `/org/gnome/shell/`, `/org/gnome/mutter/`,
  `/org/gnome/settings-daemon/`, `/org/freedesktop/`, `/org/projectluma/`,
  `/ca/desrt/` or `/system/`.
- Chunk boundaries come from a gear table keyed per zone; every stored piece
  is one ciphertext padded to a multiple of 64 KiB, the same whichever way
  it later travels.
- A removed device gets new keys and must be approved again; removing a
  device can replace the recovery key; a recovery-key join raises
  `RecoveryJoined` on every member.
- A file changed on two devices keeps both: the newer is current, the other
  stays as a labelled sibling record in the app's zone (`ListConflicts`).

## Command line

`luma-connect-sync status | setup | join | requests | approve | deny |
recover | revoke | apps | restore | app on|off|delete | sync | daemon`
(`--help` for details). It needs a Luma Connect sign-in
(`~/.local/share/luma/connect/device.json`).

## Tests

    cd tests && PYTHONPATH=..:. python3 -m unittest discover -p "test_*.py"

They run against `tests/fake_hub.py`, or against a real Hub with
`LUMA_SYNC_TEST_HUB=http://127.0.0.1:PORT` (the website repo's
`deploy/hub/connect-sync/dev-hub.mjs`). They need python-cryptography 50
(the `hpke` module) and libsodium. The two-VM proof is `tests/two-vm/`.
