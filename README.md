# ring-gtk

Native GTK4 + libadwaita Linux desktop client for [Ring](https://ring.com) home security.

> **Status:** Early development — authentication and device listing work; camera feeds and arm/disarm controls are planned.

## Features

- Sign in with your Ring account (email + password + 2FA)
- View all Ring devices (doorbells, cameras, chimes, base stations)
- Real-time doorbell and motion notifications via desktop libnotify
- System tray icon (AyatanaAppIndicator3)
- Persistent token cache — only sign in once
- Follows GNOME HIG; adapts to light/dark system theme

## Planned

- [ ] Live camera feeds (GStreamer / WebRTC)
- [ ] Arm / disarm Ring Alarm
- [ ] Event history timeline
- [ ] Flatpak packaging

## Requirements

### System packages

| Package | Notes |
|---|---|
| `python-gobject` / `python3-gi` | PyGObject — GTK Python bindings |
| `gir1.2-gtk-4.0` | GTK 4 introspection data |
| `gir1.2-adw-1` | libadwaita introspection data |
| `gir1.2-notify-0.7` | libnotify (desktop notifications) |
| `gir1.2-ayatanaappindicator3-0.1` | Systray icon (optional) |

**Arch:** `python-gobject gtk4 libadwaita libnotify libayatana-appindicator`
**Debian/Ubuntu:** `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 gir1.2-notify-0.7 gir1.2-ayatanaappindicator3-0.1`
**Fedora:** `python3-gobject gtk4 libadwaita libnotify libayatana-appindicator`

### Python dependencies (managed by uv)

- [`ring-doorbell[listen]`](https://github.com/tchellomello/python-ring-doorbell) ≥ 0.8

## Installation

```bash
# Clone
git clone https://github.com/JamesFromFL/ring-gtk
cd ring-gtk

# Install Python deps via uv
uv sync

# Run
uv run ring-gtk
```

## Development

```bash
uv sync --dev
uv run ruff check src tests
uv run pytest
```

## License

GPL-3.0-or-later — see [LICENSE](LICENSE).
