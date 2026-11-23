// SPDX-License-Identifier: Apache-2.0
// gnome-shell --automation-script for the luma_window_shape_v1 test (run.sh).
// LUMA_SHAPE_TILER=none: real pointer drags from every edge and corner
// through a Clutter virtual pointer, maximize, Mutter's half tiles
// (Super+Left/Right), a tiling manager's tiles through
// meta_window_set_externally_tiled() at several sizes and below the minimum,
// fullscreen, and ordinary GTK 3, GTK 4 and libadwaita windows.
// LUMA_SHAPE_TILER=tilingshell: Tiling Shell's own zones (half, quarter,
// third, strip, a zone below the minimum size), its neighbours,
// drag-to-zone, its keyboard moves and an app letting go of its shape.
// Screenshots are taken mid-drag, with the button still held.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Mtk from 'gi://Mtk';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const out = GLib.getenv('LUMA_SHAPE_OUT');
const here = GLib.getenv('LUMA_SHAPE_HERE');
const client = GLib.getenv('LUMA_SHAPE_CLIENT');
const expectEnforced = GLib.getenv('LUMA_SHAPE_EXPECT') !== 'stock';
const tiler = GLib.getenv('LUMA_SHAPE_TILER') || 'none';
const MIN_W = 400, MIN_H = 300; // the locked client's minimum content size
const results = [];
const shots = [];
const RW = 4, RH = 3;

const sleep = ms => new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
    resolve();
    return GLib.SOURCE_REMOVE;
}));
const check = (name, ok, detail = '') => {
    results.push({name, ok: !!ok, detail: String(detail)});
    console.log(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`);
};
const mark = text => console.log(`LUMA-SHAPE-MARK ${text}`);
const frame = w => {
    const r = w.get_frame_rect();
    return {x: r.x, y: r.y, width: r.width, height: r.height};
};
const now = () => GLib.get_monotonic_time();

const seat = Clutter.get_default_backend().get_default_seat();
const pointer = seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
const keyboard = seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
const motion = (x, y) => pointer.notify_absolute_motion(now(), x, y);
const button = pressed => pointer.notify_button(now(), Clutter.BUTTON_PRIMARY,
    pressed ? Clutter.ButtonState.PRESSED : Clutter.ButtonState.RELEASED);
const key = (keyval, pressed) => keyboard.notify_keyval(now(), keyval,
    pressed ? Clutter.KeyState.PRESSED : Clutter.KeyState.RELEASED);

function readLog(path) {
    try {
        const [, bytes] = GLib.file_get_contents(path);
        return new TextDecoder().decode(bytes).split('\n').filter(l => l.trim()).map(l => JSON.parse(l));
    } catch {
        return [];
    }
}

async function screenshot(name) {
    const path = `${out}/${name}.png`;
    const file = Gio.File.new_for_path(path);
    const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
    const shooter = new Shell.Screenshot();
    await new Promise((resolve, reject) => shooter.screenshot(true, stream, (o, res) => {
        try {
            o.screenshot_finish(res);
            resolve();
        } catch (e) {
            reject(e);
        }
    }));
    stream.close(null);
    shots.push(name);
}

const children = [];
const byName = {};
function spawn(argv, logName, backend = 'wayland') {
    const launcher = new Gio.SubprocessLauncher({flags: Gio.SubprocessFlags.STDERR_MERGE});
    launcher.setenv('GDK_BACKEND', backend, true);
    launcher.set_stdout_file_path(`${out}/${logName}.stdout`);
    const child = launcher.spawnv(argv);
    children.push(child);
    byName[logName] = child;
    return child;
}

// GNOME Shell opens the overview at startup and when the last window goes
// away; the scenarios need the desktop.
async function ensureDesktop() {
    for (let i = 0; i < 20 && (Main.overview.visible || Main.overview.animationInProgress); i++) {
        Main.overview.hide();
        await sleep(300);
    }
}

async function waitWindow(title) {
    for (let i = 0; i < 400; i++) {
        const actor = global.get_window_actors().find(a => a.meta_window.get_title() === title &&
            a.meta_window.get_frame_rect().width > 0);
        if (actor) {
            await ensureDesktop();
            await sleep(400);
            return actor.meta_window;
        }
        await sleep(100);
    }
    console.log(`window ${title} not found; mapped: ${global.get_window_actors().map(a => a.meta_window.get_title()).join(', ')}`);
    return null;
}

// Whether a frame rectangle's content (frame less the client's chrome) is
// at 4:3, to within the rounding of one pixel on each side.
const contentRatioOk = (f, base) =>
    Math.abs((f.width - base.w) * RH - (f.height - base.h) * RW) <= RW + RH;

// A locked window in an area (a tile): the largest size with 4:3 content,
// centred, inside the area, filling it in one direction.
function fitsArea(f, a, base) {
    const inside = f.x >= a.x && f.y >= a.y && f.x + f.width <= a.x + a.width && f.y + f.height <= a.y + a.height;
    const centred = Math.abs((f.x - a.x) - (a.x + a.width - f.x - f.width)) <= 1 &&
        Math.abs((f.y - a.y) - (a.y + a.height - f.y - f.height)) <= 1;
    const fills = Math.abs(f.width - a.width) <= 1 || Math.abs(f.height - a.height) <= 1;
    return inside && centred && fills && contentRatioOk(f, base);
}

// Below its minimum the window wins: its minimum at 4:3, at the area's origin.
function minimumAtOrigin(f, a, base) {
    return f.x === a.x && f.y === a.y && f.width >= MIN_W + base.w && f.height >= MIN_H + base.h &&
        f.width <= MIN_W + base.w + 2 && contentRatioOk(f, base);
}

// Nothing re-resizes the window for two seconds: no loop.
async function settled(w, log) {
    const f = frame(w);
    const count = readLog(log).length;
    for (let i = 0; i < 20; i++) {
        await sleep(100);
        if (JSON.stringify(frame(w)) !== JSON.stringify(f))
            return false;
    }
    return readLog(log).length === count;
}

// Waits (up to 3 s) for the client to report that it is tiled.
async function told(sizes) {
    for (let i = 0; i < 30 && !sizes().at(-1)?.tiled; i++)
        await sleep(100);
    return !!sizes().at(-1)?.tiled;
}

// Waits until a window's frame has not changed for half a second.
async function still(w) {
    let last = JSON.stringify(frame(w)), since = 0;
    for (let i = 0; i < 60 && since < 500; i++) {
        await sleep(100);
        const now = JSON.stringify(frame(w));
        since = now === last ? since + 100 : 0;
        last = now;
    }
}

const sameRect = (a, b) => a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;

function place(w, x, y, width, height) {
    w.move_resize_frame(true, x, y, width, height);
}

// A press just outside the window geometry, in the client's resize border,
// then a drag in steps; checks run on every step while the button is held.
const EDGES = {
    n: [0.5, 0, 0, -1], s: [0.5, 1, 0, 1], w: [0, 0.5, -1, 0], e: [1, 0.5, 1, 0],
    nw: [0, 0, -1, -1], ne: [1, 0, 1, -1], sw: [0, 1, -1, 1], se: [1, 1, 1, 1],
};

async function drag(w, edge, dx, dy, opts = {}) {
    const [fx, fy, nx, ny] = EDGES[edge];
    const f = frame(w);
    const px = f.x + fx * f.width + nx * 3;
    const py = f.y + fy * f.height + ny * 3;
    const steps = 10;
    const samples = [];
    // Approach the border the way a hand does, so the client has seen the
    // pointer over its resize border before the press.
    motion(px - nx * 12, py - ny * 12);
    await sleep(100);
    motion(px - nx * 4, py - ny * 4);
    await sleep(100);
    motion(px, py);
    await sleep(150);
    mark(`drag-begin ${opts.label ?? edge}`);
    button(true);
    await sleep(150);
    for (let i = 1; i <= steps; i++) {
        motion(px + dx * i / steps, py + dy * i / steps);
        await sleep(90);
        samples.push(frame(w));
        if (opts.shot && i === Math.round(steps * 0.6))
            await screenshot(opts.shot);
    }
    await sleep(200);
    const held = frame(w);
    button(false);
    mark(`drag-end ${opts.label ?? edge}`);
    await sleep(500);
    return {start: f, samples, held, end: frame(w)};
}

function configuresBetween(begin, end) {
    const [, bytes] = GLib.file_get_contents(`${out}/shell.log`);
    const text = new TextDecoder().decode(bytes);
    const a = text.indexOf(`LUMA-SHAPE-MARK ${begin}`);
    const b = text.indexOf('LUMA-SHAPE-MARK', a + 1);
    const segment = text.slice(a, end === 'eof' ? text.length : b < 0 ? text.length : b);
    return [...segment.matchAll(/Configuring xdg_toplevel#(\d+) \(wl_surface#(\d+)\): serial=(\d+) state=([^,]*), size=(\d+)x(\d+)/g)]
        .map(m => ({toplevel: m[1], surface: m[2], states: m[4], width: +m[5], height: +m[6]}));
}

async function work() {
    // A headless seat has no keyboard until a key is pressed once.
    key(Clutter.KEY_Shift_L, true);
    key(Clutter.KEY_Shift_L, false);
    await ensureDesktop();
    await sleep(500);
    const monitor = Main.layoutManager.primaryMonitor;
    const area = Main.layoutManager.getWorkAreaForMonitor(Main.layoutManager.primaryIndex);
    const workArea = {x: area.x, y: area.y, width: area.width, height: area.height};
    check('setup: monitor', monitor.width === 1920 && monitor.height === 1080, `${monitor.width}x${monitor.height}`);

    // An unlocked reference window: what the shell maximizes and tiles to.
    const refLog = `${out}/ref.log`;
    spawn([client, '--name', 'ref', '--log', refLog, '--size', '600x400'], 'ref');
    const ref = await waitWindow('ref');
    check('reference window mapped', ref);
    if (!ref)
        return;
    ref.maximize();
    await sleep(900);
    const M = frame(ref);
    ref.unmaximize();
    await sleep(600);
    ref.minimize();
    await sleep(300);

    const lockedLog = `${out}/locked.log`;
    spawn([client, '--name', 'locked', '--log', lockedLog, '--ratio', `${RW}:${RH}`,
        '--size', '800x520', '--min', `${MIN_W}x${MIN_H}`], 'locked');
    const locked = await waitWindow('locked');
    check('locked window mapped', locked);
    if (!locked)
        return;
    const ready = readLog(lockedLog).find(e => e.ev === 'ready');
    check(`client reports enforced=${expectEnforced}`, ready && ready.enforced === expectEnforced, JSON.stringify(ready));
    const sizes = () => readLog(lockedLog).filter(e => e.ev === 'size');
    const first = sizes().at(-1);
    const base = {w: first.w - first.cw, h: first.h - first.ch};
    check('base is the header bar', base.w === 0 && base.h > 20 && base.h < 80, JSON.stringify(base));
    const clientAtFrame = () => {
        const last = sizes().at(-1), f = frame(locked);
        return last.w === f.width && last.h === f.height;
    };

    if (tiler === 'tilingshell') {
        await tilingShell({locked, ref, base, sizes, clientAtFrame, lockedLog, refLog, M});
        return;
    }

    const initial = frame(locked);
    if (expectEnforced) {
        check('initial size keeps content at 4:3 (app asked 800x520)', contentRatioOk(initial, base), JSON.stringify(initial));
        check('client content is 4:3 at first map',
            Math.abs(first.cw * RH - first.ch * RW) <= RW + RH, `${first.cw}x${first.ch}`);
    }
    await screenshot('00-initial');

    // -- interactive resizes from every edge and corner ---------------------
    // The first button press of a fresh headless seat can be taken before
    // the client has its pointer focus; one unchecked drag settles it.
    place(locked, 520, 260, 720, 540 + base.h);
    await sleep(600);
    await drag(locked, 'se', 20, 10, {label: 'warm-up'});
    const plan = [
        ['se', 140, 30], ['s', 0, 120], ['e', 160, 0], ['n', 0, -110],
        ['w', -150, 0], ['nw', -90, -40], ['ne', 120, -60], ['sw', -40, 110],
        ['se', -200, -20],
    ];
    let i = 0;
    for (const [edge, dx, dy] of plan) {
        const label = `${edge}${i}`;
        await ensureDesktop();
        place(locked, 520, 260, 720, 540 + base.h);
        await sleep(600);
        const logMark = sizes().length;
        const r = await drag(locked, edge, dx, dy, {label, shot: `drag-${String(i).padStart(2, '0')}-${edge}`});
        const during = sizes().slice(logMark);
        const moved = r.end.width !== r.start.width || r.end.height !== r.start.height;
        check(`${label}: drag resized the window`, moved, `${JSON.stringify(r.start)} -> ${JSON.stringify(r.end)}`);
        if (expectEnforced) {
            const bad = r.samples.filter(f => !contentRatioOk(f, base));
            check(`${label}: 4:3 held on every step while the button was held (${r.samples.length} steps)`,
                bad.length === 0, bad.length ? JSON.stringify(bad[0]) : JSON.stringify(r.held));
            const badClient = during.filter(e => Math.abs(e.cw * RH - e.ch * RW) > RW + RH);
            check(`${label}: every size the client laid out had 4:3 content (${during.length} layouts)`,
                during.length > 0 && badClient.length === 0, badClient.length ? JSON.stringify(badClient[0]) : '');
        }
        const configures = configuresBetween(`drag-begin ${label}`, 'eof');
        const resizing = configures.filter(c => c.states.includes('resizing'));
        const surface = resizing[0]?.surface;
        const mine = configures.filter(c => c.surface === surface);
        const lastMine = mine.at(-1);
        check(`${label}: the configure after the drag is not marked resizing`,
            lastMine && !lastMine.states.includes('resizing'), JSON.stringify(lastMine));
        const last = sizes().at(-1);
        check(`${label}: client ends at the compositor's size`,
            last.w === r.end.width && last.h === r.end.height, `client ${last.w}x${last.h} frame ${r.end.width}x${r.end.height}`);
        i++;
    }

    // -- maximize: the largest 4:3 size in the maximize area, centred ---------
    place(locked, 300, 200, 640, 480 + base.h);
    await sleep(500);
    locked.maximize();
    await sleep(900);
    const m = frame(locked);
    await screenshot('maximized');
    if (expectEnforced)
        check('maximize: largest 4:3 size in the maximize area, centred', fitsArea(m, M, base) && m.height === M.height,
            `${JSON.stringify(m)} area ${JSON.stringify(M)}`);
    else
        check('maximize: fills the maximize area', sameRect(m, M), `${JSON.stringify(m)} area ${JSON.stringify(M)}`);
    const lm = sizes().at(-1);
    check('maximize: client is maximized at that size', lm.maximized && lm.w === m.width && lm.h === m.height, JSON.stringify(lm));
    locked.unmaximize();
    await sleep(800);
    check('unmaximize: restores the floating size', frame(locked).width === 640 || contentRatioOk(frame(locked), base), JSON.stringify(frame(locked)));

    // -- Mutter's half tiles (edge tiling: drag to a screen edge) -------------
    // Luma turns Mutter's own tiling off in favour of Tiling Shell; run.sh
    // turns edge tiling on so this path is covered for every other setup.
    ref.unminimize();
    ref.activate(global.get_current_time());
    await sleep(400);
    await dragHeader(ref, 'ref-right-edge', monitor.x + monitor.width - 1, monitor.y + monitor.height / 2, base.h);
    const right = frame(ref);
    check('mutter tile: the unlocked neighbour fills the right half',
        right.y === workArea.y && right.height === workArea.height &&
        right.x + right.width === workArea.x + workArea.width && Math.abs(right.width - workArea.width / 2) <= 1,
        JSON.stringify(right));
    locked.activate(global.get_current_time());
    await sleep(300);
    await dragHeader(locked, 'left-edge', monitor.x + 1, monitor.y + monitor.height / 2, base.h);
    const half = {x: workArea.x, y: workArea.y, width: Math.round(workArea.width / 2), height: workArea.height};
    const t = frame(locked);
    await screenshot('tiled-left');
    if (expectEnforced)
        check('mutter tile: 4:3 inside the left half, centred, not stretched', fitsArea(t, half, base),
            `${JSON.stringify(t)} tile ${JSON.stringify(half)}`);
    else
        check('mutter tile: fills the left half', sameRect(t, half), `${JSON.stringify(t)} tile ${JSON.stringify(half)}`);
    check('mutter tile: client is told it is tiled, at that size', sizes().at(-1).tiled && clientAtFrame(), JSON.stringify(sizes().at(-1)));
    check('mutter tile: settles (no re-resizing)', await settled(locked, lockedLog));
    check('mutter tile: the neighbour still fills its half', sameRect(frame(ref), right), JSON.stringify(frame(ref)));
    await dragHeader(locked, 'untile', monitor.x + monitor.width / 2, monitor.y + monitor.height / 2, base.h);
    if (expectEnforced)
        check('untile: floating again at 4:3', contentRatioOk(frame(locked), base), JSON.stringify(frame(locked)));
    ref.minimize();
    await sleep(300);

    // -- a tiling manager's tiles: move_resize_frame + set_externally_tiled ----
    if (typeof locked.set_externally_tiled === 'function') {
        const W = workArea;
        const zones = [
            ['two-thirds', {x: W.x + 10, y: W.y + 10, width: Math.round(W.width * 2 / 3) - 20, height: W.height - 20}],
            ['quarter', {x: W.x + Math.round(W.width / 2), y: W.y + Math.round(W.height / 2), width: W.width - Math.round(W.width / 2), height: W.height - Math.round(W.height / 2)}],
            ['strip', {x: W.x, y: W.y, width: W.width, height: Math.round(W.height * 0.4)}],
            ['tall', {x: W.x + 100, y: W.y, width: 520, height: W.height}],
            ['below-minimum', {x: W.x + 40, y: W.y + 40, width: 300, height: 220}],
        ];
        for (const [name, zone] of zones) {
            // Tiling Shell resizes first and marks the window tiled after.
            locked.move_resize_frame(false, zone.x, zone.y, zone.width, zone.height);
            locked.set_externally_tiled(true);
            await sleep(900);
            const z = frame(locked);
            await screenshot(`external-${name}`);
            if (!expectEnforced)
                check(`external ${name}: fills the zone`, sameRect(z, zone), `${JSON.stringify(z)} zone ${JSON.stringify(zone)}`);
            else if (name === 'below-minimum')
                check('external below-minimum: the window keeps its minimum at 4:3, at the zone origin',
                    minimumAtOrigin(z, zone, base), `${JSON.stringify(z)} zone ${JSON.stringify(zone)}`);
            else
                check(`external ${name}: 4:3 inside the zone, centred`, fitsArea(z, zone, base),
                    `${JSON.stringify(z)} zone ${JSON.stringify(zone)}`);
            check(`external ${name}: client at that size, settles`, clientAtFrame() && await settled(locked, lockedLog),
                JSON.stringify(sizes().at(-1)));
            if (name === 'two-thirds' && expectEnforced) {
                // An app's "free" mode lets go of the shape: it fills the tile
                // again, and keeps it once the shape is back.
                byName.locked.send_signal(10);
                await sleep(1000);
                check('external free mode: fills the zone', sameRect(frame(locked), zone),
                    `${JSON.stringify(frame(locked))} zone ${JSON.stringify(zone)}`);
                byName.locked.send_signal(10);
                await sleep(1000);
                check('external free mode off: 4:3 in the zone again', fitsArea(frame(locked), zone, base),
                    JSON.stringify(frame(locked)));
            }
            locked.set_externally_tiled(false);
        }
        place(locked, 300, 200, 640, 480 + base.h);
        await sleep(800);
        if (expectEnforced)
            check('external tile released: floating at 4:3 again', contentRatioOk(frame(locked), base), JSON.stringify(frame(locked)));
    }

    // -- an X11 client with one aspect ratio keeps it in a tile too ----------
    if (typeof locked.get_frame_rect_in_tile === 'function') {
        spawn(['python3', `${here}/other.py`, 'gtk3-x11-aspect', 'x11-aspect', `${out}/x11-aspect.log`], 'x11-aspect', 'x11');
        const x = await waitWindow('x11-aspect');
        check('X11 aspect window mapped (Xwayland)', x && x.get_client_type() === Meta.WindowClientType.X11);
        if (x) {
            const W = workArea;
            const zone = {x: W.x, y: W.y, width: Math.round(W.width / 2), height: W.height};
            x.move_resize_frame(false, zone.x, zone.y, zone.width, zone.height);
            x.set_externally_tiled(true);
            await sleep(1200);
            const f = frame(x);
            const expected = (() => {
                const r = x.get_frame_rect_in_tile(new Mtk.Rectangle({x: zone.x, y: zone.y, width: zone.width, height: zone.height}));
                return {x: r.x, y: r.y, width: r.width, height: r.height};
            })();
            await screenshot('x11-aspect-tile');
            if (expectEnforced)
                check('X11 aspect window: keeps its shape in a tile, centred, as Mutter reports',
                    !sameRect(f, zone) && sameRect(f, expected) &&
                    Math.abs((f.y - zone.y) - (zone.y + zone.height - f.y - f.height)) <= 1 &&
                    Math.abs((f.x - zone.x) - (zone.x + zone.width - f.x - f.width)) <= 1,
                    `${JSON.stringify(f)} expected ${JSON.stringify(expected)} zone ${JSON.stringify(zone)}`);
            x.set_externally_tiled(false);
            x.delete(global.get_current_time());
            await sleep(400);
        }
    }

    // -- fullscreen -------------------------------------------------------------
    locked.make_fullscreen();
    await sleep(900);
    const fs = frame(locked);
    await screenshot('fullscreen');
    check('fullscreen: whole monitor, ratio not applied',
        fs.x === monitor.x && fs.y === monitor.y && fs.width === monitor.width && fs.height === monitor.height, JSON.stringify(fs));
    const lf = sizes().at(-1);
    check('fullscreen: client is fullscreen at the monitor size', lf.fullscreen && lf.w === monitor.width && lf.h === monitor.height, JSON.stringify(lf));
    locked.unmake_fullscreen();
    await sleep(800);
    if (expectEnforced)
        check('leave fullscreen: back at 4:3', contentRatioOk(frame(locked), base), JSON.stringify(frame(locked)));

    locked.minimize();
    await sleep(400);

    // -- ordinary windows are unaffected ---------------------------------------
    const others = [
        ['plain-gtk4', [client, '--name', 'plain-gtk4', '--log', `${out}/plain-gtk4.log`, '--size', '600x400']],
        ['adw', ['python3', `${here}/other.py`, 'adw', 'adw', `${out}/adw.log`]],
        ['gtk3', ['python3', `${here}/other.py`, 'gtk3', 'gtk3', `${out}/gtk3.log`]],
    ];
    for (const [name, argv] of others) {
        spawn(argv, name);
        const w = await waitWindow(name);
        check(`${name}: mapped`, w);
        if (!w)
            continue;
        await ensureDesktop();
        place(w, 400, 250, 600, 400);
        await sleep(600);
        for (const [edge, dx, dy] of [['se', 150, 37], ['n', 0, -53], ['w', -71, 0]]) {
            const label = `${name}-${edge}`;
            const r = await drag(w, edge, dx, dy, {label, shot: name === 'plain-gtk4' && edge === 'se' ? 'plain-gtk4-drag-se' : null});
            const [, , nx, ny] = EDGES[edge];
            const ew = r.start.width + Math.abs(nx) * Math.sign(nx) * dx;
            const eh = r.start.height + Math.abs(ny) * Math.sign(ny) * dy;
            check(`${label}: follows the pointer exactly`,
                Math.abs(r.end.width - ew) <= 1 && Math.abs(r.end.height - eh) <= 1,
                `${JSON.stringify(r.start)} -> ${JSON.stringify(r.end)} expected ${ew}x${eh}`);
            const configures = configuresBetween(`drag-begin ${label}`, 'eof');
            const resizing = configures.filter(c => c.states.includes('resizing'));
            const mine = configures.filter(c => c.surface === resizing[0]?.surface);
            if (name !== 'gtk3' || mine.length) {
                check(`${label}: the configure after the drag is not marked resizing`,
                    mine.length && !mine.at(-1).states.includes('resizing'), JSON.stringify(mine.at(-1)));
            }
        }
        w.maximize();
        await sleep(800);
        const mw = frame(w);
        check(`${name}: maximizes to the whole maximize area`, sameRect(mw, M),
            `${JSON.stringify(mw)} area ${JSON.stringify(M)}`);
        w.unmaximize();
        await sleep(500);
        w.delete(global.get_current_time());
        await sleep(400);
    }
}

// A drag by the header bar with the real pointer.
async function dragHeader(w, label, tx, ty, headerHeight, shotWhileHeld = null) {
    await ensureDesktop();
    const f = frame(w);
    const sx = f.x + f.width / 2, sy = f.y + Math.min(20, headerHeight / 2);
    motion(sx, sy - 6);
    await sleep(150);
    motion(sx, sy);
    await sleep(150);
    mark(`header-drag-begin ${label}`);
    button(true);
    await sleep(200);
    for (let i = 1; i <= 15; i++) {
        motion(sx + (tx - sx) * i / 15, sy + (ty - sy) * i / 15);
        await sleep(80);
    }
    await sleep(400);
    if (shotWhileHeld)
        await screenshot(shotWhileHeld);
    button(false);
    mark(`header-drag-end ${label}`);
    await sleep(1500);
}

function shellLogLength() {
    const [, bytes] = GLib.file_get_contents(`${out}/shell.log`);
    return bytes.length;
}

// Tiling Shell's corrections ([luma-tiling] tile-corrected / tile-refused /
// reflow-stopped) for a window since a shell.log offset.
function tilerCorrections(since, title) {
    const [, bytes] = GLib.file_get_contents(`${out}/shell.log`);
    const text = new TextDecoder().decode(bytes.slice(since));
    return text.split('\n').filter(l => l.includes('[luma-tiling]') && l.includes(`"${title}"`) &&
        /put .* back in its tile|stays at|stopped re-tiling/.test(l));
}

// Tiling Shell, as Luma ships it: its zones through its own tile path, its
// neighbours, drag-to-zone, screen-edge tiling and its keyboard moves.
async function tilingShell({locked, ref, base, sizes, clientAtFrame, lockedLog, refLog}) {
    const ts = Main.extensionManager.lookup('tilingshell@ferrarodomenico.com')?.stateObj;
    const managers = ts?._tilingManagers;
    check('Tiling Shell is running', managers?.length > 0, `${managers?.length}`);
    if (!managers?.length)
        return;
    const manager = () => managers[locked.get_monitor()] ?? managers[0];
    const tileTo = async (w, t) => {
        manager().onTileFromWindowMenu({x: t.x, y: t.y, width: t.width, height: t.height, groups: []}, w);
        await sleep(600);
        await still(w);
    };
    // The rectangle Tiling Shell gives a tile, measured with the unlocked
    // reference window.
    const measure = async t => {
        await tileTo(ref, t);
        return frame(ref);
    };
    ref.unminimize();
    await sleep(500);

    const zones = [
        ['half', {x: 0, y: 0, width: 0.5, height: 1}, {x: 0.5, y: 0, width: 0.5, height: 1}],
        ['quarter', {x: 0, y: 0, width: 0.5, height: 0.5}, {x: 0.5, y: 0, width: 0.5, height: 0.5}],
        ['third', {x: 0, y: 0, width: 1 / 3, height: 1}, {x: 1 / 3, y: 0, width: 2 / 3, height: 1}],
        ['strip', {x: 0, y: 0, width: 1, height: 0.4}, {x: 0, y: 0.4, width: 1, height: 0.6}],
        ['below-minimum', {x: 0, y: 0, width: 0.15, height: 0.2}, {x: 0.15, y: 0, width: 0.85, height: 1}],
    ];
    for (const [name, Z, Zc] of zones) {
        const R = await measure(Z);
        const Rc = await measure(Zc);
        const since = shellLogLength();
        await tileTo(locked, Z);
        const L = frame(locked);
        await screenshot(`tilingshell-${name}`);
        if (name === 'below-minimum')
            check(`Tiling Shell ${name}: the window keeps its minimum at 4:3, at the zone origin`,
                minimumAtOrigin(L, R, base), `${JSON.stringify(L)} zone ${JSON.stringify(R)}`);
        else
            check(`Tiling Shell ${name}: 4:3 inside the zone, centred, not stretched`, fitsArea(L, R, base),
                `${JSON.stringify(L)} zone ${JSON.stringify(R)}`);
        check(`Tiling Shell ${name}: still in its tile, client told it is tiled`,
            !!locked.assignedTile && await told(sizes), JSON.stringify(sizes().at(-1)));
        check(`Tiling Shell ${name}: settles, client at that size`,
            await settled(locked, lockedLog) && clientAtFrame(), JSON.stringify(frame(locked)));
        check(`Tiling Shell ${name}: the neighbour fills its zone`, sameRect(frame(ref), Rc),
            `${JSON.stringify(frame(ref))} zone ${JSON.stringify(Rc)}`);
        const corrections = tilerCorrections(since, 'locked');
        check(`Tiling Shell ${name}: never puts the window back or re-tiles it`, corrections.length === 0,
            corrections.join(' | ').slice(0, 400));
    }

    // An app's "free" mode lets go of the shape: the window fills its tile,
    // and keeps 4:3 again once the shape is back.
    {
        const R = await measure(zones[0][1]);
        await measure(zones[0][2]);
        await tileTo(locked, zones[0][1]);
        byName.locked.send_signal(10);
        await sleep(1300);
        await screenshot('tilingshell-free-mode');
        check('Tiling Shell free mode: fills the tile', sameRect(frame(locked), R),
            `${JSON.stringify(frame(locked))} zone ${JSON.stringify(R)}`);
        byName.locked.send_signal(10);
        await sleep(1300);
        check('Tiling Shell free mode off: 4:3 in the tile again', fitsArea(frame(locked), R, base),
            JSON.stringify(frame(locked)));
    }

    const area = Main.layoutManager.getWorkAreaForMonitor(locked.get_monitor());
    const landed = async label => {
        const tile = locked.assignedTile;
        const L = frame(locked);
        await screenshot(`tilingshell-${label}`);
        check(`Tiling Shell ${label}: tiled, client told it is tiled`, !!tile && await told(sizes), JSON.stringify(L));
        if (!tile)
            return;
        const since = shellLogLength();
        check(`Tiling Shell ${label}: settles`, await settled(locked, lockedLog) && clientAtFrame(), JSON.stringify(frame(locked)));
        const corrections = tilerCorrections(since, 'locked');
        const R = await measure({x: tile.x, y: tile.y, width: tile.width, height: tile.height});
        check(`Tiling Shell ${label}: 4:3 inside its zone, centred`, fitsArea(L, R, base),
            `${JSON.stringify(L)} zone ${JSON.stringify(R)}`);
        check(`Tiling Shell ${label}: no corrections or re-tiling`, corrections.length === 0, corrections.join(' | ').slice(0, 400));
        ref.minimize();
        await sleep(400);
    };

    // Float the window, then drop it in the right column of the layout.
    ref.minimize();
    await sleep(300);
    await tileTo(locked, zones[0][1]);
    await dragHeader(locked, 'drag-to-zone', area.x + area.width * 0.75, area.y + area.height * 0.5, base.h, 'tilingshell-dragging-over-zone');
    await landed('drag-to-zone');

    // Keyboard: Tiling Shell's Super+Left moves the window to the tile beside.
    locked.activate(global.get_current_time());
    await sleep(300);
    const before = frame(locked);
    key(Clutter.KEY_Super_L, true);
    key(Clutter.KEY_Left, true);
    key(Clutter.KEY_Left, false);
    key(Clutter.KEY_Super_L, false);
    await sleep(1500);
    const byKey = !sameRect(frame(locked), before);
    // In the headless seat the Luma Shell does not route Super+arrow from the
    // virtual keyboard to Tiling Shell; run the handler that key binding runs
    // (move-window-left) so its keyboard tile path is still exercised.
    console.log(`keyboard move: Super+Left from the virtual keyboard ${byKey ? 'moved' : 'did not move'} the window`);
    if (!byKey)
        ts._onKeyboardMoveWin(global.display, 4 /* LEFT */, false);
    await sleep(1500);
    check('Tiling Shell keyboard-move: move-window-left moved the window', !sameRect(frame(locked), before),
        `${JSON.stringify(frame(locked))} via ${byKey ? 'virtual keyboard' : 'the key binding handler'}`);
    await landed('keyboard-move');

    // Screen edges: the left edge is a half, the bottom-left corner a quarter.
    await dragHeader(locked, 'left-edge', area.x + 1, area.y + area.height * 0.5, base.h);
    await landed('left-edge');
    await dragHeader(locked, 'corner', area.x + 1, area.y + 1, base.h);
    await landed('corner');
}

async function finish() {
    for (const child of children)
        child.force_exit();
    GLib.file_set_contents(`${out}/result.json`,
        JSON.stringify({pass: results.every(r => r.ok), results, shots}, null, 2));
    global.context.terminate();
}

// Upstream GNOME Shell's automation runner calls run() once the shell is up;
// Luma Shell's calls init(). Whichever comes first runs the scenario once.
let started = null;
function start() {
    started ??= (async () => {
        try {
            await work();
        } catch (error) {
            check('scenario completed', false, String(error.stack));
        }
        await finish();
    })();
    return started;
}

export function init() {
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 5000, () => {
        start();
        return GLib.SOURCE_REMOVE;
    });
}

export async function run() {
    await sleep(3000);
    await start();
}
