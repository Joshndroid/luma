# Connect Sync: operations (ADR-054 Phase 1)

Device: `src/luma-connect-sync` (user service `luma-connect-syncd`, command
`luma-connect-sync`). Hub: website repo `deploy/hub/connect-sync/`, the
`/api/sync/v1/` module with `sync.sqlite` and `sync-blobs/` in the Hub's
state folder.

## What the Hub holds

Ciphertext and metadata only: device public keys and class, key envelopes
(one per device, one for the recovery key), join requests, zones (opaque id,
key generations wrapped by the account key, record count, bytes), records
(keyed name, version, sequence, ciphertext), blobs (keyed id, size). No
names, settings, file paths or contents. A Hub backup of `sync.sqlite`
holds nothing readable; it still holds personal metadata and expires with
the other Hub backups.

## Keys a person holds

- Each device: its keys and the account key bundle in its Secret Service
  (three "Luma Connect Sync" items). A locked keyring stops Sync on that
  device; nothing is written elsewhere.
- The recovery key: 24 words shown once at turn-on (and in Settings behind a
  password check). Luma can't recover it. Losing every device and the words
  loses synced data; the account itself is unaffected.

## Support cases

| Case | What to do |
| --- | --- |
| A device was lost or sold | Remove it from another device (Settings > Connect > Sync > Devices, or `luma-connect-sync revoke <id>`). The others get a new account key at once. |
| A device was away while another was removed twice | It can't follow two key changes; it shows "join again". Join with a code or the recovery key. |
| "That key doesn't match this account" | The words belong to another account or are mistyped; numbering and capitals don't matter. |
| Storage full | New changes stay on the device; Settings shows the usage. Per-app "Delete synced data" frees space without touching the device. |
| An app's state conflicted | The newer copy is used; the other is in `~/.local/share/luma/connect-sync/versions/<app>/` for 30 days (`luma-connect-sync conflicts`). |
| Delete everything synced | `DELETE /api/sync/v1/account` from a member device (Settings > Delete synced data); account deletion does the same through the Hub's hook. |

## Limits

600 writes and 50 MB an hour per device, 10 GB per account (Hub config
`sync.limits`), 2 GB per app folder on the device unless the person opts in,
files over 1 GB never sync. Idle: one long-poll every 240 s (15 requests an
hour); a cycle with no change on the Hub and none on the device makes no
request.
