// SPDX-License-Identifier: Apache-2.0
// gnome-shell --automation-script for the window-capture test (run.sh).
// Windows painted one solid colour each; captures count their pixels
// (capture.c). A Wayland window hides itself from capture through LumaUI's
// luma_window_set_hidden_from_capture() (luma_window_capture_v1), an
// Xwayland window through _LUMA_WINDOW_HIDDEN_FROM_CAPTURE, and a control
// window never hides. Monitor, area and window screen casts through
// org.gnome.Mutter.ScreenCast, and Shell screenshots, are checked on two
// virtual monitors, with a dialog transient for the hidden window, the
// overview's window clones, a move to the other monitor and minimizing.
// LUMA_CAPTURE_MODE=perf measures what a monitor stream costs with no
// hidden window, with one on the other monitor and with one on its monitor.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Mtk from 'gi://Mtk';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

Gio._promisify(Shell.Screenshot.prototype, 'screenshot');
Gio._promisify(Shell.Screenshot.prototype, 'screenshot_area');

const out = GLib.getenv('LUMA_CAPTURE_OUT');
const here = GLib.getenv('LUMA_CAPTURE_HERE');
const captureBin = GLib.getenv('LUMA_CAPTURE_BIN');
const clientBin = GLib.getenv('LUMA_CAPTURE_CLIENT');
const fixed = GLib.getenv('LUMA_CAPTURE_EXPECT') !== 'stock';
const mode = GLib.getenv('LUMA_CAPTURE_MODE') || 'full';
const results = [];
const records = [];
const perf = [];

// The colours: the hidden Wayland window, its dialog (the complement), the
// hidden Xwayland window and the control window that never hides.
const HIDDEN = 'ff00ff';
const DIALOG = '00ff00';
const X11 = 'ffff00';
const CONTROL = '00a0ff';
const COLOURS = [HIDDEN, DIALOG, X11, CONTROL];
const SIZE = [640, 400];
const AREA = SIZE[0] * SIZE[1];

const sleep = ms => new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
    resolve();
    return GLib.SOURCE_REMOVE;
}));
// Progress, in a file: the Shell's log writer may send console output to
// the journal.
const trace = text => {
    const stream = Gio.File.new_for_path(`${out}/driver-trace.txt`)
        .append_to(Gio.FileCreateFlags.NONE, null);
    stream.write_all(new TextEncoder().encode(`${GLib.get_monotonic_time()} ${text}\n`), null);
    stream.close(null);
};
const check = (name, ok, detail = '') => {
    results.push({name, ok: !!ok, detail: String(detail)});
    trace(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`);
};

function spawn(argv, logName, env = {}) {
    const launcher = new Gio.SubprocessLauncher({flags: Gio.SubprocessFlags.STDERR_MERGE});
    for (const [k, v] of Object.entries(env))
        launcher.setenv(k, v, true);
    launcher.set_stdout_file_path(`${out}/${logName}.log`);
    return launcher.spawnv(argv);
}

const wait = proc => new Promise(resolve => proc.wait_async(null, (p, res) => {
    p.wait_finish(res);
    resolve();
}));

const readLines = name => {
    const [, bytes] = GLib.file_get_contents(`${out}/${name}.log`);
    return new TextDecoder().decode(bytes).split('\n').filter(l => l.startsWith('{'))
        .map(l => JSON.parse(l));
};

async function ensureDesktop() {
    for (let i = 0; i < 20 && (Main.overview.visible || Main.overview.animationInProgress); i++) {
        Main.overview.hide();
        await sleep(300);
    }
}

async function findWindow(title) {
    for (let i = 0; i < 300; i++) {
        const actor = global.get_window_actors().find(a => a.meta_window.get_title() === title &&
            a.meta_window.get_frame_rect().width > 0);
        if (actor)
            return actor.meta_window;
        await sleep(100);
    }
    return null;
}

async function openClient(title, rgb, hidden = false, extra = []) {
    const argv = [clientBin, '--title', title, '--rgb', rgb, '--size', `${SIZE[0]}x${SIZE[1]}`, ...extra];
    if (hidden)
        argv.push('--hidden');
    const proc = spawn(argv, `client-${title}`);
    const w = await findWindow(title);
    await ensureDesktop();
    await sleep(500);
    return {w, proc, title};
}

async function openX11(title, rgb) {
    const proc = spawn(['python3', `${here}/x11.py`, title, rgb], `client-${title}`, {GDK_BACKEND: 'x11'});
    const w = await findWindow(title);
    await ensureDesktop();
    await sleep(500);
    return {w, proc, title};
}

async function close(win) {
    win.proc.force_exit();
    await wait(win.proc);
    await sleep(500);
}

const signal = (win, sig) => win.proc.send_signal(sig);

async function place(win, x, y) {
    win.w.move_resize_frame(false, x, y, SIZE[0], SIZE[1]);
    win.w.raise();
    await sleep(600);
}

// Monitors in layout order, with the connector a screen cast names.
async function monitors() {
    const reply = await new Promise((resolve, reject) => Gio.DBus.session.call(
        'org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig',
        'org.gnome.Mutter.DisplayConfig', 'GetCurrentState', null, null,
        Gio.DBusCallFlags.NONE, -1, null, (c, res) => {
            try {
                resolve(c.call_finish(res));
            } catch (e) {
                reject(e);
            }
        }));
    const [, list] = reply.recursiveUnpack();
    const manager = global.backend.get_monitor_manager();
    return list.map(([spec]) => {
        const index = manager.get_monitor_for_connector(spec[0]);
        return {connector: spec[0], index, rect: global.display.get_monitor_geometry(index)};
    }).sort((a, b) => a.index - b.index);
}

// meta_compositor_is_capture_hidden_in(): must the stream of that monitor
// paint the stage rather than copy the screen.
const compositor = () => global.display.get_compositor();
function paintsFor(monitor) {
    const r = monitor.rect;
    return compositor().is_capture_hidden_in(new Mtk.Rectangle({x: r.x, y: r.y, width: r.width, height: r.height}));
}

const cpuMs = () => {
    const [, bytes] = GLib.file_get_contents('/proc/self/stat');
    const fields = new TextDecoder().decode(bytes).split(') ')[1].split(' ');
    // utime and stime are fields 14 and 15 of stat, 12 and 13 after the name.
    return (Number(fields[11]) + Number(fields[12])) * 1000 / 100;
};

let serial = 0;
async function record(kind, what, label, durationMs = 1500, during = null) {
    const name = `rec${++serial}-${label.replace(/[^a-z0-9]+/gi, '-')}`;
    const stop = `${out}/${name}.stop`;
    GLib.unlink(stop);
    const proc = spawn([captureBin, kind, String(what), `${out}/${name}`, stop, ...COLOURS], name);
    await sleep(700); // the stream is up and has its first frame
    const cpu0 = cpuMs(), t0 = GLib.get_monotonic_time();
    if (during)
        await during();
    await sleep(durationMs);
    const cpu = cpuMs() - cpu0, seconds = (GLib.get_monotonic_time() - t0) / 1e6;
    GLib.file_set_contents(stop, '');
    await wait(proc);
    const lines = readLines(name);
    const frames = lines.filter(l => 'frame' in l);
    const error = lines.find(l => l.error);
    // The most of each colour any frame held, and the last frame's.
    const most = COLOURS.map((_, c) => Math.max(0, ...frames.map(f => f.counts[c])));
    const lastFrame = frames[frames.length - 1]?.counts ?? COLOURS.map(() => -1);
    const entry = {label, kind, what: String(what), frames: frames.length, most, last: lastFrame,
        format: frames[0]?.format, size: frames[0] ? [frames[0].width, frames[0].height] : null,
        error: error?.error, shellCpuMsPerS: cpu / seconds, seconds};
    records.push(entry);
    trace(`record ${label}: ${JSON.stringify(entry)}`);
    return entry;
}

async function screenshot(label, area = null) {
    const path = `${out}/shot-${label}.png`;
    const file = Gio.File.new_for_path(path);
    const stream = file.replace(null, false, Gio.FileCreateFlags.NONE, null);
    const shooter = new Shell.Screenshot();
    // The calls the Shell's screenshot D-Bus service makes.
    let ok;
    try {
        ok = area
            ? await shooter.screenshot_area(...area, stream)
            : await shooter.screenshot(false, stream);
    } catch (error) {
        trace(`screenshot ${label}: ${error.message}`);
    }
    stream.close(null);
    trace(`screenshot ${label}: ${JSON.stringify(ok)} ${file.query_info('standard::size', 0, null).get_size()} bytes`);
    const proc = spawn([captureBin, 'png', path, ...COLOURS], `shot-${label}`);
    await wait(proc);
    const final = readLines(`shot-${label}`).find(l => l.final);
    records.push({label: `screenshot ${label}`, kind: 'screenshot', counts: final?.counts});
    return final?.counts ?? COLOURS.map(() => -1);
}

const ci = colour => COLOURS.indexOf(colour);
const full = n => n >= AREA * 0.95;

// Luma's share picker (Shell 0382): the hidden window's tile says so, the
// control's does not, and screen tiles do not clone the hidden window. Only
// with Luma's Shell, which has the picker.
async function checkPicker(a, control) {
    let share;
    try {
        share = await import('resource:///org/gnome/shell/ui/lumaShare.js');
    } catch {
        trace('no Luma share picker in this Shell: picker checks skipped');
        return;
    }
    const picker = new share.SharePicker({types: 1 | 2, displays: [],
        requester: {name: 'Window capture test', icon: null, appId: null},
        bodyText: 'Choose a window or a whole screen.'});
    await sleep(1500);
    const tileOf = w => picker._tiles.find(t => t.source.window === w);
    const hiddenTile = tileOf(a.w), controlTile = tileOf(control.w);
    const label = 'Hidden from screen sharing';
    check('picker: the hidden window has a tile', hiddenTile);
    check('picker: its tile is marked', hiddenTile?._hidden?.visible === true &&
        hiddenTile.accessible_name.includes(label), hiddenTile?.accessible_name);
    check('picker: the control tile is not', controlTile && !controlTile._hidden?.visible &&
        !controlTile.accessible_name.includes(label), controlTile?.accessible_name);
    await screenshot('picker-windows');
    picker._setMode('screens');
    await sleep(1500);
    const actor = a.w.get_compositor_private();
    const screenTiles = picker._tiles.filter(t => t.source.kind === 'screen');
    check('picker: screen tiles leave the hidden window out', screenTiles.length > 0 &&
        screenTiles.every(t => !t._content._sources.includes(actor)) &&
        screenTiles.some(t => t._content._sources.includes(control.w.get_compositor_private())),
    screenTiles.map(t => t._content._sources.length).join(' '));
    picker.close();
    await sleep(800);
}

async function stockRun(m) {
    // A Mutter without the protocol: the kit says so, and the window is in
    // the stream (the infinite mirror a streaming app got).
    const a = await openClient('hidden', HIDDEN, true);
    check('stock: hidden window mapped', a.w);
    await place(a, 200, 150);
    const lines = readLines('client-hidden');
    check('stock: the kit reports no capture hiding', lines[0] && !lines[0].supported, JSON.stringify(lines[0]));
    const rec = await record('monitor', m[0].connector, 'stock monitor');
    check('stock: the window is in the monitor stream', full(rec.most[ci(HIDDEN)]), JSON.stringify(rec.most));
    await close(a);
}

async function fullRun(m) {
    const [m0, m1] = m;
    check('two monitors', m.length >= 2, JSON.stringify(m.map(x => x.connector)));
    check('no window hidden: no stream paints for it', !paintsFor(m0) && !paintsFor(m1));

    const control = await openClient('control', CONTROL);
    const a = await openClient('hidden', HIDDEN);
    check('windows mapped', control.w && a.w);
    await place(control, 1000, 500);
    await place(a, 200, 150);
    const started = readLines('client-hidden')[0];
    check('the kit finds the protocol', started?.supported === true, JSON.stringify(started));

    let rec = await record('monitor', m0.connector, 'shown');
    check('shown: the window is in the monitor stream', full(rec.most[ci(HIDDEN)]), JSON.stringify(rec.most));
    check('shown: frames arrive', rec.frames >= 1, rec.frames);
    let shot = await screenshot('shown');
    check('shown: the window is in a screenshot', full(shot[ci(HIDDEN)]), JSON.stringify(shot));

    signal(a, 10); // SIGUSR1: hide
    await sleep(600);
    check('hidden: MetaWindow hidden-from-capture', a.w.hidden_from_capture === true);
    check('hidden: the stream of its monitor paints, the other copies',
        paintsFor(m0) && !paintsFor(m1), `${paintsFor(m0)} ${paintsFor(m1)}`);

    rec = await record('monitor', m0.connector, 'hidden');
    check('hidden: no pixel of the window in any frame of its monitor', rec.frames > 0 &&
        rec.most[ci(HIDDEN)] === 0, JSON.stringify(rec.most));
    check('hidden: the control window is still there', full(rec.most[ci(CONTROL)]), JSON.stringify(rec.most));

    rec = await record('window', a.w.get_id(), 'own window cast');
    check('hidden: a cast of the window itself shows it', full(rec.most[ci(HIDDEN)]), JSON.stringify(rec.most));

    rec = await record('area', `${m0.rect.x},${m0.rect.y},${m0.rect.width},${m0.rect.height}`, 'area');
    check('hidden: an area cast over it leaves it out', rec.frames > 0 && rec.most[ci(HIDDEN)] === 0 &&
        full(rec.most[ci(CONTROL)]), JSON.stringify(rec.most));

    rec = await record('window', control.w.get_id(), 'other window cast', 1200,
        async () => {
            // Over the control window: a cast of another window never had it.
            await place(a, 1100, 550);
        });
    check('hidden: a cast of a window it covers does not show it', rec.most[ci(HIDDEN)] === 0,
        JSON.stringify(rec.most));
    await place(a, 200, 150);

    shot = await screenshot('screen');
    check('hidden: a screenshot of the screen leaves it out', shot[ci(HIDDEN)] === 0 &&
        full(shot[ci(CONTROL)]), JSON.stringify(shot));
    shot = await screenshot('area', [100, 100, 900, 600]);
    check('hidden: a screenshot of an area leaves it out', shot[ci(HIDDEN)] === 0 && shot.every(c => c >= 0),
        JSON.stringify(shot));

    await checkPicker(a, control);

    // A dialog transient for it is hidden with it.
    signal(a, 12); // SIGUSR2: open the dialog
    const dialog = await findWindow('hidden-dialog');
    await sleep(600);
    check('dialog: mapped and hidden with its parent', dialog?.hidden_from_capture === true);
    rec = await record('monitor', m0.connector, 'dialog');
    check('dialog: no pixel of it in the monitor stream', rec.most[ci(DIALOG)] === 0 &&
        rec.most[ci(HIDDEN)] === 0, JSON.stringify(rec.most));
    signal(a, 12);
    await sleep(600);

    // The overview shows clones of every window, on every monitor.
    Main.overview.show();
    await sleep(1500);
    check('overview: every stream paints while a clone shows it', paintsFor(m0) && paintsFor(m1));
    rec = await record('monitor', m0.connector, 'overview');
    check('overview: no pixel of its clone in the stream', rec.frames > 0 && rec.most[ci(HIDDEN)] === 0,
        JSON.stringify(rec.most));
    await ensureDesktop();
    await sleep(800);

    // On the other monitor, then minimized.
    await place(a, m1.rect.x + 300, 200);
    check('moved: only the stream of its new monitor paints', !paintsFor(m0) && paintsFor(m1),
        `${paintsFor(m0)} ${paintsFor(m1)}`);
    rec = await record('monitor', m1.connector, 'other monitor');
    check('moved: no pixel of it on its new monitor', rec.frames > 0 && rec.most[ci(HIDDEN)] === 0,
        JSON.stringify(rec.most));
    a.w.minimize();
    await sleep(800);
    check('minimized: no stream paints for it', !paintsFor(m0) && !paintsFor(m1));
    a.w.unminimize();
    await sleep(800);
    await place(a, 200, 150);

    // Xwayland, through the window property.
    const x = await openX11('x11-hidden', X11);
    check('x11: mapped and hidden through the property', x.w?.hidden_from_capture === true);
    if (x.w) {
        x.w.move_frame(false, 700, 300);
        await sleep(600);
        rec = await record('monitor', m0.connector, 'x11');
        check('x11: no pixel of it in the monitor stream', rec.frames > 0 && rec.most[ci(X11)] === 0,
            JSON.stringify(rec.most));
        await close(x);
    }

    // Shown again.
    signal(a, 10);
    await sleep(600);
    check('shown again: hidden-from-capture cleared', a.w.hidden_from_capture === false);
    check('shown again: no stream paints for it', !paintsFor(m0) && !paintsFor(m1));
    rec = await record('monitor', m0.connector, 'shown again');
    check('shown again: the window is back in the stream', full(rec.most[ci(HIDDEN)]), JSON.stringify(rec.most));

    // Hidden while the stream runs, then the client quits while hidden.
    rec = await record('monitor', m0.connector, 'hidden while recording', 1500, async () => {
        signal(a, 10);
        await sleep(400);
    });
    check('hidden while recording: gone from the last frame', rec.last[ci(HIDDEN)] === 0, JSON.stringify(rec.last));
    await close(a);
    check('closed while hidden: no stream paints any more', !paintsFor(m0) && !paintsFor(m1));
    await close(control);
}

async function perfRun(m) {
    const [m0, m1] = m;
    const control = await openClient('control', CONTROL);
    const a = await openClient('hidden', HIDDEN);
    // Repaints every frame, so the monitor stream records at the refresh rate.
    const ticker = await openClient('ticker', '808080', false, ['--animate']);
    ticker.w.move_resize_frame(false, 1300, 100, 300, 200);
    await place(control, 1000, 500);
    await place(a, 200, 150);
    // The cost of the per-frame question, with and without a hidden window.
    const rect = new Mtk.Rectangle({x: m0.rect.x, y: m0.rect.y, width: m0.rect.width, height: m0.rect.height});
    const time = () => {
        const t0 = GLib.get_monotonic_time();
        for (let i = 0; i < 20000; i++)
            compositor().is_capture_hidden_in(rect);
        return (GLib.get_monotonic_time() - t0) / 20000;
    };
    const conditions = [
        ['no window hidden', async () => {}],
        ['hidden on the other monitor', async () => {
            signal(a, 10);
            await sleep(600);
            await place(a, m1.rect.x + 300, 200);
        }],
        ['hidden on this monitor', async () => {
            await place(a, 200, 150);
        }],
    ];
    for (const [label, setup] of conditions) {
        await setup();
        const query = time();
        const runs = [];
        for (let i = 0; i < 3; i++)
            runs.push(await record('monitor', m0.connector, `perf ${label} ${i}`, 4000));
        const entry = {
            label, paints: paintsFor(m0), queryUs: query,
            fps: runs.map(r => r.frames / r.seconds),
            shellCpuMsPerS: runs.map(r => r.shellCpuMsPerS),
            hiddenPixels: Math.max(...runs.map(r => r.most[ci(HIDDEN)])),
        };
        perf.push(entry);
        trace(`perf ${JSON.stringify(entry)}`);
    }
    check('perf: no window hidden keeps the copy decision', perf[0].paints === false);
    check('perf: hidden elsewhere keeps the copy decision', perf[1].paints === false);
    check('perf: hidden here paints', perf[2].paints === true);
    check('perf: hidden here has no pixel of it', perf[2].hiddenPixels === 0, perf[2].hiddenPixels);
    await close(ticker);
    await close(a);
    await close(control);
}

async function work() {
    // The capture client talks to org.gnome.Mutter.ScreenCast directly
    // instead of through the portal.
    global.context.unsafe_mode = true;
    await ensureDesktop();
    await sleep(500);
    const m = await monitors();
    trace(`monitors ${JSON.stringify(m.map(x => [x.connector, x.index]))}`);
    if (!fixed)
        await stockRun(m);
    else if (mode === 'perf')
        await perfRun(m);
    else
        await fullRun(m);
}

// The automation runner calls run() only once org.gnome.Shell.PerfHelper is
// on the bus, which needs a session manager to launch it; a container has
// none. The checks start from init() instead, once the Shell is up, and
// end the Shell themselves.
export function init() {
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 6000, () => {
        main();
        return GLib.SOURCE_REMOVE;
    });
}

export async function run() {
    await new Promise(() => {});
}

async function main() {
    trace('started');
    await sleep(3000);
    try {
        await work();
    } catch (error) {
        check('scenario completed', false, String(error.stack));
    }
    GLib.file_set_contents(`${out}/result.json`, JSON.stringify({results, records, perf}, null, 1));
    global.context.terminate();
}
