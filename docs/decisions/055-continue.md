# ADR-055: Continue: the desktop session follows you

- **Status:** Accepted
- **Date:** 2026-11-23
- **Owner:** Project Luma
- **Review basis:** Mutter 50.4 (`MetaWindow::configure`, `MetaWindowConfig`, `xx_session_manager_v1` in `src/wayland/meta-wayland-xdg-session*.c`, `xdg_toplevel_tag_v1`); GTK 4.22.4 (`gtkapplication.c`, `gtkapplication-wayland.c`, `gtkapplication-dbus.c`: save and restore built but private, signals compiled out); gnome-session 50 (no `RegisterRestore`); GNOME Shell 50.3 with Luma's patches; ADR-051, ADR-052, ADR-054 and `luma-connect-sync` on main; macOS state restoration and Handoff, Windows "Restart apps" and Phone Link, as of November 2026
- **Revisit trigger:** `xdg_session_manager_v1` or `xdg_toplevel_tag_v1` declared stable upstream; GTK making its save and restore API public; a toolkit (Qt, SDL, Electron) adopting either protocol; process checkpointing that survives a GPU and a compositor connection
- **Extends:** ADR-054 (zones, records, device classes, member keys), ADR-052 (app records, scopes, data folders), ADR-053 (window state the compositor owns)

## User problem

Someone closes the work laptop, takes a Surface out of the bag and turns it on. They expect the windows they had: the same apps, in the same places, the same one in front, the same ones behind and minimized, the same documents with the unsaved text still in them. One question at most: "Continue from ThinkPad?". On a single device the same is expected after logging out, an update restart, a crash or a power cut.

Today nothing of the session survives. Mutter places every window afresh. GTK 4.22 contains a full save-and-restore implementation, but its API is private and its signals are compiled out; gnome-session has no `RegisterRestore`; Mutter's `xx_session_manager_v1` is behind a debug switch and stores only geometry, workspace and minimized per client-named toplevel, with no stacking, focus or monitor identity, and only for clients that implement it. Connect Sync (ADR-054) syncs installed apps and closed-app data, but a running app's data is never taken, so the session on the laptop is exactly what does not travel.

## Decision

### 1. The snapshot

A session snapshot is one JSON document, format 1. The shell takes it; nothing in it is read back from the Hub in the clear.

- **Session:** snapshot ID (128-bit random), device ID and name, device class (ADR-054 §3), time taken, reason (`lock`, `suspend`, `lid`, `logout`, `shutdown`, `periodic`, `request`), workspace count and the active workspace, expiry.
- **Monitors:** connector, vendor, product, serial, built-in, primary, logical rectangle, scale and work area. A monitor's identity is vendor, product and serial; the connector is only a hint.
- **Windows,** in stacking order from bottom to top, one entry per toplevel (dialogs and other transients are not entries; the app re-creates them):
  - app ID (ADR-052 app record ID or desktop ID), the window's kind (Wayland, X11, Android) and its key (section 3);
  - workspace (or all workspaces) and monitor;
  - frame rectangle, absolute and normalised to its monitor's work area (four fractions);
  - the floating rectangle, absolute and normalised, kept for maximized, tiled and fullscreen windows so that leaving those states returns to it;
  - state: floating, maximized, tiled left or right (with the tile fraction), fullscreen, plus minimized, always on top and on all workspaces;
  - the window title (used for matching; encrypted with the rest);
  - whether it had focus. Exactly one window has focus, or none.
- **Apps:** for every app with a window, and for windowless apps the person started (background and tray apps): the app ID, how it was started (desktop ID; origin from the app record), and the app's state when the app provides one (section 3): an opaque app-state blob, and per window a list of documents, each `{uri, dirty, title}` and, for a dirty or never-saved document, the unsaved content as a blob.
- **Size.** The record is at most 256 KB (ADR-054 §2.1); app-state blobs and unsaved content are blobs in the same zone, 8 MB per app and 32 MB per snapshot. Above that a document is listed without its unsaved content, and the restore says so.

### 2. The compositor

Mutter already has the hook Continue needs: `MetaWindow::configure` is emitted once per window with its initial `MetaWindowConfig`, for Wayland at the initial commit before the first configure is sent and for X11 before the window is mapped. A handler that sets the position marks the window placed, so Mutter's own placement never runs. The shell restores from there; no window is moved after it has been seen.

Luma's Mutter adds what is missing (`patches/mutter/0048`):

1. `meta_window_config_set_maximized()`, `meta_window_config_set_tile_mode()` and `meta_window_config_set_tile_monitor()` are exported, so a configure handler can open a window maximized or tiled, not only at a position or fullscreen.
2. When a configure handler gives both a position and a maximized, tiled or fullscreen state, the position becomes the window's floating rectangle first, so unmaximizing a restored window returns it to where it was before.
3. `MetaWindow:restored-from-session` is true for a window its own client restored through `xx_session_manager_v1`; the shell leaves such a window's geometry alone and only orders and focuses it.
4. `xx_session_manager_v1` is on by default. Mutter keeps the upstream implementation and its file under `$XDG_DATA_HOME/mutter/`. Clients that name their toplevels through it get Mutter's own exact restore on the same device; the shell's snapshot still covers stacking, focus, monitors and every other device.

Luma does not add a client protocol now. Mutter already implements `xx_session_manager_v1` and `xdg_toplevel_tag_v1`; a third protocol would only rename them. The shell reads a window's tag (`meta_window_get_tag()`, Xwayland's `WM_WINDOW_ROLE`) as its key where a client sets one. When either protocol is declared stable, Luma follows the stable name and keeps the experimental one for a release.

### 3. Apps

Every app gets geometry, state, stacking and focus without doing anything. What an app needs to do is give each window a key and, if it can, its state.

**Window keys,** in order of preference:

1. a LumaUI key (`luma_window_set_restore_key()`, below);
2. the toplevel tag (`xdg_toplevel_tag_v1`) or X11 `WM_WINDOW_ROLE`;
3. the title, exact and then by prefix up to the app's own suffix (" — Text Editor");
4. the order in which the app opens its windows.

The shell matches a new window to a snapshot entry of the same app by the best key both have, and only once.

**The LumaUI API** (C `luma-restore.h`, Python `AppWindow`; one semantic API for LumaUI and plain GTK 4 apps):

- `luma_window_set_restore_key (window, key)`: a key the app can find the window by again, for example `main` or `document:<id>`. The kit tells the shell over `org.projectluma.Session1.SetWindowKey`; the shell accepts it only from the client that owns the window (its unique bus name is the window's `gtk_unique_bus_name`). It is sent synchronously before the window first maps, so the configure handler already knows it.
- `LumaRestore::save (app_state, window_states)`: the shell asks every LumaUI app for its state when it takes a snapshot (`Session1.SaveRequested`); the kit collects a `GVariantDict` per app and per window and returns it with `Session1.SaveState`. Apps put the open documents in it (`luma_restore_add_document (dict, uri, dirty, unsaved)`), the view (scroll position, selection, tabs, the page shown) and anything else that makes the window the same. The kit answers within 500 ms or the snapshot goes ahead without that app.
- `LumaRestore::restore (reason, app_state)` and `LumaRestore::restore-window (key, window_state)`: when the shell starts an app to restore it, the kit asks `Session1.TakeRestore` once at startup and re-creates the windows in the snapshot's order, each with its key, before the app's own `activate`. The reason is `restore` (same device), `continue` (from another device) or `recover` (after a crash). The shell identifies the caller by its process's systemd scope or Flatpak instance (ADR-052), so an app only ever receives its own state.
- Python: `AppWindow.restore_key`, `AppWindow.save_state() -> dict`, `AppWindow.restore_state(state)`, and `luma_appkit.restore.enable(app, open_window)`.

First-party LumaUI apps adopt it: Settings, Filer, Text, Notes, Viewer, Terminal, Tide, Leaf, Depot and Monitor, then every prairie-core app. GTK 4 apps that are not LumaUI get keys from their titles. GTK's own private implementation is not enabled: it stores state in files the shell can't verify and depends on gnome-session.

**Everything else** (GTK 3, Qt, Electron, X11, `.deb`, `.rpm` and Flatpak apps under ADR-052, Android apps under Luma's Android runtime) is relaunched from its app record and placed exactly; its windows are matched by tag, role, title and order. An app that re-opens its own last documents (most editors and browsers) then shows them; one that doesn't opens empty, and the restore says so. Android apps are relaunched on their last activity; their saved instance state stays on the device they ran on.

### 4. Snapshots and restore on one device

1. **When.** On lock, suspend (a logind delay inhibitor, at most 2 s), lid close, log-out and shutdown, and at most once a minute while windows change. Taking a snapshot reads Mutter's window list; no app is interrupted beyond the `SaveRequested` call.
2. **Where.** `$XDG_STATE_HOME/luma/session/` (mode 0700), the last snapshot and the one before it. Unsaved content is stored there too, never in `/tmp`.
3. **Restore.** At login, the shell restores the last snapshot when the previous session ended by log-out, restart or shutdown, or ended without a clean shutdown (crash, power cut: the periodic snapshot is at most a minute old). Settings › Windows & Workspaces › Reopen windows after restart turns it off; an app's own page has Reopen with your windows, which leaves it out of both restore and Continue. A snapshot is restored once; an app that crashes while being restored is not started again for that snapshot.
4. **Order.** Apps start in parallel; each window is placed at its first configure; when the last expected window has appeared, or after 20 s, the shell restores stacking from the bottom up, minimizes, switches to the saved workspace and focuses the saved window. Windows that appear later (an app still installing) are placed when they come and don't take focus.

### 5. Continue on another device

1. **The record.** The snapshot is written to Connect Sync as one record per device in a `continue` zone (ADR-054 zones: the Hub sees only an opaque zone, a size and a time). The payload is encrypted again with a fresh key that is HPKE-wrapped to each eligible member device's X25519 key from the signed member bundle: Computers and Handhelds. TVs and removed devices never receive the key. The device writes on lock, suspend, lid close and log-out, and the periodic snapshot at most every 5 minutes while online. A suspend waits for the write up to the inhibitor's limit; a device that was offline writes when it next has a network, and its record is marked older than it is.
2. **Expiry.** A snapshot record is valid for 72 hours. Readers ignore an expired record; its writer overwrites or deletes it. Removing a device deletes its record, and later snapshots are not wrapped to it.
3. **The offer.** When a device is unlocked, resumes or logs in, it reads the newest unexpired record of the other members. If that record is newer than this device's own last use, it shows **Continue from ThinkPad?**, naming the apps, with **Continue** and **Not now** of equal weight. Nothing restores without that tap, and nothing about the record (not even app names) is shown on the lock screen. A device that is locked never applies a snapshot. Not now hides the offer until a newer snapshot arrives. Settings › Luma Connect › Sync › Continue has two device-local switches: offer windows from other devices here, and let other devices continue from this one (off deletes this device's record); Saved windows · Clear deletes the local snapshot and the record.
4. **Over an existing session.** If the target has no open windows, the snapshot opens on its workspaces. If it has windows, the snapshot opens on a new workspace after the last one and the shell switches to it; nothing the person had open on the target moves or closes. An app that is already running and can't open another instance gets its windows restored through LumaUI (as extra windows) or is focused and listed in the result.
5. **App state that hasn't arrived.** Before restoring, the target runs one Connect Sync cycle so that closed-app data (ADR-054 §4) is current. Running apps on the source had no quiet point; only LumaUI apps carry their state in the snapshot. The others open with the data they last synced, and the result names them.
6. **Apps missing on the target.** An app with an `apps` record is installed through app sync first (Depot, Flathub or the ADR-052 store, newest release for the target's architecture) and its windows are placed when it starts, for up to 15 minutes. An app with no build for the target is listed as not available.
7. **The source.** The next time the source is unlocked it shows "Your windows continued on Surface", and if the target's snapshot is newer it offers to continue from there: the same offer in the other direction.

### 6. Other screens and device classes

- **Monitors.** A window goes to the monitor with the same vendor, product and serial. Without one, it goes to the monitor with the same role (built-in, or the same position relative to the built-in), then to the primary monitor. Its rectangle is the normalised rectangle on that monitor's work area, kept at least at the app's minimum size and inside the work area; maximized, tiled and fullscreen windows stay so on the new monitor.
- **Computer ↔ Computer:** everything above.
- **Computer → Handheld (tablet, phone):** the shell runs every app full screen there, so geometry, stacking and minimized don't apply. The offer is **Continue <app>** for the app that had focus, opening it with its state and documents; the other apps of the snapshot open in the app switcher in their stacking order, unstarted until chosen.
- **Handheld → Computer:** the handheld's foreground app and its state open on the computer at the app's default size, beside what is there.
- **TV:** no snapshot is wrapped to a TV. A TV receives a separate `continue-tv` record with only media apps that have a TV presentation and their playback state (the item and position), never titles, documents or sign-ins.

### 7. Security and privacy

- Window titles, document names and content leave the device only inside the end-to-end encrypted record and blobs (ADR-054 §1), and only for devices of the account's signed member list.
- The `Session1` interface is the shell's. `SetWindowKey` is accepted only from the window's own client; `SaveState` only from a process whose scope is a running app, for that app; `TakeRestore` returns only the caller's own app state, once. Sandboxed apps reach `Session1` through these three methods only; taking or applying a snapshot is the shell's own decision.
- The local snapshot is the person's own data, mode 0600 in a 0700 directory, on ADR-052's protected-path list, so no app (sandboxed or not under ADR-052) can read another app's state from it.
- Apps can exclude a window (`luma_window_set_restore_key (window, NULL)`) or the app (`sync.continue = false` in its store record). Password managers, the keyring and the Connect device approval never appear in a snapshot.
- Applying a snapshot starts apps the person had open on their own device; it never runs commands from the record. Apps are started from the target's own app records; an app ID with no local record and no `apps` record is skipped.

### 8. Failure handling

A restore that couldn't do everything says so once, in one notification: "Reopened 9 of 11 windows" with the reasons per app (not installed yet, not available on this device, didn't open in time, opened without its documents, opened on another monitor). The notification opens a list with **Open** for apps that can still be started. A restore never retries in a loop, and a snapshot that fails to decrypt or parse is dropped with a log line.

## Alternatives rejected

- **Process checkpointing (CRIU).** A GUI app holds a GPU context, a compositor connection and D-Bus names that can't be checkpointed or moved between machines. Revisit with the trigger above.
- **Enabling GTK's private save and restore.** Its state files are written by the app where the shell can't verify them, its restore reason comes from gnome-session, and it covers GTK only. LumaUI's API takes the same shape and moves to GTK's when that is public.
- **A new client protocol now.** Mutter already implements `xx_session_manager_v1` and `xdg_toplevel_tag_v1`; the keys LumaUI needs go over D-Bus with an ownership check, which works for GTK 3, GTK 4 and Python without a toolkit patch.
- **Restoring automatically over another device's session.** It would move or close what the person has open on the target. The offer and the new workspace keep that from happening.
- **Copying open files with the snapshot.** Documents sync device to device opt-in per folder (ADR-054 Defaults 4). Continue carries unsaved content only; a saved file that isn't on the target is listed, and Phase C revisits copies.

## Tradeoffs

- Apps that don't use LumaUI reopen in place but not always with their documents; the restore names them.
- A Mutter build is needed for maximized and tiled restores; position, size, fullscreen, minimized, workspace, stacking and focus work with the configure signal alone.
- A suspend waits up to 2 s for the snapshot and its upload.
- Three-day expiry means a laptop closed for a long weekend offers nothing on Tuesday.

## Order of work

1. **Phase A: one device.** Mutter 0048; the shell's snapshot, restore and `Session1` (`patches/gnome-shell/0392`); LumaUI `luma-restore` (C and Python) and its adoption by first-party apps; the Reopen windows after restart switch. *Shown:* a headless shell test that opens Wayland, GTK and X11 windows in every state on two monitors, restarts the shell and measures each window's rectangle, state, workspace, stacking and focus; then a VM that logs out and reboots with first-party apps open.
2. **Phase B: computers.** The `continue` zone and its per-device wrap in `luma-connect-sync`; the offer; monitor mapping; the new-workspace rule; install-then-place; the result notification. *Shown:* two VMs with different screens: windows on one reappear on the other after Continue, with no title or app ID in the Hub database.
3. **Phase C: more state.** `xdg_toplevel_tag_v1` keys for toolkits that adopt it; `ConnectSync1.Quiesce` at snapshot time for apps that can flush their data while running; Android activity relaunch; copies of open files that aren't in a synced folder (Folders, ADR-054 Phase 5).
4. **Phase D: handhelds and TVs.** The single-app offer on phones and tablets, the handheld-to-computer offer and the TV record.

The offer, the result list and the Settings rows follow the design handoff (design-center `mockups/continue/`): a standard banner that turns into progress and then into the result list in place, a lip card and bottom drawer on phones, the TV home's resume hero.
