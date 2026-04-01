# Changelog

All notable changes to halo-gtk are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-03-30

### Added
- Project scaffold — GTK4 + libadwaita application shell, system tray (AyatanaAppIndicator3), `.desktop` file, GNOME light/dark theme support, uv package management, ruff linting, pytest, GitHub Actions CI
- App icon — full hicolor set at 256×256, 128×128, 64×64, 48×48, and 32×32
- Ring API authentication — email/password login, 2FA/OTP dialog, token caching to `~/.local/share/halo-gtk/token.cache`, session restore on startup, device list populated after sign-in, FCM real-time event listener via firebase-messaging
- Camera snapshot thumbnails — 80×45 thumbnails loaded on startup via `async_get_snapshot()`; auto-refresh on FCM ding/motion event (no polling); click row to expand full size in an `Adw.Dialog`
