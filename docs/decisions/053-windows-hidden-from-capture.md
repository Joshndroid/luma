# ADR-053: Windows hidden from screen capture

- **Status:** Accepted
- **Date:** 2026-11-21
- **Owner:** Project Luma
- **Review basis:** Mutter 50.4 screen cast and paint code with Luma patches 0001-0037; GNOME Shell 50.3 with Luma's share picker (0149, 0353, 0357); macOS `NSWindow.sharingType` and ScreenCaptureKit, Windows `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` as of November 2026
- **Revisit trigger:** an upstream Wayland or portal protocol for capture exclusion; a capture path that does not paint through Clutter (hardware planes in captures, a compositor-side encoder)

## User problem

An app that records or streams the display it is on captures itself. Streamy showing a display widget of its own monitor fills the stream with copies of its preview, each inside the last. A recorder's controls, a teleprompter or a password field have the same problem in a smaller form: they belong on the screen and not in what is shared. macOS and Windows let an app mark such a window; Linux desktops do not, and xdg-desktop-portal's ScreenCast has no way to say "everything but this window".

## Decision

1. **A window can hide itself from capture.** Hidden windows stay on the screen with their input, and captures of the screen show what is behind them: monitor and area screen casts, and screenshots of the screen or an area. Windows transient for a hidden window (its dialogs, menus, popovers) are hidden with it.
2. **A capture of the window itself still shows it.** A window screen cast the person picks, or a screenshot of that window, is the window by choice. Luma's share picker marks the window's tile "Hidden from screen sharing", and screen tiles leave it out, so the picker shows what each choice shares.
3. **No permission.** Hiding only removes content from captures, so any app may do it for its own windows, at any time. An app cannot hide another app's window. Hidden windows also stay out of remote desktop streams of a monitor; that is the same capture and the same choice the app made.
4. **Client API.** Wayland clients use `luma_window_capture_v1` (`src/luma-platform/protocols/luma-window-capture-v1.xml`): `get_window_capture(wl_surface)` and a double-buffered `set_visibility(visible|hidden)`, a separate global like `luma_window_shape_v1`, so Luma never collides with upstream's protocol versions. Xwayland clients set the `_LUMA_WINDOW_HIDDEN_FROM_CAPTURE` CARDINAL property to 1. LumaUI wraps the protocol as `luma_window_set_hidden_from_capture()` (C) and `AppWindow.set_hidden_from_capture()` (Python); elsewhere these do nothing and return false.
5. **Mechanism.** `clutter_actor_set_hidden_from_capture()` makes an actor, with its effects and children, skip paints that carry `CLUTTER_PAINT_FLAG_CAPTURE` (mutter 0036 sets it on every paint of the stage outside its views), including paints through a `ClutterClone` of it or of its descendants. Mutter sets it on a window's actor from `MetaWindow:hidden-from-capture`. Painting the window actor alone, which window casts and window screenshots do, carries no such flag.
6. **No cost without a hidden window.** Monitor streams copy the screen (the scanout buffer or a blit of the view) when they can. They paint the stage instead only while a hidden window is mapped on that monitor, or shown through a mapped clone anywhere (the overview). `meta_compositor_is_capture_hidden_in()` answers that with a counter check when no window is hidden. Area streams and screenshots already paint the stage.

## Consequences

- Streamy hides its windows by default ("Hide Streamy from screen capture" in its menu), so display widgets of its own monitor show the desktop without the mirror.
- While a hidden window is on a monitor, that monitor's DMA-BUF streams cost one stage paint per frame instead of a copy, as they already do for monitors spanning several outputs.
- Content an ancestor's offscreen effect cached during a screen paint is reused by a capture paint. Window actors have no such ancestor in Luma's Shell; an effect added to the window group would have to repaint for captures.
- An app that wants to be captured again calls the API with false, or destroys the protocol object.

## Tests

`tests/mutter/window-capture/run.sh` (headless GNOME Shell, two virtual monitors, PipeWire): no pixel of a hidden Wayland window, of its dialog or of a hidden Xwayland window in monitor or area streams or screenshots, including the overview and a move to the other monitor; the window's own cast still shows it; per-monitor copy decisions; `MODE=perf` measures a monitor stream with no hidden window, one elsewhere and one on that monitor. `src/luma-platform/tests/test-window-capture.c` covers the kit without the protocol.
