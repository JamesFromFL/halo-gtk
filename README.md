# Halo GTK

<p align="center">
  <img src="data/icons/hicolor/256x256/apps/io.github.JamesFromFL.HaloGtk.png" width="160" alt="Halo GTK icon">
</p>

Halo GTK is a native GTK 4 + libadwaita desktop client for Ring cameras and
home security on Linux. It is built to feel like a real GNOME application:
fast navigation, useful camera controls, desktop notifications, local media
tools, and a layout that stays out of the way while you monitor your home.

The interface uses an adaptive left navigation ribbon on wide windows and an
overlay drawer on compact windows. Camera imagery remains the primary content,
with explicit snapshot, stream, connection, and device states shown in text as
well as standard system icons.

> Halo GTK is an unofficial project and is not affiliated with, endorsed by,
> or supported by Ring or Amazon.

## Current Features

### Ring Account

- Ring sign-in with email, password, and two-factor authentication.
- OAuth token storage through the desktop Secret Service.
- Background session restore so the UI can open before the keyring finishes.
- Device loading for Ring cameras, doorbells, chimes, and other discovered Ring devices.
- Firebase Cloud Messaging listener for live Ring events.

### Ring Alarm Backend (Experimental)

- Uses the existing Halo Ring login, hardware identity, OAuth session, and rotating token owner;
  Alarm does not create a second account session or require a Node helper.
- Runs an independent, native per-location Alarm WebSocket transport without changing the
  established camera, history, notification, or media paths.
- Publishes immutable, revisioned location, hub, security-panel, and sensor snapshots with
  explicit connection, inventory, stale, unknown, transition, and alarm states.
- Normalizes contact, motion, glass-break, tilt, flood/freeze, smoke/CO, retrofit, power,
  tamper, battery, lock, switch, valve, and unknown device data when Ring reports it.
- Provides a conservative backend request API for Disarmed, Home, and Away modes. Requests
  require current owner authorization, a fresh online panel, an expected state revision, and
  explicit confirmation of any bypass set; success is reported only after Ring publishes the
  requested panel state.
- Keeps Alarm tickets and raw protocol frames in memory only. It does not persist Alarm state
  or send Alarm notifications yet.

The reserved Alarm page is intentionally not connected to this backend yet. The implementation
uses Ring's undocumented CLAP interfaces and has only been exercised with bounded synthetic
fixtures. Live read-only inventory comparison must be completed before live mode-control testing.
Halo is not an emergency-monitoring interface, and the backend intentionally exposes no panic,
dispatch, Alarm siren, lock, switch, schedule, or device-configuration commands. Protocol behavior
was independently implemented against the pinned
[`ring-client-api` reference](https://github.com/koush/ring/tree/516e96a24ec279168c246795e623b6bfdf58ec45/packages/ring-client-api).

### Dashboard

- Account summary and loaded Ring device status.
- Snapshot camera grid for a quick overview.
- Camera cards with local Halo nicknames, latest snapshots, device power, network strength,
  and camera settings access.
- Shared Balanced (`4 / 2 / 1`) and Dense (`5 / 3 / 2`) Small/Medium/Large grid presets.
- Snapshot refresh on first load, Ring motion/ding events, and a fallback refresh timer.
- Motion Detection Off handling with a consistent blurred snapshot overlay.

### Live Monitoring

- Dedicated Live Monitoring page for multi-camera viewing.
- Adaptive selected-camera inspector for health, talk, audio, light, siren, settings,
  and history controls.
- Saved custom layouts with camera visibility, camera order, and grid size.
- Default layout uses the first four cameras in alphabetical order at medium size.
- Layout save, rename, delete, and reset-to-default behavior.
- Camera selector with live selection counts.
- Built-in stream limits: four live cameras by default, optional six-camera mode for users
  who explicitly enable it.
- Gatekeeper logic to stop all streams if a bug ever tries to exceed the configured maximum.
- Play and stop controls for starting or ending live monitoring.
- Optional settings for auto-start, keeping streams alive across pages, keeping streams alive
  while focusing a camera, and starting streams unmuted.
- Per-camera live controls for volume, microphone, lights, and siren where Ring exposes the
  feature for that device.

### Focused Live View

- Opens from the Dashboard or Live Monitoring and uses the same focused camera experience.
- Reuses an existing Live Monitoring stream when a camera is already active.
- Shows the latest cached frame or snapshot immediately while the live stream connects.
- Keeps the final frame visible when the stream is stopped instead of cutting to a blank view.
- Image-first 16:9 monitor with direct play, stop, microphone, screenshot, light, siren,
  mute, volume, and zoom controls.
- Camera power, network, settings, and event-history access remain visible below the monitor.
- Mouse-wheel zoom and drag-to-pan video inspection from 100% to 250%.
- Screenshots capture the visible zoomed viewport.
- Audio playback and two-way talk support through GStreamer and aiortc.

### Event History

- Event History view with camera selection, event filters, and backend history paging.
- Time display that reads naturally: Today, Yesterday, or weekday/date plus the event time.
- Event classification for doorbell rings, on-demand views, motion, person, vehicle, package,
  linked events, and other Ring event kinds exposed by the API.
- Ring recording playback with GStreamer.
- Adaptive list/detail review with an image-only 16:9 playback surface and event metadata below it.
- Progress bar, elapsed/total timer, previous/play/next controls, mute, volume, fullscreen,
  zoom, screenshot, favorite, share, download, and delete actions.
- Video-only fullscreen behavior.
- Persistent zoom between events so an area of interest can stay framed while reviewing clips.
- Screenshots capture the zoomed playback viewport.
- Next Auto Play setting for recorded event review.
- Local Favorites archive for preserving selected Ring recordings and thumbnails.

### Device Details And Settings

- Camera settings window with configurable Ring options when the device exposes them.
- Device Info view for camera health, power, network, and raw device details.
- Local Halo device nicknames stored outside Ring so device names can be customized per app.
- Standard symbolic power states for hardwired/plugged-in devices, charging batteries,
  battery ranges, and unknown/error states.
- Standard symbolic network states for disconnected, wireless signal ranges, and ethernet
  connections.

### Notifications And Desktop Integration

- Desktop notifications for Ring events.
- Optional notification preview images and notification summaries.
- Custom notification message support.
- Optional tray/status indicator support.
- User-local launcher, desktop file, hicolor icons, and GSettings schema installation.
- GNOME light/dark style integration through libadwaita.
- Six organized settings areas: General, Live View, Events, Storage, Account, and Advanced.

### Local Media And Privacy

- Configurable screenshot and video download folders.
- Optional per-camera subfolders.
- Button to open the local Favorites folder.
- Clear preview image cache and privacy cleanup actions.
- Diagnostics export for troubleshooting.
- XDG-based local storage so removing Halo's config directory returns the app to defaults.

## Requirements

### System Packages

PyGObject and the GNOME introspection libraries must come from your distribution packages;
they cannot be installed cleanly through pip.

On Arch Linux:

```bash
sudo pacman -S python-gobject gtk4 libadwaita libnotify libsecret \
  gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly \
  gst-libav gst-plugin-gtk4
```

For PipeWire audio output, also make sure the PipeWire GStreamer plugin is available:

```bash
sudo pacman -S gst-plugin-pipewire
```

Package names vary by distribution, but Halo needs GTK 4, **libadwaita 1.5 or newer**,
libnotify, libsecret, GStreamer 1.0, common codec plugins, and the GTK 4 GStreamer
paintable sink. Distributions shipping an older libadwaita, including Debian 12's base
repositories, need a newer supported package source or distribution release.

### Python Dependencies

Halo supports Python 3.11 through 3.14. Python dependencies are locked with
`uv` 0.11.24:

- `ring-doorbell` for Ring API access
- `aiohttp` for the isolated Ring Alarm WebSocket transport
- `aiortc` for WebRTC live streams
- `av` and `numpy` for video frame handling
- `Pillow` for snapshots, overlays, and screenshots
- `requests` for bounded Ring recording downloads

## Installation

```bash
git clone https://github.com/JamesFromFL/halo-gtk
cd halo-gtk
./scripts/install.sh
halo-gtk
```

The installer creates a `.venv` with system site packages enabled, installs only the locked
production dependencies, installs the launcher to `~/.local/bin/halo-gtk`, and installs desktop
assets under `~/.local/share`. The launcher points into this checkout, so keep the checkout at the
same path while Halo is installed.

The wheel built in CI validates Halo's Python package and bundled application icon. It does not install
the desktop file, GSettings schema, or hicolor icons; `scripts/install.sh` remains the supported
desktop installation path.

The headless Ring-to-Scrypted backend is maintained separately as Halo Server. It is not
installed or run by the desktop application.

To update an installed checkout:

```bash
./scripts/update.sh
```

To remove the user-local launcher and desktop integration:

```bash
./scripts/uninstall.sh
```

## Development

```bash
uv venv --system-site-packages
uv lock --check
uv sync --frozen --no-group audit
.venv/bin/halo-gtk
.venv/bin/ruff check --no-cache src tests scripts
.venv/bin/ruff format --check --no-cache src tests scripts
.venv/bin/pytest -q -p no:cacheprovider
uv build --out-dir dist
```

`uv venv --system-site-packages` is required so the virtual environment can see `gi`
and the GObject introspection libraries installed by your package manager. The project metadata
rejects uv versions other than 0.11.24 so lock and artifact behavior cannot silently drift.

## Local Storage

Halo follows XDG paths for app-owned files:

- Settings: `~/.config/halo-gtk/settings.json`
- Live Monitoring layouts: `~/.config/halo-gtk/live-monitoring-layouts.json`
- Device nicknames: `~/.config/halo-gtk/device-names.json`
- Local Favorites archive: `~/.local/share/halo-gtk/favorites/`
- Cache: `~/.cache/halo-gtk/`
- Logs: `~/.local/state/halo-gtk/halo-gtk.log`

User media defaults to:

- Screenshots: `~/Pictures/halo-gtk`
- Downloads: `~/Videos/halo-gtk`

These folders can be changed in Halo Settings.

## Planned Features

- Connect the experimental Ring Alarm backend to the reserved Alarm page after controlled
  read-only and mode-control validation with owned hardware.
- Expanded support for non-camera Ring devices such as chimes and sensors.
- More camera settings as Ring exposes them through `ring-doorbell`.
- Direct PipeWire capture/export mode for using live camera output in tools such as OBS
  or Discord.
- A dedicated saved media browser for local Favorites, screenshots, and downloaded clips.
- More advanced notification rules and per-device notification behavior.
- Packaging for broader distribution, including Arch/AUR and Flatpak.

## License

GPL-3.0-or-later - see [LICENSE](LICENSE).
