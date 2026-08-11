# Changelog

All notable changes to halo-gtk are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

This section tracks the work toward Halo GTK's first tagged release.

### Added
- Ring sign-in, two-factor authentication, Secret Service token storage, and background
  session restore.
- Experimental native Ring Alarm page and backend sharing the existing authenticated Ring
  session, with location selection, service and mode status, grouped sensor visibility,
  reconnect supervision, and revision-checked Disarmed/Home/Away requests. Mode changes and
  faulted-sensor bypasses require explicit confirmation, and the UI waits for Ring to confirm
  the result. The integration uses undocumented CLAP interfaces and bounded synthetic fixtures;
  live-hardware validation remains deferred. Halo is not an emergency-monitoring service, and
  panic, dispatch, Alarm siren, lock, switch, and device-configuration controls remain excluded;
  the official Ring app or keypad is still required for critical security workflows.
- Camera dashboard, multi-camera live monitoring layouts, focused live view, event history,
  local favorites, downloads, screenshots, and desktop notifications.
- Shared WebRTC session ownership with bounded stream counts, H.264 compatibility handling,
  audio playback, two-way talk, light control, and siren control where supported.
- User-local desktop integration, background autostart, status notifier support, privacy
  controls, diagnostics, and XDG-based storage.
- Automated lint, test, dependency-audit, schema, desktop-file, and installed-wheel checks.

### Changed
- Rebuilt the application interface around an adaptive libadwaita navigation ribbon, image-first
  camera surfaces, a selected-camera monitoring inspector, and compact list/detail history.
- Reorganized settings into General, Live View, Events, Storage, Account, and Advanced areas.
- Camera grid density can use Balanced 4/2/1 or Dense 5/3/2 Small/Medium/Large presets.
- In-app activity, power, network, light, siren, and media controls now use standard symbolic
  theme icons; Halo's unique application mark remains the only custom UI icon.
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
- The superseded custom event, power, network, light, and siren bitmap icon matrix.
