# Changelog

All notable changes to ring-gtk are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-03-29

### Added
- Initial project scaffold: GTK4 + libadwaita application shell
- `RingClient` — authentication, token caching, device enumeration
- Real-time event listener via `ring-doorbell[listen]` websocket
- `AuthDialog` — email/password + OTP two-factor flow
- `SystemTray` — AyatanaAppIndicator3 / AppIndicator3 systray icon
- Desktop notifications via libnotify (`gi.repository.Notify`)
- `.desktop` file and GSettings schema for `io.github.JamesFromFL.RingGtk`
- JSON config fallback (`~/.config/ring-gtk/config.json`)
- uv package management; ruff linting; pytest test suite
- GitHub Actions CI (lint + test)
