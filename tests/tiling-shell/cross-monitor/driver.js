// SPDX-License-Identifier: GPL-2.0-or-later
// Cross-monitor drags with a different layout on each monitor: the drawn
// zones, the selection and the drop on the monitor under the pointer must be
// that monitor's layout exactly, whatever the drag carried in from the other
// monitor. Run by run.sh inside gnome-shell --automation-script.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Clutter from 'gi://Clutter';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const UUID = 'tilingshell@ferrarodomenico.com';
const env = (k, d) => GLib.getenv(k) ?? d;
const out = env('LUMA_XM_OUT', '/tmp');
const wantMainScale = Number(env('LUMA_XM_MAIN_SCALE', '1'));
const wantSideScale = Number(env('LUMA_XM_SIDE_SCALE', '1'));
const sideLayoutName = env('LUMA_XM_SIDE_LAYOUT', 'rows');
const trace = env('LUMA_XM_TRACE', '0') === '1';
const fast = env('LUMA_XM_FAST', '0') === '1';
const reconfig = env('LUMA_XM_RECONFIG', '0') === '1';
const wsIndex = Number(env('LUMA_XM_WS', '0'));
const results = [];
const shots = [];
const r4 = r => r ? { x: r.x, y: r.y, width: r.width, height: r.height } : null;
const same = (a, b, tol = 1) => a && b && Math.abs(a.x - b.x) <= tol && Math.abs(a.y - b.y) <= tol &&
  Math.abs(a.width - b.width) <= tol && Math.abs(a.height - b.height) <= tol;
const inside = (p, r) => p.x >= r.x && p.x < r.x + r.width && p.y >= r.y && p.y < r.y + r.height;
const pause = ms => new Promise(res => GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => { res(); return GLib.SOURCE_REMOVE; }));
const check = (name, ok, detail = '') => {
  results.push({ name, ok: !!ok, detail: ok ? '' : String(detail) });
  print(`${ok ? 'PASS' : 'FAIL'} ${name}${ok ? '' : ' -- ' + detail}`);
  return !!ok;
};
const call = (method, args) => new Promise((resolve, reject) => Gio.DBus.session.call(
  'org.gnome.Mutter.DisplayConfig', '/org/gnome/Mutter/DisplayConfig', 'org.gnome.Mutter.DisplayConfig',
  method, args, null, Gio.DBusCallFlags.NONE, 5000, null,
  (c, res) => { try { resolve(c.call_finish(res)); } catch (e) { reject(e); } }));
async function shot(name) {
  const file = `${out}/${name}.png`;
  const stream = Gio.File.new_for_path(file).replace(null, false, Gio.FileCreateFlags.NONE, null);
  try {
    const [content] = await new Shell.Screenshot().screenshot_stage_to_content();
    const texture = content.get_texture();
    await Shell.Screenshot.composite_to_stream(texture, 0, 0, -1, -1, 1, null, 0, 0, 1, stream);
  } catch (e) {
    print(`XM screenshot ${name}: ${e}`);
  }
  stream.close(null);
  shots.push(file);
}

// Layouts. The main monitor's is the reported one: two rows on the left,
// one big centre zone, two rows on the right.
const tile = (x, y, width, height) => ({ x, y, width, height, groups: [] });
const LAYOUTS = {
  rails: [tile(0, 0, .25, .5), tile(0, .5, .25, .5), tile(.25, 0, .5, 1), tile(.75, 0, .25, .5), tile(.75, .5, .25, .5)],
  rows: [tile(0, 0, 1, .5), tile(0, .5, 1, .5)],
  quarters: [tile(0, 0, .5, .5), tile(.5, 0, .5, .5), tile(0, .5, .5, .5), tile(.5, .5, .5, .5)],
  thirds: [tile(0, 0, 1 / 3, 1), tile(1 / 3, 0, 1 / 3, 1), tile(2 / 3, 0, 1 / 3, 1)],
  rails22: [tile(0, 0, .22, .5), tile(0, .5, .22, .5), tile(.22, 0, .56, 1), tile(.78, 0, .22, .5), tile(.78, .5, .22, .5)],
  halves: [tile(0, 0, 1, .5), tile(0, .5, 1, .4999999999999999)],
  full: [tile(0, 0, 1, 1)],
  split: [tile(0, 0, .5, 1), tile(.5, 0, .5, 1)],
  // Rows at a third: no split line of the main layout lines up with it.
  uneven: [tile(0, 0, 1, 1 / 3), tile(0, 1 / 3, .6, 2 / 3), tile(.6, 1 / 3, .4, 2 / 3)],
};

let ext, TileUtils, mainMon, sideMon, window;
let GlobalStateIds = () => null;

function managerOf(monitor) {
  return ext._tilingManagers.find(m => m._monitor.index === monitor.index);
}

// What the drawn layout must be, worked out from the layout's fractions and
// the manager's work area and gaps, independently of the drawn actors.
function expectedTiles(manager, tiles) {
  const tl = manager._workspaceTilingLayout.get(window.get_workspace());
  const wa = manager._workArea, o = tl.outerGaps;
  const container = { x: wa.x + o.left, y: wa.y + o.top, width: wa.width - o.left - o.right, height: wa.height - o.top - o.bottom };
  return tiles.map(t => {
    const r = r4(TileUtils.apply_props(t, container));
    const near = (a, b) => Math.abs(a - b) <= 1;
    const top = near(r.y, container.y), left = near(r.x, container.x);
    const right = near(r.x + r.width, container.x + container.width), bottom = near(r.y + r.height, container.y + container.height);
    if (top) { r.y -= o.top; r.height += o.top; }
    if (left) { r.x -= o.left; r.width += o.left; }
    if (right) r.width += o.right;
    if (bottom) r.height += o.bottom;
    return { rect: r, edges: { top, left, right, bottom } };
  });
}

function actorRect(a) {
  const [x, y] = a.get_transformed_position();
  const [w, h] = a.get_transformed_size();
  return { x: Math.round(x), y: Math.round(y), width: Math.round(w), height: Math.round(h) };
}

// Everything a person sees and can drop into, checked against the layout of
// the monitor under the pointer.
function assertState(label, p, monitor, tiles, anchor = null) {
  const manager = managerOf(monitor);
  const tl = manager._workspaceTilingLayout.get(window.get_workspace());
  const want = expectedTiles(manager, tiles);
  const previews = tl._previews;
  const sel = manager._selectedTilesPreview;
  if (trace) print('XM_TRACE ' + JSON.stringify({ label, p, monitor: monitor.index, workArea: r4(manager._workArea), ws: window.get_workspace().index(), active: global.workspace_manager.get_active_workspace_index(), nws: global.workspace_manager.get_n_workspaces(), layout: tl.layout?.id, selected: ext.getSettings().get_value('selected-layouts').deep_unpack(), layouts: GlobalStateIds(),
    previews: previews.map(v => ({ rect: r4(v.rect), orig: r4(v.originalRect), restore: v.canRestore, showing: v.showing })),
    sel: { showing: sel.showing, rect: r4(sel.rect), gaps: [sel.gaps.top, sel.gaps.right, sel.gaps.bottom, sel.gaps.left] } }));
  let ok = true;
  ok &= check(`${label}: layout of monitor ${monitor.index} is drawn`, tl.showing && tl.visible);
  ok &= check(`${label}: ${tiles.length} zones, no extra pieces`, previews.length === tiles.length && previews.every(v => v.canRestore),
    JSON.stringify(previews.map(v => [r4(v.rect), v.canRestore])));
  const unmatched = want.filter(w => !previews.some(v => same(r4(v.originalRect), w.rect, 0)));
  ok &= check(`${label}: zones are monitor ${monitor.index}'s layout`, unmatched.length === 0,
    JSON.stringify({ missing: unmatched.map(w => w.rect), drawn: previews.map(v => r4(v.originalRect)) }));
  const pointed = want.find(w => inside(p, w.rect));
  ok &= check(`${label}: pointer is over a zone`, !!pointed, JSON.stringify(p));
  if (!pointed) return false;
  const under = anchor ? wholeZones(want, pointed.rect, want.find(w => inside(anchor, w.rect)).rect) : pointed;
  // Every zone keeps its own place; only the zones selected give way.
  const moved = previews.filter(v => !same(r4(v.rect), r4(v.originalRect), 0));
  ok &= check(`${label}: no zone is cut or moved`, moved.length === 0, JSON.stringify(moved.map(v => [r4(v.rect), r4(v.originalRect)])));
  const hidden = previews.filter(v => !v.showing).map(v => r4(v.originalRect));
  const covered = want.filter(w => w.rect.x >= under.rect.x && w.rect.y >= under.rect.y &&
    w.rect.x + w.rect.width <= under.rect.x + under.rect.width && w.rect.y + w.rect.height <= under.rect.y + under.rect.height);
  ok &= check(`${label}: exactly the selected zones give way`, hidden.length === covered.length && covered.every(w => hidden.some(h => same(h, w.rect, 0))),
    JSON.stringify({ hidden, selected: covered.map(w => w.rect) }));
  for (const v of previews.filter(v => v.showing)) {
    const a = actorRect(v), inner = { x: v.innerX, y: v.innerY, width: v.innerWidth, height: v.innerHeight };
    if (!same(a, inner, 1) || v.opacity < 255 || !v.visible) {
      ok &= check(`${label}: zone ${JSON.stringify(r4(v.rect))} is painted where it is`, false, JSON.stringify({ actor: a, inner, opacity: v.opacity, visible: v.visible }));
    }
  }
  // The selection is the cell of the zone under the pointer, with the inner
  // gap on every side that meets another zone and rounded corners there.
  const cell = r4(manager._lumaInCells(under.rect));
  ok &= check(`${label}: selection is that zone's cell`, sel.showing && same(r4(sel.rect), cell, 1),
    JSON.stringify({ selection: r4(sel.rect), cell }));
  const g = sel.gaps, inner = tl.innerGaps;
  const wantGap = edge => under.edges[edge] ? 0 : inner[edge] / 2;
  ok &= check(`${label}: selection keeps the gaps`, ['top', 'right', 'bottom', 'left'].every(e => Math.abs(g[e] - wantGap(e) * (manager._enableScaling ? tl.scalingFactor : 1)) <= 1),
    JSON.stringify({ gaps: [g.top, g.right, g.bottom, g.left], edges: under.edges }));
  const a = actorRect(sel), selInner = { x: sel.innerX, y: sel.innerY, width: sel.innerWidth, height: sel.innerHeight };
  ok &= check(`${label}: selection is painted where it is`, sel.visible && sel.opacity === 255 && same(a, selInner, 1),
    JSON.stringify({ actor: a, inner: selInner, opacity: sel.opacity }));
  const corners = ['top-left', 'top-right', 'bottom-right', 'bottom-left'].filter(c => sel.has_style_class_name(`${c}-border-radius`));
  const wantCorners = [['top', 'left'], ['top', 'right'], ['bottom', 'right'], ['bottom', 'left']]
    .filter(([v, h]) => !under.edges[v] && !under.edges[h]).map(([v, h]) => `${v}-${h}`);
  ok &= check(`${label}: selection corners`, JSON.stringify(corners) === JSON.stringify(wantCorners), JSON.stringify({ corners, wantCorners }));
  // No other monitor shows anything.
  for (const other of ext._tilingManagers.filter(m => m !== manager)) {
    const otl = other._workspaceTilingLayout.get(window.get_workspace());
    ok &= check(`${label}: monitor ${other._monitor.index} shows nothing`, !otl?.showing && !other._selectedTilesPreview.showing);
  }
  return ok;
}

// Whole zones: the smallest union of zones containing both rectangles, with
// the screen edges it touches.
function wholeZones(want, a, b) {
  const overlap = (r, q) => Math.min(r.x + r.width, q.x + q.width) - Math.max(r.x, q.x) > 1 &&
    Math.min(r.y + r.height, q.y + q.height) - Math.max(r.y, q.y) > 1;
  const union = (r, q) => {
    const x = Math.min(r.x, q.x), y = Math.min(r.y, q.y);
    return { x, y, width: Math.max(r.x + r.width, q.x + q.width) - x, height: Math.max(r.y + r.height, q.y + q.height) - y };
  };
  let rect = union(a, b);
  for (let changed = true; changed; ) {
    changed = false;
    for (const w of want) {
      if (!overlap(w.rect, rect)) continue;
      const next = union(rect, w.rect);
      if (!same(next, rect, 0)) { rect = next; changed = true; }
    }
  }
  const edge = (k, f) => want.some(w => w.edges[k] && Math.abs(f(w.rect) - f(rect)) <= 1);
  return { rect, edges: { top: edge('top', r => r.y), left: edge('left', r => r.x),
    right: edge('right', r => r.x + r.width), bottom: edge('bottom', r => r.y + r.height) } };
}

const keyboard = Clutter.get_default_backend().get_default_seat().create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
const alt = state => keyboard.notify_keyval(GLib.get_monotonic_time(), Clutter.KEY_Alt_L, state);
const pointer = Clutter.get_default_backend().get_default_seat().create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
let at = { x: 0, y: 0 };
async function moveTo(x, y, steps = 8, ms = 12) {
  if (fast) { steps = 2; ms = 4; }
  const from = { ...at }, samples = [];
  for (let i = 1; i <= steps; i++) {
    at = { x: from.x + (x - from.x) * i / steps, y: from.y + (y - from.y) * i / steps };
    samples.push({ ...at });
    pointer.notify_absolute_motion(GLib.get_monotonic_time(), at.x, at.y);
    await pause(ms);
  }
  await pause(220); // the tiler polls every 15 ms; let the preview settle
  return samples;
}
const monitorAt = p => Main.layoutManager.monitors.find(m => p.x >= m.x && p.x < m.x + m.width && p.y >= m.y && p.y < m.y + m.height);
const button = state => pointer.notify_button(GLib.get_monotonic_time(), Clutter.BUTTON_PRIMARY, state);

function zonePoint(monitor, tiles, i, fx = .5, fy = .5) {
  const z = expectedTiles(managerOf(monitor), tiles)[i].rect;
  return { x: Math.round(z.x + z.width * fx), y: Math.round(z.y + z.height * fy) };
}

async function placeWindowOn(monitor) {
  if (window.maximized_horizontally || window.maximized_vertically) {
    try { window.unmaximize(); } catch (e) { window.unmaximize(Meta.MaximizeFlags.BOTH); }
  }
  const wa = Main.layoutManager.getWorkAreaForMonitor(monitor.index);
  window.move_resize_frame(false, wa.x + Math.round(wa.width * .3), wa.y + Math.round(wa.height * .3), 640, 420);
  window.activate(global.get_current_time());
  await pause(500);
}

// One drag. `path` is a list of [x, y, monitor, tiles, label]; a null
// monitor is a waypoint with no checks. The drop happens at the last point
// and must fill exactly that zone of that monitor.
async function drag(name, start, path) {
  await placeWindowOn(start);
  const f = window.get_frame_rect();
  at = { x: f.x + 200, y: f.y + 18 };
  pointer.notify_absolute_motion(GLib.get_monotonic_time(), at.x, at.y);
  await pause(150);
  button(Clutter.ButtonState.PRESSED);
  await pause(150);
  await moveTo(at.x + 30, at.y + 10, 4);
  let n = 0, anchor = null;
  try {
    for (const [x, y, monitor, tiles, label, opts = {}] of path) {
      if (opts.alt === 'press') { alt(Clutter.KeyState.PRESSED); anchor = { ...at }; await pause(120); }
      if (opts.alt === 'release') { alt(Clutter.KeyState.RELEASED); anchor = null; await pause(120); }
      const samples = await moveTo(x, y);
      // Spanning starts again from the first zone reached on another monitor.
      if (anchor && monitor && monitorAt(anchor) !== monitor)
        anchor = samples.find(q => monitorAt(q) === monitor && expectedTiles(managerOf(monitor), tiles).some(w => inside(q, w.rect))) ?? anchor;
      if (!monitor) continue;
      const ok = assertState(`${name} ${label}`, { x, y }, monitor, tiles, anchor);
      if (!ok || n++ === 0) await shot(`${name}-${label.replace(/\W+/g, '-')}`);
    }
  } finally {
    button(Clutter.ButtonState.RELEASED);
    // A person lets go of the button, then of Alt.
    if (anchor) { await pause(200); alt(Clutter.KeyState.RELEASED); }
  }
  await pause(900);
  const [x, y, monitor, tiles] = path.at(-1);
  const manager = managerOf(monitor);
  const zones = expectedTiles(manager, tiles), pointed = zones.find(w => inside({ x, y }, w.rect));
  const region = anchor ? wholeZones(zones, pointed.rect, zones.find(w => inside(anchor, w.rect)).rect).rect : pointed.rect;
  const want = r4(manager._lumaWindowRect(manager._lumaCellTile(TileUtils.apply_props({ x: 0, y: 0, width: 1, height: 1 }, region))));
  const got = r4(window.get_frame_rect());
  check(`${name}: drop fills the zone under the pointer on monitor ${monitor.index}`,
    window.get_monitor() === monitor.index && !!window.assignedTile && same(got, want, 2), JSON.stringify({ got, want, monitor: window.get_monitor() }));
}

async function work() {
  let [serial, physical] = (await call('GetCurrentState', null)).deep_unpack();
  const three = physical.length === 3;
  if (!check('two or three virtual monitors', physical.length === 2 || three, physical.length)) return;
  const pick = (modes, want) => {
    const m = modes.find(x => x[6]['is-current']?.deep_unpack()) ?? modes[0];
    const scales = m[5];
    const s = scales.reduce((b, c) => Math.abs(c - want) < Math.abs(b - want) ? c : b, scales[0]);
    return { id: m[0], width: m[1], height: m[2], scale: s };
  };
  const outs = physical.map(([spec, modes]) => ({ connector: spec[0], modes }));
  outs.sort((a, b) => b.modes[0][1] - a.modes[0][1]); // widest first
  const mainMode = pick(outs[0].modes, wantMainScale), sideMode = pick(outs[1].modes, wantSideScale);
  const mainW = Math.round(mainMode.width / mainMode.scale);
  if (three) {
    // A laptop panel left of the two, at 1.5, then closed: the monitor count
    // changes under Tiling Shell, as it does when a lid closes.
    const lap = pick(outs[2].modes, 1.5), lapW = Math.round(lap.width / lap.scale);
    await call('ApplyMonitorsConfig', new GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})', [serial, 1, [
      [0, 0, lap.scale, 0, false, [[outs[2].connector, lap.id, {}]]],
      [lapW, 0, mainMode.scale, 0, true, [[outs[0].connector, mainMode.id, {}]]],
      [lapW + mainW, 0, sideMode.scale, 0, false, [[outs[1].connector, sideMode.id, {}]]],
    ], { 'layout-mode': new GLib.Variant('u', 1) }]));
    await pause(2500);
    print(`XM three ${JSON.stringify(Main.layoutManager.monitors.map(m => [m.index, m.x, m.width]))} managers ${Main.extensionManager.lookup(UUID)?.stateObj?._tilingManagers.length}`);
    [serial] = (await call('GetCurrentState', null)).deep_unpack();
  }
  const logical = [
    [0, 0, mainMode.scale, 0, true, [[outs[0].connector, mainMode.id, {}]]],
    [mainW, 0, sideMode.scale, 0, false, [[outs[1].connector, sideMode.id, {}]]],
  ];
  if (env('LUMA_XM_SIDE_FIRST', '0') === '1') logical.reverse();
  await call('ApplyMonitorsConfig', new GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})', [serial, 1, logical,
    { 'layout-mode': new GLib.Variant('u', 1) }]));
  await pause(2000);
  if (reconfig) {
    // The same two monitors, rearranged and put back: monitor objects and
    // work areas are rebuilt while the count stays the same.
    const [serial2] = (await call('GetCurrentState', null)).deep_unpack();
    const sideW = Math.round(sideMode.width / sideMode.scale);
    await call('ApplyMonitorsConfig', new GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})', [serial2, 1, [
      [sideW, 0, mainMode.scale, 0, true, [[outs[0].connector, mainMode.id, {}]]],
      [0, 0, sideMode.scale, 0, false, [[outs[1].connector, sideMode.id, {}]]],
    ], { 'layout-mode': new GLib.Variant('u', 1) }]));
    await pause(1500);
    const [serial3] = (await call('GetCurrentState', null)).deep_unpack();
    await call('ApplyMonitorsConfig', new GLib.Variant('(uua(iiduba(ssa{sv}))a{sv})', [serial3, 1, [
      [0, 0, mainMode.scale, 0, true, [[outs[0].connector, mainMode.id, {}]]],
      [mainW, 0, sideMode.scale, 0, false, [[outs[1].connector, sideMode.id, {}]]],
    ], { 'layout-mode': new GLib.Variant('u', 1) }]));
    await pause(2000);
  }
  print(`XM monitors ${JSON.stringify(Main.layoutManager.monitors.map(m => [m.index, m.x, m.y, m.width, m.height, global.display.get_monitor_scale(m.index)]))}`);
  mainMon = Main.layoutManager.monitors.find(m => m.x === 0);
  sideMon = Main.layoutManager.monitors.find(m => m.x !== 0);
  check('main and side monitors', mainMon && sideMon);

  ext = Main.extensionManager.lookup(UUID)?.stateObj;
  if (!check('Tiling Shell is running', !!ext)) return;
  TileUtils = (await import(`file:///usr/share/gnome-shell/extensions/${UUID}/components/layout/TileUtils.js`)).default;
  const GS = (await import(`file:///usr/share/gnome-shell/extensions/${UUID}/utils/globalState.js`)).default;
  GlobalStateIds = () => GS.get().layouts.map(l => l.id);
  check('one tiling manager per monitor', ext._tilingManagers.length === 2);
  const settings = ext.getSettings();
  settings.set_boolean('enable-tiling-system', true);
  settings.set_boolean('show-layout-while-dragging', true);
  settings.set_boolean('enable-span-multiple-tiles', true);
  settings.set_boolean('enable-autotiling', false);
  if (env('LUMA_XM_SPAN_KEY', '')) settings.set_strv('span-multiple-tiles-activation-key', [env('LUMA_XM_SPAN_KEY', '')]);
  let layouts, selected;
  if (sideLayoutName === 'reported') {
    // The reporting setup's own data: three entries per workspace for a
    // laptop that is closed, two workspaces listed for four, the ultrawide
    // second, the other monitor first with top and bottom halves.
    layouts = [{ id: 'Luma Split', tiles: LAYOUTS.split }, { id: 'Layout 1', tiles: LAYOUTS.rails22 },
      { id: 'Layout 2', tiles: LAYOUTS.thirds }, { id: '1791833165864', tiles: LAYOUTS.full },
      { id: '1791854190171', tiles: LAYOUTS.halves }];
    const row = [];
    row[sideMon.index] = '1791854190171'; row[mainMon.index] = 'Layout 1'; row[2] = '1791833165864';
    const row1 = [];
    row1[sideMon.index] = 'Luma Split'; row1[mainMon.index] = 'Layout 1'; row1[2] = 'Luma Split';
    selected = [row, row1];
  } else {
    layouts = [{ id: 'xm-rails', tiles: LAYOUTS.rails }];
    if (sideLayoutName !== 'none') layouts.push({ id: `xm-${sideLayoutName}`, tiles: LAYOUTS[sideLayoutName] });
    const perWs = [];
    perWs[mainMon.index] = 'xm-rails';
    if (sideLayoutName !== 'none') perWs[sideMon.index] = `xm-${sideLayoutName}`;
    // With no entry of its own, a monitor follows the first one's layout.
    selected = Array.from({ length: 4 }, () => sideLayoutName === 'none' ? ['xm-rails'] : perWs);
  }
  settings.set_string('layouts-json', JSON.stringify(layouts));
  settings.set_value('selected-layouts', new GLib.Variant('aas', selected));
  await pause(800);
  if (wsIndex > 0) {
    const wm = global.workspace_manager;
    while (wm.get_n_workspaces() <= wsIndex) wm.append_new_workspace(false, global.get_current_time());
    wm.get_workspace_by_index(wsIndex).activate(global.get_current_time());
    await pause(800);
  }
  // What each monitor must show on the active workspace, by the stored
  // selection: a workspace without an entry uses the first one's, a monitor
  // without an entry the first monitor's.
  const lookup = monitor => {
    const ws = global.workspace_manager.get_active_workspace_index();
    const row = selected[ws] ?? selected[0];
    const id = row[monitor.index] ?? row[0];
    return (layouts.find(l => l.id === id) ?? layouts[0]).tiles;
  };
  const sideLayout = lookup(sideMon);
  print(`XM layouts main ${JSON.stringify(lookup(mainMon))} side ${JSON.stringify(sideLayout)}`);

  const child = Gio.Subprocess.new(['python3', env('LUMA_XM_APP', ''), 'org.projectluma.CrossMonitorProbe'], Gio.SubprocessFlags.NONE);
  try {
    for (let i = 0; i < 80 && !window; i++) {
      window = global.get_window_actors().find(a => a.mapped && a.meta_window.get_title() === 'Shelf test')?.meta_window;
      if (!window) await pause(100);
    }
    if (!check('probe window', !!window)) return;
    const R = lookup(mainMon);
    const m = mainMon, s = sideMon;
    const zm = (i, fx, fy) => zonePoint(m, R, i, fx, fy);
    const zs = (i, fx, fy) => zonePoint(s, sideLayout, i, fx, fy);
    const sideTop = { x: s.x + s.width * .5, y: s.y + s.height * .25 };
    const sideBottom = { x: s.x + s.width * .5, y: s.y + s.height * .75 };
    const pt = (p, mon, tiles, label, opts) => [p.x, p.y, mon, tiles, label, opts];
    // Side -> main at the height of the top-right zone, across the centre,
    // down the centre, back to the top-right zone and down from it.
    await drag('in-top', s, [
      pt(sideTop, null), pt(zm(3, .7, .5), m, R, 'top-right'), pt(zm(2, .5, .25), m, R, 'centre-high'),
      pt(zm(2, .5, .8), m, R, 'centre-low'), pt(zm(3, .5, .5), m, R, 'top-right-again'),
      pt(zm(4, .5, .5), m, R, 'down-to-bottom-right'), pt(zm(2, .5, .85), m, R, 'centre-bottom')]);
    await drag('in-bottom', s, [
      pt(sideBottom, null), pt(zm(4, .7, .5), m, R, 'bottom-right'), pt(zm(2, .5, .75), m, R, 'centre-low'),
      pt(zm(1, .5, .5), m, R, 'bottom-left'), pt(zm(0, .5, .5), m, R, 'top-left'), pt(zm(3, .5, .3), m, R, 'top-right')]);
    await drag('top-right-down', s, [
      pt(sideTop, null), pt(zm(3, .5, .3), m, R, 'top-right'), pt(zm(3, .5, .9), m, R, 'top-right-low'),
      pt(zm(4, .5, .2), m, R, 'bottom-right-high'), pt(zm(4, .5, .7), m, R, 'bottom-right')]);
    // Re-entry: main -> side -> main, twice, at the same and at new points.
    await drag('reentry', s, [
      pt(sideTop, null), pt(zm(2, .5, .3), m, R, 'centre'), pt(zs(0, .5, .5), s, sideLayout, 'side'),
      pt(zm(2, .5, .3), m, R, 'centre-same-point'), pt(zs(sideLayout.length - 1, .5, .5), s, sideLayout, 'side-last'),
      pt(zm(3, .5, .5), m, R, 'top-right'), pt(zm(2, .5, .9), m, R, 'centre-bottom')]);
    // Reverse: main -> side.
    await drag('reverse', m, [
      pt(zm(2, .5, .4), m, R, 'centre'), pt(zs(0, .5, .5), s, sideLayout, 'side-first'),
      pt(zs(sideLayout.length - 1, .5, .5), s, sideLayout, 'side-last')]);
    // And back again, dropped on main.
    await drag('back', s, [
      pt(zs(0, .5, .5), s, sideLayout, 'side-first'), pt(zm(4, .5, .5), m, R, 'bottom-right'),
      pt(zm(2, .5, .6), m, R, 'centre')]);
    // Spanning (Alt): whole zones from the zone where Alt went down to the
    // zone under the pointer, never a zone cut in two; released, one zone.
    await drag('span', s, [
      pt(sideTop, null), pt(zm(3, .5, .5), m, R, 'top-right'),
      pt(zm(4, .5, .5), m, R, 'span-down', { alt: 'press' }), pt(zm(2, .5, .3), m, R, 'span-centre'),
      pt(zm(0, .5, .5), m, R, 'span-all'), pt(zm(2, .5, .7), m, R, 'single-centre', { alt: 'release' }),
      pt(zm(3, .5, .5), m, R, 'span-centre-top-right', { alt: 'press' })]);
    // Spanning across the monitors: the side monitor's zones never join the
    // main monitor's.
    await drag('span-cross', s, [
      pt(zs(0, .5, .5), s, sideLayout, 'side', { alt: 'press' }), pt(zm(4, .5, .5), m, R, 'bottom-right'),
      pt(zm(1, .5, .5), m, R, 'bottom-row')]);
    // A finished drag leaves nothing connected to the stage behind.
    check('drags release their stage handlers', ext._tilingManagers.every(m => !m._lumaDragSignals),
      JSON.stringify(ext._tilingManagers.map(m => !!m._lumaDragSignals)));
  } finally {
    child.force_exit();
  }
}

export function init() {
  GLib.timeout_add(GLib.PRIORITY_DEFAULT, 4000, () => {
    work().catch(e => check('scenario completed', false, `${e}\n${e.stack}`)).finally(() => {
      GLib.file_set_contents(`${out}/result.json`, JSON.stringify({ pass: results.every(r => r.ok), results, shots }, null, 2));
      global.context.terminate();
    });
    return GLib.SOURCE_REMOVE;
  });
}
