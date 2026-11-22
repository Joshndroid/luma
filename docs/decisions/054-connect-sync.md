# ADR-054: Luma Connect Sync (whole-device sync and the Connect Sync API)

- **Status:** Accepted
- **Date:** 2026-11-22
- **Owner:** Project Luma
- **Review basis:** `luma-connect-sync` and `prairie_apps/connect_*` on main (72e450e3); `src/luma-continuity`; the live Hub at `/opt/luma-hub/server/local-hub` (read-only, 2026-11-22); ADR-051 (Luma Messages), ADR-052 (one application model); prior art in `PRIOR-ART.md` beside this draft
- **Revisit trigger:** more than 5,000 accounts with Sync on; OpenMLS used by every Luma client (the device-key join could then move into an account MLS group); a request to let a Luma server read synced content
- **Extends:** ADR-051 (account ID, device identity, recovery key), ADR-052 (app record, data folder, systemd scope, portal identity). **Replaces for new data:** the snapshot and collection services in `connect_sync.py`, which move onto this model service by service.

## User problem

Someone signs a desktop and a laptop into Luma Connect and turns on Sync. They expect the laptop to have the same apps, with the same state, settings and sign-ins. Today Connect syncs ten Luma apps by hand-written adapters and nothing else:

| Area | Today | How | Verified on hardware |
| --- | --- | --- | --- |
| Notes | Works | Hub-mediated; per-note deltas with etags, image blobs; long-poll feed | Dell edit reached a second device in 5.8 s (2026-10-17); 9 devices synced Notes in the last 7 days |
| Contacts, Calendar | Works | CardDAV/CalDAV against the Hub (Radicale), EDS as the client | Contacts on 3 devices this week |
| Photos | Works | SHA-256 manifest and blobs, plaintext on the Hub disk | 4 devices this week, 3,004 photos on the Hub |
| Clocks, Weather, Books (Leaf), Music servers (Tide) | Works | Account-wide collections with base-merge on the Hub; Tide passwords as sealed secrets | 4 devices this week; Tide's second-device convergence not recorded |
| Messages and call logs | Built, off | Logs sealed at rest with the Hub's own key | Last use September/October |
| Shared Notes and Tasks | Half-built | Cross-account documents sealed with the Hub key; `collaboration_broker.py` gives sandboxes token-free access | Unit and integration tests only |
| Phone link (luma-continuity, Android app) | Half-built | Peer-to-peer mutual TLS and an opaque relay; notifications, calls, texts | Phone-side; not a sync engine |
| Installed apps, app state, settings, wallpaper, shell, keyboard, Wi-Fi, keychain, browser, third-party API | Missing | None | None |

Everything is Hub-mediated over TLS and **nothing is end-to-end encrypted**. Notes, contacts, collections and photos are plaintext in `hub.sqlite` and on disk. Secrets, messages and shared documents are sealed with `session.key`, which sits on the same disk. About sixty `hub.sqlite.pre-*` copies since September keep old plaintext beyond account deletion. Each new app needs its own adapter, its own path in `luma-connect-sync.path` and its own Hub table. A third-party app has no way in at all.

## Constraints

- The Hub stores ciphertext. It may know account, device, zone, size and time. It may not know content, file names, record names, setting names or which Luma app a system zone belongs to.
- Most apps know nothing about Luma. Their state must sync without code changes, and without corrupting a database that is open.
- Devices differ (desktop, laptop, phone, tablet, TV; x86_64 and aarch64). A setting that is right on a laptop can be wrong on a TV.
- The Dell has no root; Phase 1 must run entirely as a user service.
- Turning Sync off never deletes anything on any device (the rule in `connect_services.py`).
- One droplet with 2 GB of memory runs the Hub, Keycloak, the website and dl today. Hub changes go in as patches, never `deploy-hub.sh`.
- Features are on by default with no opt-in walls. Nothing blocks an app waiting for an answer.

## Decision

### 1. Keys and encryption

1. **Device keys.** Each device has one Ed25519 identity key and one X25519 key in its Secret Service, named in the Hub `devices` table. The identity key is the same one ADR-051 registers for Messages; the credential is `luma1:<account id>:<device id>`. One device has one identity.
2. **Account sync key (ASK).** A random 256-bit key, made by the first device that turns Sync on. The Hub never holds it in the clear.
3. **Zone keys.** Every zone has its own random key, wrapped by the ASK and stored beside the zone. Records and blobs use XChaCha20-Poly1305 with a fresh nonce. The associated data binds zone, record and version, so the Hub can't move ciphertext between them. Record and file names are HMAC-SHA256 under the zone key.
4. **Envelopes.** The ASK is wrapped to each device's X25519 key with HPKE (RFC 9180; X25519, HKDF-SHA256, ChaCha20-Poly1305, the primitives of ADR-051's suite 0x0003). The Hub stores one envelope per device and one recovery envelope.
5. **Joining.** A new device signs in (Keycloak device grant, as today), registers its keys and asks to join. Any device that already syncs shows "ThinkPad wants to sync" with a six-digit code computed from both public keys. The new device shows the same code. Approve wraps the ASK to the new device. If no other device is available, the recovery key decrypts the recovery envelope instead. Account sign-in alone never grants keys.
6. **Recovery key.** 24 words (Argon2id, never sent to Luma), shown once when Sync is first turned on and stored wherever the person chooses. It is the same key ADR-051 promises for Messages backups, so an account has one recovery key. If every device and the recovery key are lost, synced data can't be recovered. The Luma account survives and Sync starts again empty.
7. **Revocation.** Revoking a device (the existing `devices/{id}/revoke`) deletes its envelope. The next device online makes a new ASK and re-wraps every zone key. New writes use new zone keys; old ciphertext stays as it is, since the revoked device had already read it.
8. **What the Hub sees:** account ID, device IDs and names, opaque zone IDs, the owning app ID of third-party zones (needed for quotas and revocation), record counts, sizes, change times, the writing device and IP addresses as the existing rate limits need them. **What it can't see:** any content, any name, which system category a zone is, settings and their values, app folder contents, passwords.

### 2. Data model

1. **Account → zones → records → blobs**, as in CloudKit. A zone belongs to one category or one app and has one key. A record is at most 256 KB of ciphertext with a server-assigned version. A blob is a content-defined chunk of 64 KB–4 MB, encrypted under the zone key and deduplicated within the zone by keyed hash. No deduplication happens across accounts.
2. **Change feed.** One sequence number per account. A device keeps one change token per zone and asks for records changed after it. Writes carry the version they replace; a stale write gets `409` with the current record, and the device resolves it. All merging is on devices, because only devices can read.
3. **Conflict rules by kind:**
   - *Settings:* one record per key; last writer wins by hybrid logical clock.
   - *Installed apps:* one record per app; the set is the union. Removing an app on one device doesn't remove it elsewhere.
   - *App folders:* snapshot trees (section 4).
   - *Documents that must merge:* an Automerge document stored as change chunks plus a periodic compacted snapshot.
4. **Files or records.** App folders, wallpaper images and other large data travel as chunked blobs under a manifest record. Settings, app lists, Wi-Fi networks, keychain items and API data travel as records.

### 3. Whole-device scope

The form-factor classes are **Computer** (desktop and laptop), **Handheld** (phone and tablet) and **TV**. A category syncs by default only between the classes marked here. Every row is a switch in Settings > Connect > Sync.

| Category | Computer ↔ Computer | Handheld ↔ Handheld | TV ↔ TV | Across classes |
| --- | --- | --- | --- | --- |
| Apps installed (ADR-052 records) | Yes | Yes | Yes | Offered, never installed automatically |
| Per-app state (data folder; opt-out per app) | Yes | Phase 3 | No | Only through the Connect Sync API |
| Settings (gsettings schema allow-list) | Yes | Yes | Yes | Shared keys only: language, region, clock format, units, accessibility |
| Appearance (dark, accent, text scale) | Yes | Yes | Yes | Dark mode and accent |
| Wallpaper | Yes | Yes | No | "Same wallpaper everywhere" switch, off by default |
| Shell layout (dock, pinned apps, tiling, quick settings) | Yes | Yes | Yes | No |
| Keyboard and input (layouts, input methods, user dictionary) | Yes | Yes | Layouts only | Layouts, dictionary |
| Wi-Fi networks and passwords | Yes | Yes | Yes | Yes, except networks marked "this device only" and 802.1X certificates |
| Sign-ins and passwords (keychain) | Yes | Yes | Yes | Yes |
| Browser (Viola: bookmarks, tabs, history, settings, extensions) | Yes | Yes | Bookmarks | Bookmarks, history, tabs |
| Files (Documents, Downloads, Desktop) | **No** | **No** | **No** | **No** |

- **Apps installed.** The record carries app ID, origin (Depot, Flathub, a distribution userland and its repositories, as ADR-052 records them), channel and hold state, never a version. A receiving device installs the newest release from the same origin for its architecture. A hold travels with the record. If the origin has no build for the receiving device, the app is listed as not available there. System modules and workspaces are not synced.
- **Settings.** Only allow-listed schemas and keys sync, each tagged with its classes. The list ships in the OS image (`/usr/share/luma/sync/settings.d/*.toml`). Anything machine-specific stays local: displays, power, touchpad calibration, audio devices, hostnames and per-device paths.
- **Keychain.** Items synced are those in the Luma collection that apps mark syncable, the Flatpak secret-portal app secrets (section 4), Wi-Fi secrets and online-account tokens that can be refreshed. Connect device tokens, SSH and GPG keys, and device identity keys never sync. Passkeys follow once the FIDO Credential Exchange format is settled.
- **Viola** is the first client of the Connect Sync API. Its profile is never copied as a folder.
- **Files are not part of Sync.** Photos remain their own service. Documents need a separate decision about a cloud drive and its storage cost.

### 4. App state for apps that know nothing about Luma

1. **What is synced.** The app's ADR-052 data folder (for a Flatpak, `~/.var/app/<id>`). For apps that write host dconf, also their dconf path. For Flatpak apps using the secret portal, also their per-app portal secret, so that `data/keyrings/` opens on the other device.
2. **Always excluded:** `cache/` and `.cache/`, `tmp/`, sockets, FIFOs, device nodes, `*.lock`, `lock`, `Singleton*`, `*.pid`, `.flatpak-info`, shader caches, and any file over 1 GB. The app's record may add exclusions (`sync.exclude`) or opt out entirely (`sync.folder = false`). Luma keeps a signed deny-list for apps that sync themselves (mail clients, browsers, Steam, cloud-drive clients).
3. **Quiet points.** A snapshot is taken only when the app's systemd scope has ended and 20 seconds have passed with no new launch. Reads are checked as stable (size and mtime before and after); a file that changes during the read aborts the snapshot. SQLite files and their `-wal` and `-shm` files are taken as one unit. An app that never exits (chat clients, tray apps) is marked "syncs when closed". Later it can call `ConnectSync1.Quiesce` to ask for a snapshot.
4. **Apply.** An incoming snapshot is staged beside the folder and swapped in by rename while the app isn't running. The ADR-052 launcher applies a staged snapshot before it starts the app, so a launch always opens the newest state. A running app is never touched. Its update applies at the next launch.
5. **Conflicts.** Each snapshot names its parent.
   - Fast-forward when the parent is the local base.
   - When both devices changed, merge file by file: a file changed on one side only takes that side. A file changed on both sides takes the newer version (SQLite units are treated as one file), and the other is kept for 30 days as "Version from Dell, 21 Nov".
   - If an app's state comes from a newer app version than the one installed, the app is updated first; the state is never applied to an older version.
6. **Budget.** 2 GB per app folder by default. Larger apps sync only after the person opts in.

### 5. The Connect Sync API (third-party)

1. **Surface.** The D-Bus interface `org.projectluma.ConnectSync1`, served by the host sync daemon. Apps reach it through Luma's portal, which identifies them by Flatpak instance or by ADR-052 SELinux category and scope, the verifier `collaboration_broker.py` already uses. A caller names zones and records only. It never sees a token, key, URL or path. Client libraries:
   - `LumaSync` in the LumaUI SDK: GObject introspection for C, Python, Rust and JS.
   - A Kotlin library for Android.
   - A TypeScript/WASM library for the web.
2. **Model.** CloudKit-style zones and records, with a CKSyncEngine-style engine in the daemon. The engine owns scheduling, batching, retries, the offline queue (durable, so an app can write offline and quit), change tokens and push. Calls:
   - `Save(zone, records, base_versions)` and `FetchChanges(zone, token)`.
   - The `ZoneChanged` signal.
   - Assets up to 100 MB.
   - An optional `Document` type backed by Automerge for data that must merge.
   - A high-level `Store` with a local SQLite mirror for apps that want put/get/changed and nothing else. Queries are local; the Hub can't index ciphertext.
3. **App identity.** A zone's owner is (app ID, publisher key from the Depot or Flathub store record). A sideloaded app that claims the same ID gets a different owner and can't read the real app's zones. Unsigned apps get a local developer zone that syncs only with Connect developer mode on.
4. **Quotas.** 100 MB of records and 1 GB of assets per app per account, both within the account's storage. Writes are capped at 60 a minute per app per device. Abuse limits are set per publisher.
5. **Permission UX.** No prompt. The install sheet says "Syncs its data with your devices". Settings > Connect > Sync lists every app with its usage, a switch, and "Delete synced data". Turning an app off stops sync and keeps local data. Removing an app keeps its zones for 30 days.
6. **Companions.**
   - *Android:* the Luma Connect Android app is a device with its own keys. It exposes a bound service to Android apps whose package name and signing certificate the store record lists.
   - *Web:* a web companion becomes a device only after approval on a Luma device, with a per-origin key and its own envelope, revocable like any device. Each is a later phase.
7. **Developer docs:**
   - What syncs and what Luma can see.
   - Quick start (the recipe app).
   - Zones, records and assets.
   - Documents (Automerge).
   - Offline behaviour and conflicts.
   - Quotas and errors.
   - Store record fields (`sync.*`).
   - Testing with two VMs and developer mode.
   - Android and web companions.
   - Deleting and exporting data.

```python
from gi.repository import LumaSync
store = LumaSync.Store.open("recipes")             # zone owned by this app's store record
store.put(recipe.id, {"title": recipe.title, "steps": recipe.steps}, assets={"photo": recipe.photo_path})
store.connect("changed", lambda s, ids: reload(ids))   # fires for edits from other devices
```

### 5a. Apps that sync across every device (game saves, libraries)

Amended 2026-11-22 for apps such as emulators and games.

1. **App zones are not bound to a device class.** The Computer, Handheld and TV boundary in section 3 applies only to whole-device sync of settings and layout. A zone created through the Connect Sync API reaches every approved device on the account by default. An app may limit a zone to classes it names.
2. **Large assets on demand.** Besides inline assets, a zone may hold *on-demand assets*:
   - **Storage.** Assets are chunked, deduplicated by content hash, encrypted per chunk with the zone key, and counted against the account's storage, not the per-app 1 GB.
   - **Limits.** Each asset may be up to 16 GB.
   - **What every device gets.** Metadata syncs to every device. The bytes download only when the app asks on that device, or when the person pins the item.
   - **Storage pressure.** A device that is short of space keeps metadata and evicts unpinned bytes, oldest first.
3. **Exclusive sessions.** An app can take a lease on a record (`Lease(zone, id, ttl)`), for example "playing this game". Another device that opens the same record sees who holds it and can take it over. The lease is advisory and never blocks.
4. **Conflicts that can't merge** (binary save files) keep both versions. The newer one becomes current, and the other stays as a labelled sibling ("from Living room TV, 21:04"), which the app shows the person until one is chosen. Nothing is silently overwritten.
5. **Worked example: an emulator.**
   - Zone `saves`, all devices, on by default: save files and save states as records with inline assets, using leases while playing.
   - Zone `library`, opt-in in the app: game entries, artwork and play history sync everywhere. The game files are on-demand assets; each device downloads only what is opened or pinned there.
   - The app's install sheet shows the "Syncs with your devices" chip. Settings shows both zones with their usage, and the person can turn the library off while keeping saves.

### 6. Server (Hub)

1. **Placement.** A Hub module, `server/local-hub/sync/`, with its own `sync.sqlite` and the path prefix `/api/sync/v1/`. It is shaped like ADR-051's delivery module, so it can move to its own host by changing one nginx upstream. It authenticates with the existing device tokens and `devices` table.
2. **Storage.**
   - Tables: `zones` (account, opaque ID, owner app, key envelope, usage), `records` (zone, keyed name, version, ciphertext or manifest, size, deleted, writer device), `envelopes` (device or recovery, epoch) and `changes` (account sequence).
   - Blobs live on disk with the photo free-space guard, moving to object storage on the second host.
3. **Change feed and push.** The existing `waitForChange` long-poll for computers. Phones get an empty-payload wake through UnifiedPush (microG's distributor, as Messages plans). TVs use the long-poll.
4. **Rate limits.** The Hub's `store.limit`: 600 writes and 50 MB an hour per device, and per-app limits for API zones.
5. **Residency.** Data lives in the droplet's region. Encryption makes it content-free, but metadata is still personal data and the privacy notice says where it lives.
6. **Deletion and export (GDPR, CRA).**
   - Deleting the account or the Sync data removes zones at once and blobs within 24 hours.
   - Hub backups expire within 30 days. The `pre-*` copies of `hub.sqlite` get the same expiry.
   - Export is a plaintext archive made on a device. The Hub can export only ciphertext and metadata, and says so.
   - Under the CRA, E2EE on by default is the secure default; sync protocol versions are listed in the SBOM and vulnerability notices.
7. **Scale.** Launch: hundreds of accounts. Design target: 10,000 accounts × 4 devices, 500 MB each on average (5 TB), 50 writes a second at peak. Move to the second host (Postgres and object storage) at whichever comes first: 2,000 accounts with Sync on, `sync.sqlite` over 5 GB, or blobs over 50 GB.

## Alternatives rejected

- **Keep Hub-readable sync and add E2EE later.** Every record would have to be migrated, and the promise would not be real at launch. Retrofitting costs more than starting right.
- **One MLS group of the account's own devices for keys (ADR-051).** It gives better revocation, but OpenMLS is not yet in the Python clients. HPKE envelopes do the job now; the revisit trigger covers the move.
- **Escrowed keys (Luma can recover).** No HSM exists on a droplet, and escrow makes the Hub able to read everything.
- **Syncing app folders live (Syncthing or Dropbox style).** It corrupts open databases. Quiet points are what Steam Cloud proved.
- **Requiring every app to adopt an API.** No third-party app would sync on day one.
- **Syncing whole home folders or Documents.** That is a cloud drive, with its own cost and conflict model; it needs a separate decision.
- **Electric, Replicache or a server CRDT relay as the engine.** They need a server that reads the data.

## Tradeoffs

- A person without a recovery key who loses every device loses synced data. The setup screen says so plainly.
- The Hub can't search, dedupe across accounts or repair data. Bugs in the device engine are the only bugs that matter, so the engine gets the test budget.
- Folder sync assumes one device uses an app at a time. Two open copies of the same app produce versions, not merges. Apps that need merging use the API.
- Third-party zone owners are visible to the Hub, which then knows which apps use sync. Depot already knows which apps someone installed.
- CalDAV/CardDAV and E2EE pull against each other. Standard clients (Thunderbird, DAVx5, iPhone) need a server that reads cards and events. Calendar and contacts stay labelled "not end-to-end encrypted" until that is decided.

## Order of work

1. **Phase 1: two computers stay in sync for apps and app state (target: 3 weeks).**
   - Ships:
     - the key hierarchy, device join by code, and the recovery key;
     - the Hub sync module, as a patch;
     - a user-level `luma-connect-syncd` that syncs installed Flatpak apps (user and system installs, Depot and Flathub; receivers install with `--user`);
     - Flatpak app folders, with the dconf paths and portal secrets from section 4, at quiet points;
     - the deny-list and the 2 GB budget;
     - apply at launch for Luma's launcher and `flatpak run`;
     - Settings > Connect > Sync rows for Apps and App data, with a per-app switch.
   - Not in Phase 1: settings, Wi-Fi, keychain, Viola, the API.
   - Test:
     1. Unit and fault tests with a fake Hub.
     2. Two nightly VMs on luma-build, scripted: install, edit while closed, edit while the receiver is running, conflict, revoke, recovery-key join. A canary string must never appear in `sync.sqlite`.
     3. The Dell XPS (a user-level preview through drop-ins, as for auto-sync in October) and the ThinkPad (or a VM if it is busy), with eight chosen Flathub apps covering keyfile settings, SQLite, many small files and secret-portal sign-ins.
   - Passing:
     - an app installed on one appears on the other within 2 minutes;
     - closed-app state arrives within 60 seconds of the next quiet point;
     - zero mismatches by hash over a week of daily use;
     - idle requests no higher than today's (about 15 an hour on mains).
2. **Phase 2: settings, appearance, wallpaper, shell layout, keyboard; keychain and Wi-Fi (Wi-Fi through a system helper); ADR-052 apps beyond Flatpak; move Clocks, Weather, Leaf and Tide onto zones and retire their plaintext tables.**
3. **Phase 3: the Connect Sync API, docs and quotas; Viola as its first client; Notes and Photos onto encrypted zones; handheld app folders; TV.**
4. **Phase 4: the Android and web companion paths, passkeys, the second Hub host, and the decision on Calendar and Contacts.**

## Defaults pending the owner's confirmation

These are decided as below unless overruled. None of them blocks Phase 1.

1. **Calendar and contacts** move to end-to-end encrypted zones for Luma devices. A per-account CalDAV/CardDAV bridge stays available for other clients (Thunderbird, iPhone) as an explicit opt-in. Turning it on tells the person that the Hub can then read calendars and contacts.
2. **Documents are not synced.** A cloud drive is a separate decision.
3. **No escrow.** Losing every device and the recovery key loses synced data. Setup makes saving the recovery key a required step, and every approved device can show it again.

## Phase 1 note

"Apps installed" covers Flatpak apps and apps installed through the ADR-052 store. The store record (origin, userland, resolved package list, vendor repositories) is what syncs. The receiving device rebuilds the app's tree from its own repositories and never copies binaries.

