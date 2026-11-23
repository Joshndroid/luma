# SPDX-License-Identifier: Apache-2.0
# Streamy: live streaming and recording on LumaUI, with its lumaframe
# GStreamer element (gst-lumaframe, libgstlumavideofx.so) that draws each
# picture widget's rounded corners, stroke and shadow on the GPU.
%global debug_package %{nil}

Name:           luma-streamy
Version:        0.1.0
Release:        1.luma.22%{?dist}
Summary:        Stream and record your screen, windows and camera
# The app: Apache-2.0. gst-lumaframe: MPL-2.0. Vendored crates (Cargo.lock):
# gstreamer-rs and glib-rs (MIT OR Apache-2.0, MIT), memchr (Unlicense OR
# MIT), target-lexicon (Apache-2.0 WITH LLVM-exception), unicode-ident
# ((MIT OR Apache-2.0) AND Unicode-3.0).
License:        Apache-2.0 AND MPL-2.0 AND MIT AND (MIT OR Apache-2.0) AND (Unlicense OR MIT) AND Apache-2.0 WITH LLVM-exception AND Unicode-3.0
URL:            https://simplyluma.com/
ExclusiveArch:  x86_64 aarch64
# scripts/packages/build-luma-streamy-source.sh: the app plus the plugin with
# every crate vendored against Cargo.lock, so the build needs no network.
Source0:        luma-streamy.tar.gz

BuildRequires:  cargo >= 1.92
BuildRequires:  rust >= 1.92
BuildRequires:  gcc
BuildRequires:  pkgconf-pkg-config
BuildRequires:  gstreamer1-devel
BuildRequires:  gstreamer1-plugins-base-devel
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
# %%check runs the plugin's GL pipelines on llvmpipe, a recording engine with
# drawn stand-in sources, and opens the window under a private X server.
BuildRequires:  gstreamer1-plugins-base-tools
BuildRequires:  gstreamer1-plugins-good
BuildRequires:  gstreamer1-plugins-bad-free
BuildRequires:  gstreamer1-plugin-gtk4
BuildRequires:  mesa-dri-drivers
BuildRequires:  mesa-libEGL
BuildRequires:  python3-gobject
BuildRequires:  python3-cairo
BuildRequires:  gtk4 >= 4.22
BuildRequires:  libadwaita >= 1.9
BuildRequires:  luma-developer-platform >= 0.1.0-1.luma.149
BuildRequires:  xorg-x11-server-Xvfb
BuildRequires:  dbus-daemon

Requires:       python3
Requires:       python3-gobject
Requires:       gtk4 >= 4.22
Requires:       libadwaita >= 1.9
# Dock (fitted, movable seams), NavigationList, ArrangeView with crop, ColorPicker and the AudioMeter.
Requires:       luma-developer-platform >= 0.1.0-1.luma.149
Requires:       google-figtree-fonts
# The program graph: GL upload, mixing and conversion (base), FLV/MKV/MP4
# muxers, PulseAudio capture and level meters (good), RTMP(S), VA-API
# encoders, parsers and AAC (bad-free), the preview sink (gtk4), and portal
# capture of displays, windows and cameras (pipewire).
Requires:       gstreamer1
Requires:       gstreamer1-plugins-base
Requires:       gstreamer1-plugins-good
Requires:       gstreamer1-plugins-bad-free
Requires:       gstreamer1-plugin-gtk4
Requires:       pipewire-gstreamer
# Software H.264 for computers without a hardware encoder.
Requires:       gstreamer1-plugin-openh264
Requires:       xdg-desktop-portal
# Chat sockets and sign-in (Soup 3 through GObject introspection).
Requires:       libsoup3
# SRT destinations. Not on aarch64: Raspberry Pi 5 replaces bad-free and
# bad-free-libs with its own build (rpm-ostree override), and Fedora's -extras
# requires the libs of its own release, so a generic ARM image carrying it
# cannot take the Pi override (card builds and Pi updates both fail).
%ifnarch aarch64
Recommends:     gstreamer1-plugins-bad-free-extras
%endif

%description
Streamy puts displays, windows, cameras, images and sound together in scenes
and sends them live to an RTMP(S) server or records them to MKV or MP4. Every
picture widget can have rounded corners, a stroke and a soft shadow, drawn on
the graphics card by the lumaframe GStreamer element this package includes.
Only the scene on screen captures, and nothing runs while it isn't in use.

%prep
%autosetup -n luma-streamy

%build
cd gst-lumaframe
export CARGO_HOME=$PWD/.cargo-home
cargo build --release --offline --locked -j %{_smp_build_ncpus}

%install
install -d %{buildroot}%{python3_sitelib}/luma_streamy
install -m 0644 luma_streamy/*.py %{buildroot}%{python3_sitelib}/luma_streamy/
install -D -m 0755 bin/streamy %{buildroot}%{_bindir}/streamy
install -D -m 0644 style/streamy.css %{buildroot}%{_datadir}/luma-streamy/streamy.css
install -D -m 0644 -t %{buildroot}%{_datadir}/luma-streamy/logos data/logos/*.svg
install -D -m 0644 data/org.projectluma.Streamy.desktop \
  %{buildroot}%{_datadir}/applications/org.projectluma.Streamy.desktop
install -D -m 0644 data/org.projectluma.Streamy.metainfo.xml \
  %{buildroot}%{_metainfodir}/org.projectluma.Streamy.metainfo.xml
install -D -m 0644 data/org.projectluma.Streamy.svg \
  %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/org.projectluma.Streamy.svg
install -D -m 0755 gst-lumaframe/target/release/libgstlumavideofx.so \
  %{buildroot}%{_libdir}/gstreamer-1.0/libgstlumavideofx.so
# Licence texts of every vendored crate, by crate.
(cd gst-lumaframe/vendor && find . -mindepth 2 -maxdepth 2 -type f \( -iname 'LICENSE*' -o -iname 'COPYING*' -o -iname 'NOTICE*' \) -print0 |
  while IFS= read -r -d '' file; do install -D -m 0644 "$file" "../../licenses/vendor/$file"; done)
install -D -m 0644 gst-lumaframe/LICENSE licenses/gst-lumaframe/LICENSE
found=$(find licenses/vendor -type f | wc -l)
[ "$found" -gt 20 ] || { echo "only $found vendored licence files were found" >&2; exit 1; }

%check
python3 -m py_compile %{buildroot}%{python3_sitelib}/luma_streamy/*.py
desktop-file-validate %{buildroot}%{_datadir}/applications/org.projectluma.Streamy.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/org.projectluma.Streamy.metainfo.xml
# Scene model, layouts, the pipeline graph, and the camera and sound device lists across a
# service that goes away and comes back.
PYTHONPATH=. python3 -B -m unittest discover -s tests -p 'test_*.py' -v
# Headless GL: Mesa's EGL surfaceless platform on llvmpipe.
export GST_GL_PLATFORM=egl GST_GL_WINDOW=surfaceless LIBGL_ALWAYS_SOFTWARE=1
(
  cd gst-lumaframe
  export CARGO_HOME=$PWD/.cargo-home
  # Geometry and shadow maths, then real GL pipelines through the element.
  cargo test --offline --locked -j %{_smp_build_ncpus} --lib
  cargo test --offline --locked -j %{_smp_build_ncpus} --test pipeline -- --test-threads=2
)
export GST_PLUGIN_PATH=%{buildroot}%{_libdir}/gstreamer-1.0 GST_REGISTRY=$PWD/gst-registry.bin
# The installed plugin registers the element.
gst-inspect-1.0 lumaframe | grep -q 'libgstlumavideofx.so'
# A failing or silent source comes out of the program without stalling it.
PYTHONPATH=%{buildroot}%{python3_sitelib} timeout 120 python3 -B tests/engine_failure.py
# A microphone meters at once, also beside a source that failed; a frameless widget draws its Look.
PYTHONPATH=%{buildroot}%{python3_sitelib} timeout 120 python3 -B tests/engine_levels.py
PYTHONPATH=%{buildroot}%{python3_sitelib} timeout 120 python3 -B tests/engine_placeholder.py
# Every capture kind and format reaches a picture, also reopened and beside an MJPEG camera.
# Half-size displays: llvmpipe builders composite a 3440x1440 cast at a few frames a second.
PYTHONPATH=%{buildroot}%{python3_sitelib} STREAMY_FORMATS_FAST=1 timeout 240 python3 -B tests/engine_formats.py
PYTHONPATH=%{buildroot}%{python3_sitelib} STREAMY_FORMATS_BESIDE=1 STREAMY_FORMATS_ONLY=display \
  timeout 120 python3 -B tests/engine_formats.py
# A resize is composited on the next canvas frame, never behind an older picture.
PYTHONPATH=%{buildroot}%{python3_sitelib} timeout 120 python3 -B tests/engine_resize_frame.py
# A recording and a stream row (FLV) carry the program's sound, also from an encoder that moves its
# timestamps (vah264enc); skipped where no H.264 and AAC encoder is installed (no OpenH264 here).
PYTHONPATH=%{buildroot}%{python3_sitelib} timeout 300 python3 -B tests/engine_stream_sound.py
# Every scene's sources stay open: a switch reopens nothing, a cut shows on the first output frame,
# a fade reaches full alpha in its time and a picture in both scenes glides; a parked camera shows.
PYTHONPATH=%{buildroot}%{python3_sitelib} STREAMY_FAKE_SOURCES=1 timeout 180 python3 -B tests/engine_scenes.py
# Per-destination picture size and frame rate (a 60 fps feed recorded at 360p60 and its own size at
# 30 fps) and a live bitrate change; skipped where no H.264 encoder or ffprobe is installed.
PYTHONPATH=%{buildroot}%{python3_sitelib} timeout 120 python3 -B tests/outputs_quality.py $PWD/quality-out
# The installed app opens on drawn stand-in sources and renders its window
# at phone and desktop widths. The first try waits 15 s: on a loaded builder
# the window can take that long to map while llvmpipe composites the scene.
mkdir -p check-home
# Every editor key and menu reaches a method; switching scenes or a relayout never moves a panel.
HOME=$PWD/check-home XDG_DATA_HOME=$PWD/check-home/data XDG_CONFIG_HOME=$PWD/check-home/config \
  XDG_CACHE_HOME=$PWD/check-home/cache PYTHONPATH=%{buildroot}%{python3_sitelib} \
  timeout 150 xvfb-run -a -s "-screen 0 1920x1080x24" dbus-run-session -- \
  env GDK_BACKEND=x11 GSK_RENDERER=cairo python3 -B tests/window_actions.py
# A preview leaving the program lets GTK's sink go on the main thread (elsewhere it aborts).
PYTHONPATH=%{buildroot}%{python3_sitelib} timeout 120 xvfb-run -a -s "-screen 0 1920x1080x24" \
  env GDK_BACKEND=x11 GSK_RENDERER=cairo python3 -B tests/engine_preview_teardown.py
for width in 360 1280; do
  HOME=$PWD/check-home XDG_DATA_HOME=$PWD/check-home/data XDG_CONFIG_HOME=$PWD/check-home/config \
    XDG_CACHE_HOME=$PWD/check-home/cache STREAMY_FAKE_SOURCES=1 STREAMY_SHOT_DELAY=15000 STREAMY_SHOT_TRIES=120 \
    STREAMY_SHOT=$PWD/shot-$width.png STREAMY_SHOT_WIDTH=$width \
    PYTHONPATH=%{buildroot}%{python3_sitelib} \
    timeout 240 xvfb-run -a -s "-screen 0 1920x1080x24" dbus-run-session -- \
    env GDK_BACKEND=x11 GSK_RENDERER=cairo python3 -B %{buildroot}%{_bindir}/streamy
  test -s shot-$width.png
done

%post
update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || :
gtk4-update-icon-cache --force --quiet %{_datadir}/icons/hicolor >/dev/null 2>&1 || :

%postun
update-desktop-database %{_datadir}/applications >/dev/null 2>&1 || :
gtk4-update-icon-cache --force --quiet %{_datadir}/icons/hicolor >/dev/null 2>&1 || :

%files
%license LICENSE.md licenses
%doc README.md
%{python3_sitelib}/luma_streamy/
%{_bindir}/streamy
%{_datadir}/luma-streamy/
%{_datadir}/applications/org.projectluma.Streamy.desktop
%{_metainfodir}/org.projectluma.Streamy.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/org.projectluma.Streamy.svg
%{_libdir}/gstreamer-1.0/libgstlumavideofx.so

%changelog
* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.22
- Each destination sets what it sends: picture size (the canvas's own, 720p
  or 480p), 30 or 60 fps, video bitrate and sound bitrate; the recording
  also sets its codec. Defaults come from the platform. Rows with different
  settings get their own encode; a smaller picture is scaled once per
  encode, on the GPU before a VA encoder. Each encode paces itself to its
  own frame rate, and a canvas's tap runs at its fastest encode's rate.
- Automatic bitrate (on by default for streams): an encode steps its
  bitrate down by a quarter while its rows fall behind (pictures waiting or
  dropped for the network), to a quarter of the row's at most, and climbs
  back slowly once they keep up. Rows sharing an encode follow the worst
  connected one. The row reads "Live · 3.2 of 6 Mb/s"; Stats shows it,
  seconds waiting and stalls. Every step is logged.
- A live stream whose bytes stop going out for 10 s is torn down and
  reconnected, instead of waiting for the server to drop it.
- While live, bitrate and automatic bitrate change at once on a row's own
  encode; picture size, frame rate and sound apply on the next Go live, as
  the editor says.

* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.21
- A widget added to a running canvas (a scene, the other canvas turned on)
  never stops its source: its layer is fed only once it runs, and a redrawn
  picture reaches it only after its stream's caps.
- Scene changes keep time with the compositor when it runs late: a switch
  starts once every picture of the new scene is in the compositor, and fades
  and glides follow the frames it actually makes.

* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.20
- Streams carry sound again: vah264enc puts its output 1000 hours above
  its input, and every stream row dropped all of the encode's sound as
  before its first picture. Encoded samples now go back on their input's
  timeline.
- A row starts with the sound its encode put out since its first picture
  went in: behind a slow encoder, recordings and streams began seconds
  late on sound.
- Dropped sound is logged (rate-limited), with each encode's and row's
  first sound offset; STREAMY_ROW_DUMP=<dir> writes what a stream row
  sends.
- A screen lock no longer stops the program while a destination is live
  (the server closed the connection and the reconnect waited for good).
- An encode's timestamps carry on across a program restart.
- Encodes keep 30 fps whatever rate a canvas hands over (60 fps doubled
  every bitrate).
- The Recording row records whenever Go live starts; End stream ends that
  recording. No Add widget in the action bar.

* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.19
- Every scene's sources stay open: a scene switch reopens nothing and shows
  no "Connecting" card. A source no shown scene uses is parked (a camera's
  pictures are dropped before decoding, a screen passes one a second).
- Scene transitions: Fade (300 ms by default) or Cut, set with nothing
  selected in the Inspector. Keyframed on the program clock, so streams and
  recordings fade too; a picture in both scenes glides to its new place.
- Linked widgets: Paste (Ctrl+V) links a widget into another scene, sharing
  its source, name and sound while its place and Look stay its own. Paste as
  new widget, Unlink from other scenes; a link icon marks shared widgets.
- Duplicate scene links its widgets; older projects link identical sources.
- The canvas ⋯ menu opens again.

* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.18
- Go live appears and goes the moment a stream destination is turned on or
  off, added or removed (the bar followed only the next other event).
- X as a destination: its ingest server and a stream key from Media Studio
  Producer, with X's own logo.

* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.17
- A camera reopens on its new node while the session is locked (it asks
  nothing, and a lock doesn't pause it); after a camera service restart a
  camera that fails or sends nothing is opened again, up to six times in
  30 s; cameras open at start while locked

* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.15
- The camera list is read again when wireplumber@video-capture, the
  WirePlumber instance that hosts the camera monitors, restarts

* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.14
- Cameras and microphones follow PipeWire and WirePlumber restarts: the camera
  list is read again on a fresh portal connection when its connection closes,
  never lists, the service restarts, a camera stream ends or a chosen camera
  is missing; a device is unplugged only once a fresh list agrees; a camera
  on a new node reopens by itself, a microphone that came back is listened to
  again
- MJPEG cameras decode on the GPU with vajpegdec (rank none, so decodebin
  never chose it); a GPU-decoded camera that sends but shows nothing in 8 s
  decodes in software

* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.13
- A preview leaving the program (a canvas hidden, the program closing) lets
  GTK's paintable sink go on the main thread; dropped on the close thread it
  aborted the app as it quit

* Sun Nov 22 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.12
- Quitting waits for the program and every pending stop to reach NULL, so
  no GL thread is mid-teardown when the process exits (exit segfaults)
- Every program pipeline in a process shares one GL display; an old
  program's eglTerminate no longer breaks the next one

* Sat Nov 21 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.11
- aarch64: no gstreamer1-plugins-bad-free-extras recommendation; it conflicts
  with the Raspberry Pi 5 bad-free override

* Sat Nov 21 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.10
- Hide Streamy from screen capture (app menu, on by default): Streamy's
  windows stay out of display widgets and screen shares through
  AppWindow.set_hidden_from_capture(), so a display widget of Streamy's own
  display shows no copies of Streamy. No effect on platforms without it.

* Fri Nov 20 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.9
- Outputs: one encode per canvas and quality, teed to RTMP(S), SRT, WHIP and
  recording rows, each reconnecting on its own; Destinations, Stats and Chat
  panels (Twitch and YouTube sign-in, read-only Twitch and Kick chat)
- Camera Resolution and Frame rate choices, sizes the camera lacks scaled on
  the GPU; new cameras start at 1080p30; MJPEG decodes in software
- Nothing asks for a share while the session is locked; sources that need a
  picker wait for one Share in the bar
- The preview rests while only stills show; compositing at 60 fps, outputs at
  30 fps; level words at 4 Hz

* Fri Nov 20 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.8
- lumaframe: a revision property commits property changes together and
  stamps output buffers; downscaled content is box-filtered over each output
  pixel's footprint
- Mixer pads take the geometry of the buffer being composited, so Look
  changes never move the picture; window casts prefer alpha formats
- Inspector beside the island (Source, Look, Arrange; the scene with nothing
  selected); menus at the pointer with Order/Size/Align/Look submenus; inline
  rename in rows; per-field Look overrides with a sync key; panel moves undo
- Landscape and Vertical are separate preview panels with canvas chips; each
  source is captured once and teed to one compositor per canvas, which runs
  only while its panel shows or an output takes it; edits go to the canvas
  clicked
- Window casts negotiate alpha formats only, opaque when none is offered
- Every camera on the portal is listed (V4L2 and libcamera, one row each);
  sound devices get short names; monitoring is one "Hear it on" choice
- Stills and placeholder cards are uploaded once; per-frame layer reports
  run only while something changes

* Fri Nov 20 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.7
- lumaframe: content-x/-y/-width/-height pick the source region before
  crop-*; with content-width 0 a buffer's GstVideoCropMeta does, so a window
  cast inside a larger buffer fills the box

* Fri Nov 20 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.6
- Source first in every widget's bar: cameras by name with resolution, frame
  rate and mirror; microphones and outputs; Change… for shares; the pointer
- Failures name their cause; a window cast that sends nothing yet waits
- The program runs on past a source that fails, so meters and recordings do
- Look: presets, one-column sliders, border colour picker and position
- Arrange: position, crop, order, opacity; canvas crop and size commands
- Widgets inset beside Audio; Scenes-only sidebar with its + heading
- Undo and redo for every edit; per-canvas placements linked to the primary
- Placeholder cards for widgets without a picture; monitoring for sound

* Fri Nov 20 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.4
- lumaframe: stroke-position (inside, center, outside); an outside or centred
  stroke grows past the box with concentric corners and the shadow starts
  beyond it

* Fri Nov 20 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.3
- Audio strips use LumaUI's VolumeSlider: the stereo meter lies along the
  fader's track, 0 dB is marked, muted goes quiet

* Fri Nov 20 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.2
- App audio widgets through luma_appkit.audio_capture (the Share sound
  picker, restore tokens); a capture lives while its scene is shown.
- A refused or failed restore token falls back to the picker once; a
  cancelled, timed-out or failed share leaves the widget saying so with
  its fix in the bar and the Source editor.
- Registers its app id with xdg-desktop-portal, so the picker and the
  sharing pill name Streamy and keep its restore tokens.
- The Audio inset is as tall as its strips; on a phone it stays under the
  preview. Widget rows end in their eye or speaker; hidden and muted show.
- An open editor keeps the selection's name in the bar.
- SVT-AV1 (svtav1enc) as the software AV1 encoder after VA AV1:
  preset 10, bitrate-targeted, a key frame every 2 s.
* Fri Nov 20 2026 Project Luma <maintainers@projectluma.org> - 0.1.0-1.luma.1
- First package: scenes of displays, windows, cameras, images and sound;
  rounded corners, a stroke and a shadow per picture through lumaframe;
  streaming to RTMP(S) and recording to MKV or MP4.
