# luma-connect-sync

Phase 1 of ADR-054 (`docs/decisions/054-connect-sync.md`): two computers keep
the same apps and the same closed-app state, end-to-end encrypted, through
the Luma Connect Hub's `/api/sync/v1/` (website repo,
`deploy/hub/connect-sync/`). It runs as a user service.

## What it syncs

- **Apps installed** (zone `apps`): one record per Flatpak (remote name and
  URL, branch, hold) and per ADR-052 store app (id, package, userland, the
  vendor hosts it needed). The receiving computer installs Flatpaks with
  `--user` from a remote it already trusts at the same URL, and rebuilds
  store apps from its own repositories for that userland with its own
  installer (`store_rebuild.py`); no binary, repository file, key or command
  comes from the record. A store app that needed a vendor's repository is
  listed, not installed. The set is a union: removing an
  app on one device doesn't remove it elsewhere.
- **App data** (zone `appdata:<owner>:<app>`, owner = the Flatpak remote's
  URL and signing keys, or the store app's userland; an app from an unsigned
  source stays on its device): the app's data folder
  (`~/.var/app/<id>`, or the ADR-052 data folder), its host dconf path, and,
  through the `portal:<owner>:<app>` record, the secret portal's per-app secret, so
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
| Device identity and envelope keys | `systemd-creds --user` sealed files in `~/.local/share/luma/sync/keys/` (`keystore.py`); never the login keyring | Ed25519, X25519 |
| Account key bundle (ASK, naming key, recovery keys' public halves, members, removed devices, signer) | sealed files as above; on the Hub only as HPKE envelopes, one per device and one for the recovery key | HPKE X25519 / HKDF-SHA256 / ChaCha20-Poly1305 (python-cryptography), Ed25519-signed from epoch 2 |
| Zone keys, one per zone and key generation | Hub, wrapped under HKDF(ASK) | XChaCha20-Poly1305 (libsodium) |
| Records and blobs | Hub | XChaCha20-Poly1305 with a format byte, associated data binding account, zone, name, generation, version |
| Names, zone ids | | HMAC-SHA256 under the naming key |
| Blob ids | | HMAC-SHA256 under the zone key of the generation |
| Recovery key | the person; sealed on the device that made it, for Settings > Show… | 24 BIP-39 words, Argon2id (64 MiB, 3 passes) → X25519 envelope key and Ed25519 auth key |

The wire format is `docs/operations/connect-sync-wire.md` (format 2).

Joining: the new device commits to its half of the code; the first member
online posts the approvers' half (the same on every member, derived from the
ASK); the new device keeps the first half it sees and reveals once; the
approver checks the reveal opens the commitment; both show six digits over
the whole transcript. The person confirms on the new device that both codes
matched (`ConfirmJoin`, `luma-connect-sync join-confirm yes`). The approver
checks the code and wraps the key from one and the same request, answers at
most one open request per device and three new ones a day, and shows a card
for each. The approver signs a membership statement and the envelope; the new
device accepts only an envelope from a member that matches its code, after
the person's confirmation. Members are the signed bundle plus signed statements, never the
Hub's list.

Revoking a device makes a new ASK chained to the old one, signed by the
remover, listing the removed device for good, and new zone key generations;
devices refuse to write with an older one (for every zone they have seen). If
the removed device held the recovery words, the recovery key is replaced too:
the new words stay sealed on the device that replaced them, and `GetState`
shows `new_recovery_key` until the person reads them (`RevealRecoveryKey`,
`luma-connect-sync recovery-key`) and acknowledges. A device that joined with
the old words while the removal was pending doesn't get the new key.

Every request that changes something on the Hub is signed with the device
key (sealed, unreadable to apps), so the Luma Connect token in `device.json`,
which apps with home access can read, can't change or delete synced data.

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
refused on every method. Setup, join, approval, removal, recovery and
delete-everything calls answer only a login session, the shell or Settings
(by the caller's cgroup). `ApproveJoin`, `RemoveDevice`, `DeleteAppData` and
`SetEnabled(false)` need polkit `org.projectluma.connect-sync.manage`
(`auth_self_keep`), `RevealRecoveryKey` needs
`org.projectluma.connect-sync.reveal-recovery-key`. The bus name is never
queued for. `JoinRequested` carries no code; callers read it with
`ListJoinRequests`.

## Hardening

- Incoming manifests: every path relative and normalised, owner and app
  checked, nothing created below or through a symlink, symlink targets with
  `..` or `/` dropped, piece ids checked, file modes stripped of set-id bits.
- Files are opened beneath the folder one component at a time with
  `O_NOFOLLOW` and checked with `fstat`; incoming files are copied from the
  verified descriptor.
- dconf: only `/<app id as path>/`, and never an app whose path is or holds
  `/org/gnome/desktop/`, `/org/gnome/shell/`, `/org/gnome/mutter/`,
  `/org/gnome/settings-daemon/`, `/org/gnome/terminal/`, `/org/gnome/ptyxis/`,
  `/org/gnome/online-accounts/`, `/org/gnome/crypto/`, `/org/gnome/system/`,
  `/org/gnome/login-screen/`, `/org/freedesktop/`, `/org/projectluma/`,
  `/ca/desrt/` or `/system/`.
- Pieces: files over 1 MiB in fixed 1 MiB blocks padded with random blocks
  to the Padmé size of the file; small files in packs padded to Padmé; no
  compression; uploads in random order; every piece at most 4 MiB.
- Deletions are sealed tombstones naming their writer; a Hub-set deletion
  flag is ignored. Hub answers are capped and redirects are refused.
- A removed device gets new keys and must be approved again; a recovery-key
  join needs the recovery key's signature on the Hub and raises
  `RecoveryJoined` on every member; deleting all synced data needs the
  recovery key.
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
