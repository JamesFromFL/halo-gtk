# Changelog

All notable changes to halo-gtk are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

This section tracks the work toward Halo GTK's first tagged release.

### Added
- Ring sign-in, two-factor authentication, Secret Service token storage, and background
  session restore.
- Camera dashboard, multi-camera live monitoring layouts, focused live view, event history,
  local favorites, downloads, screenshots, and desktop notifications.
- Shared WebRTC session ownership with bounded stream counts, H.264 compatibility handling,
  audio playback, two-way talk, light control, and siren control where supported.
- User-local desktop integration, background autostart, status notifier support, privacy
  controls, diagnostics, and XDG-based storage.
- Automated lint, test, dependency-audit, schema, desktop-file, and installed-wheel checks.

### Changed
- Ring authentication tokens moved from a legacy plaintext cache to Secret Service.
- Configuration, layouts, nicknames, and favorites use validated, atomic JSON persistence.
- Live media and Ring client resources now have explicit generation and ownership lifecycles.
- Runtime dependencies and the uv/build toolchain are pinned where Halo relies on private
  compatibility contracts.

### Fixed
- Stale authentication, event-listener, snapshot, and live-stream callbacks can no longer
  overwrite a newer session.
- Blocking recording-frame decoding no longer stalls Ring's asynchronous event loop.
- Hidden camera pages no longer retain duplicate event subscriptions and refresh timers.
- Desktop talkback activates the Ring camera speaker before microphone capture starts.
- Archived favorites no longer duplicate the matching Ring event in filtered history.

### Removed
- The obsolete in-repository headless bridge, RTSP spike utilities, embedded live-view
  fallback, and other unreachable compatibility code. The headless backend now lives in
  Halo Server.
