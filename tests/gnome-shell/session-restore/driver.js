// SPDX-License-Identifier: Apache-2.0
// gnome-shell --automation-script for the session restore test (run.sh).
//
// save:    open GTK 4 and X11 windows on two monitors and two workspaces,
//          floating, maximized, fullscreen, minimized and always on top, in a
//          known stacking order with one focused; take the log-out snapshot.
// restore: a new Shell over the same home restores it at login; every
//          window's rectangles, state, workspace, monitor, stacking and focus
//          are measured against what was saved.
// remap:   the same snapshot continued on one 2560x1600 monitor: windows
//          keep their state and their share of the work area.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Mtk from 'gi://Mtk';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const out = GLib.getenv('LUMA_SESSION_OUT');
const phase = GLib.getenv('LUMA_SESSION_PHASE');
const modulePath = GLib.getenv('LUMA_SESSION_MODULE');
const results = [];
const records = [];

const sleep = ms => new Promise(resolve => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
    resolve();
    return GLib.SOURCE_REMOVE;
}));
const trace = text => {
    const stream = Gio.File.new_for_path(`${out}/driver-trace-${phase}.txt`)
        .append_to(Gio.FileCreateFlags.NONE, null);
    stream.write_all(new TextEncoder().encode(`${GLib.get_monotonic_time()} ${text}\n`), null);
    stream.close(null);
};
const check = (name, ok, detail = '') => {
    results.push({name: `${phase}: ${name}`, ok: !!ok, detail: String(detail)});
    trace(`${ok ? 'PASS' : 'FAIL'} ${name} ${detail}`);
};
const r = x => ({x: x.x, y: x.y, width: x.width, height: x.height});
const same = (a, b) => a.x === b.x && a.y === b.y && a.width === b.width && a.height === b.height;
const show = x => `${x.width}x${x.height}+${x.x}+${x.y}`;

function appWindows() {
    return global.display.sort_windows_by_stacking(global.display.list_all_windows()
        .filter(w => w.get_window_type() === Meta.WindowType.NORMAL && !w.is_override_redirect()));
}

async function windowTitled(title, timeoutMs = 20000) {
    for (let i = 0; i < timeoutMs / 100; i++) {
        const w = appWindows().find(x => x.get_title() === title && x.get_frame_rect().width > 0);
        if (w)
            return w;
        await sleep(100);
    }
    return null;
}

function launch(id) {
    const app = Shell.AppSystem.get_default().lookup_app(id);
    if (!app)
        throw new Error(`no app ${id}`);
    app.launch(0, -1, Shell.AppLaunchGpu.APP_PREF);
    return app;
}

function describe(w) {
    const floating = w.get_floating_rect ? r(w.get_floating_rect()) : r(w.get_frame_rect());
    return {
        title: w.get_title(),
        frame: r(w.get_frame_rect()),
        floating,
        maximized: w.is_maximized(),
        fullscreen: w.is_fullscreen(),
        minimized: w.minimized,
        above: w.is_above(),
        workspace: w.get_workspace()?.index() ?? -1,
        monitor: w.get_monitor(),
        focus: global.display.focus_window === w,
    };
}

async function place(w, {x, y, width, height}) {
    w.move_resize_frame(false, x, y, width, height);
    await sleep(400);
}

async function savePhase(session) {
    const wm = global.workspace_manager;
    while (wm.get_n_workspaces() < 3)
        wm.append_new_workspace(false, global.get_current_time());

    GLib.setenv('CONTINUE_DOCS', 'Doc A,Doc B,Doc C', true);
    launch('org.projectluma.ContinueDocs.desktop');
    GLib.unsetenv('CONTINUE_DOCS');
    const counter = launch('org.projectluma.ContinueCounter.desktop');
    launch('org.projectluma.ContinueX11.desktop');
    launch('org.projectluma.ContinueKeyed.desktop');

    const a = await windowTitled('Doc A');
    const b = await windowTitled('Doc B');
    const c = await windowTitled('Doc C');
    const c1 = await windowTitled('Counter 1');
    counter.open_new_window(-1);
    const c2 = await windowTitled('Counter 2');
    const x = await windowTitled('X11 window');
    await windowTitled('Notes');
    await sleep(1500);
    const notes = keyedWindows(session);
    check('keyed windows opened', notes.has('note:1') && notes.has('note:2'), [...notes.keys()].join(', '));
    check('windows opened', a && b && c && c1 && c2 && x,
        appWindows().map(w => w.get_title()).join(', '));
    if (!(a && b && c && c1 && c2 && x))
        return;

    await place(a, {x: 120, y: 140, width: 820, height: 600});
    await place(b, {x: 2050, y: 180, width: 700, height: 520});
    b.maximize();
    await place(c, {x: 300, y: 200, width: 900, height: 640});
    c.change_workspace_by_index(1, false);
    await place(c1, {x: 600, y: 360, width: 560, height: 420});
    c1.make_fullscreen();
    c1.change_workspace_by_index(2, false);
    await place(c2, {x: 260, y: 520, width: 600, height: 380});
    await place(x, {x: 2520, y: 600, width: 520, height: 420});
    await place(notes.get('note:1'), {x: 1300, y: 90, width: 500, height: 360});
    await place(notes.get('note:2'), {x: 1300, y: 560, width: 520, height: 380});
    x.make_above();
    await sleep(800);
    // Stacking on workspace 0, bottom to top: Doc B, X11, Counter 2, Doc A.
    for (const w of [b, x, c2, a])
        w.raise();
    c2.minimize();
    Main.activateWindow(a, global.get_current_time());
    await sleep(1000);

    const keyed = Object.fromEntries([...keyedWindows(session)].map(([k, w]) => [k, r(w.get_frame_rect())]));
    const expected = appWindows().map(describe);
    const stacking = appWindows().map(w => w.get_title());
    records.push({expected, stacking});
    GLib.file_set_contents(`${out}/expected.json`, JSON.stringify({expected, stacking, keyed}, null, 1));

    const snapshot = await session.finalSnapshot('logout');
    check('snapshot taken', snapshot && snapshot.windows.length === 8,
        `${snapshot?.windows.length} windows`);
    const path = GLib.build_filenamev([GLib.get_user_state_dir(), 'luma', 'session', 'current.json']);
    const info = Gio.File.new_for_path(path).query_info('unix::mode', 0, null);
    check('snapshot is private', (info.get_attribute_uint32('unix::mode') & 0o077) === 0,
        (info.get_attribute_uint32('unix::mode') & 0o777).toString(8));
    GLib.file_set_contents(`${out}/snapshot.json`, JSON.stringify(snapshot, null, 1));
    const keyedApp = snapshot.apps.find(app => app.app === 'org.projectluma.ContinueKeyed.desktop');
    check('LumaUI app state in the snapshot', keyedApp?.state?.includes("'note:2'") &&
        keyedApp.state.includes("'draft'") && keyedApp.state.includes('second note'), keyedApp?.state);
    check('window keys in the snapshot', snapshot.windows.filter(w => w.key?.startsWith('note:')).length === 2,
        snapshot.windows.map(w => w.key).join(', '));
}

function keyedWindows(session) {
    const map = new Map();
    for (const w of appWindows().filter(x => x.get_title() === 'Notes')) {
        const key = session._keys.get(`${w.get_gtk_unique_bus_name()}|${w.get_gtk_window_object_path()}`);
        if (key)
            map.set(key, w);
    }
    return map;
}

async function waitRestore(session) {
    const started = GLib.get_monotonic_time();
    for (let i = 0; i < 400 && !session._lastResult; i++)
        await sleep(100);
    const seconds = (GLib.get_monotonic_time() - started) / 1e6;
    check('restore finished', session._lastResult, JSON.stringify(session._lastResult));
    records.push({restoreResult: session._lastResult, waited: seconds});
    await sleep(1500);
}

async function restorePhase(session) {
    const {expected, stacking, keyed} = JSON.parse(new TextDecoder().decode(
        GLib.file_get_contents(`${out}/expected.json`)[1]));
    await waitRestore(session);
    const now = appWindows();
    const measured = now.map(describe);
    records.push({measured, stacking: now.map(w => w.get_title())});
    const notes = keyedWindows(session);
    for (const [key, frame] of Object.entries(keyed)) {
        const w = notes.get(key);
        check(`${key}: placed by its key`, w && same(r(w.get_frame_rect()), frame),
            w ? `${show(w.get_frame_rect())} vs ${show(frame)}` : 'missing');
    }
    let handed = null;
    try {
        handed = JSON.parse(new TextDecoder().decode(GLib.file_get_contents(`${out}/keyed-restored.json`)[1]));
    } catch {}
    check('LumaUI app got its own state back', handed?.reason === 'restore' &&
        handed.windows.map(([k, st]) => `${k}=${st.text}`).sort().join(',') ===
        'note:1=first note,note:2=second note', JSON.stringify(handed));
    for (const e of expected.filter(x => x.title !== 'Notes')) {
        const m = measured.find(w => w.title === e.title);
        if (!m) {
            check(`${e.title}: reopened`, false);
            continue;
        }
        check(`${e.title}: frame`, same(m.frame, e.frame), `${show(m.frame)} vs ${show(e.frame)}`);
        check(`${e.title}: floating rectangle`, same(m.floating, e.floating),
            `${show(m.floating)} vs ${show(e.floating)}`);
        check(`${e.title}: state`, m.maximized === e.maximized && m.fullscreen === e.fullscreen &&
            m.minimized === e.minimized && m.above === e.above,
            JSON.stringify([m.maximized, m.fullscreen, m.minimized, m.above]));
        check(`${e.title}: workspace and monitor`, m.workspace === e.workspace && m.monitor === e.monitor,
            `${m.workspace}/${m.monitor} vs ${e.workspace}/${e.monitor}`);
    }
    // What a person sees: the order of the windows on each workspace.
    const wsOf = t => expected.find(e => e.title === t)?.workspace;
    for (const ws of [...new Set(expected.map(e => e.workspace))]) {
        const want = stacking.filter(t => wsOf(t) === ws);
        const got = now.map(w => w.get_title()).filter(t => wsOf(t) === ws);
        check(`stacking order on workspace ${ws}`, JSON.stringify(got) === JSON.stringify(want),
            `${got.join(' < ')} vs ${want.join(' < ')}`);
    }
    const focus = expected.find(e => e.focus)?.title;
    check('focus', global.display.focus_window?.get_title() === focus,
        `${global.display.focus_window?.get_title()} vs ${focus}`);
    check('one window per saved window', measured.length === expected.length,
        `${measured.length} vs ${expected.length}`);
}

async function remapPhase(session) {
    const snapshot = JSON.parse(new TextDecoder().decode(
        GLib.file_get_contents(`${out}/snapshot.json`)[1]));
    // A different device: its own monitors, nothing open yet.
    snapshot.device = 'another-device';
    session.restore(snapshot, 'continue');
    await waitRestore(session);
    const area = global.workspace_manager.get_active_workspace().get_work_area_for_monitor(0);
    const measured = appWindows().map(describe);
    records.push({measured, area: r(area)});
    for (const saved of snapshot.windows) {
        const m = measured.find(w => w.title === saved.title);
        if (!m) {
            check(`${saved.title}: reopened`, false);
            continue;
        }
        const inside = m.frame.x >= area.x && m.frame.y >= area.y &&
            m.frame.x + m.frame.width <= area.x + area.width &&
            m.frame.y + m.frame.height <= area.y + area.height;
        check(`${saved.title}: inside the work area`, inside || m.fullscreen, show(m.frame));
        check(`${saved.title}: state kept`, m.maximized === (saved.state === 'maximized') &&
            m.fullscreen === (saved.state === 'fullscreen') && m.minimized === saved.minimized,
            JSON.stringify([m.maximized, m.fullscreen, m.minimized]));
        if (saved.state === 'floating') {
            const s = saved.frameShare;
            const got = {
                x: (m.frame.x - area.x) / area.width, y: (m.frame.y - area.y) / area.height,
                width: m.frame.width / area.width, height: m.frame.height / area.height,
            };
            const near = Math.abs(got.width - s.width) < 0.02 && Math.abs(got.height - s.height) < 0.02;
            check(`${saved.title}: share of the work area`, near,
                `${JSON.stringify(got)} vs ${JSON.stringify(s)}`);
        }
    }
}

async function main() {
    trace(`started ${phase}`);
    await sleep(2000);
    try {
        const {LumaSession} = await import(`file://${modulePath}`);
        const session = new LumaSession();
        if (phase === 'save')
            await savePhase(session);
        else if (phase === 'restore')
            await restorePhase(session);
        else
            await remapPhase(session);
    } catch (error) {
        check('scenario completed', false, String(error.stack));
    }
    let previous = [];
    try {
        previous = JSON.parse(new TextDecoder().decode(GLib.file_get_contents(`${out}/result.json`)[1])).results;
    } catch {}
    GLib.file_set_contents(`${out}/result.json`,
        JSON.stringify({results: [...previous, ...results], records}, null, 1));
    global.context.terminate();
}

export function init() {
    GLib.timeout_add(GLib.PRIORITY_DEFAULT, 4000, () => {
        main();
        return GLib.SOURCE_REMOVE;
    });
}

export async function run() {
    await new Promise(() => {});
}
