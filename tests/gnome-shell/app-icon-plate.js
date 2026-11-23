// SPDX-License-Identifier: GPL-2.0-or-later
// The LumaUI app-icon plate (0389): a v4 icon's 860/1024 plate is found and
// scaled to fill its tile, an icon already cropped to its plate is drawn as
// it is, and a legacy, raster or unreadable icon has no plate (the Shell
// draws one behind it). Detection is read once per file and modification time.
// Run: gjs -m app-icon-plate.js js/ui/lumaAppIconPlate.js
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

const module = await import(Gio.File.new_for_path(ARGV[0]).get_uri());
const {plateScaleFromSvg, plateScaleForFile, platedIconSize} = module;

function assert(value, message) {
    if (!value)
        throw new Error(`FAIL: ${message}`);
}

const DEFS = '<defs><clipPath id="c"><rect x="0" y="0" width="10" height="10"/></clipPath></defs>';
const V4 = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
${DEFS}<g id="plate"><rect x="82" y="82" width="860" height="860" rx="202" fill="#5d6780"/></g>
<path d="M300 300h400v400H300z" fill="#fff"/></svg>`;
const CROPPED = V4.replace('viewBox="0 0 1024 1024"', 'viewBox="82 82 860 860"');
const LEGACY = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">
<path d="M8 8h112v112H8z" fill="#3584e4"/><rect x="20" y="20" width="88" height="88" rx="12"/></svg>`;
const OFF_CENTRE = V4.replace('x="82" y="82"', 'x="40" y="82"');
const PNG = GLib.base64_decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==');

const near = (a, b) => Math.abs(a - b) < 1e-9;
assert(near(plateScaleFromSvg(V4), 1024 / 860), 'a v4 icon fills its tile at 1024/860');
assert(near(plateScaleFromSvg(CROPPED), 1), 'an icon cropped to its plate is drawn as it is');
assert(plateScaleFromSvg(LEGACY) === null, 'a legacy icon drawing art first has no plate');
assert(plateScaleFromSvg(OFF_CENTRE) === null, 'a square off the canvas centre is not a plate');
assert(plateScaleFromSvg('not an svg') === null, 'unreadable text has no plate');

assert(platedIconSize(34, 1024 / 860) === 42, '34 px tile: 42');
assert(platedIconSize(16, 1024 / 860) === 20, '16 px badge: 20');
assert(platedIconSize(32, 1024 / 860) === 40, '32 px row: 40');
assert(platedIconSize(48, 1024 / 860) === 58, '48 px TV row: 58');
assert(platedIconSize(30, 1024 / 860) === 36, '30 px card: 36');
assert(platedIconSize(34, 1) === 34 && platedIconSize(34, null) === 34, 'no scale, no change');
for (const size of [16, 30, 32, 34, 48])
    assert(platedIconSize(size, 1024 / 860) * 860 / 1024 >= size, `the plate covers ${size}`);

const dir = GLib.dir_make_tmp('luma-plate-XXXXXX');
const write = (name, bytes) => {
    const path = GLib.build_filenamev([dir, name]);
    GLib.file_set_contents(path, bytes);
    return path;
};
const setTime = (path, seconds) => {
    const file = Gio.File.new_for_path(path);
    file.set_attribute_uint64('time::modified', seconds, Gio.FileQueryInfoFlags.NONE, null);
    file.set_attribute_uint32('time::modified-usec', 0, Gio.FileQueryInfoFlags.NONE, null);
};
try {
    const v4 = write('app.svg', V4);
    const legacy = write('legacy.svg', LEGACY);
    const raster = write('app.png', PNG);
    assert(near(plateScaleForFile(v4), 1024 / 860), 'a v4 file has its plate');
    assert(plateScaleForFile(legacy) === null, 'a legacy file has none');
    assert(plateScaleForFile(raster) === null, 'a raster has none');
    assert(plateScaleForFile(GLib.build_filenamev([dir, 'missing.svg'])) === null, 'a missing file has none');
    assert(plateScaleForFile(null) === null, 'no file, no plate');

    // Read once per path and modification time.
    setTime(v4, 1000000);
    assert(near(plateScaleForFile(v4), 1024 / 860), 'read at its time');
    GLib.file_set_contents(v4, LEGACY);
    setTime(v4, 1000000);
    assert(near(plateScaleForFile(v4), 1024 / 860), 'the same time is not read again');
    setTime(v4, 1000001);
    assert(plateScaleForFile(v4) === null, 'a new time is read again');
} finally {
    for (const name of ['app.svg', 'legacy.svg', 'app.png'])
        GLib.unlink(GLib.build_filenamev([dir, name]));
    GLib.rmdir(dir);
}

print('app icon plate: ok');
