# Changelog

All notable changes to halo-gtk are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

## [Unreleased]

This section tracks the work toward Halo GTK's first tagged release.

### Added
- Experimental Alarm cloud history with a separate non-video view, bounded
  session cache, cancellation, source labels and explicit coverage limitations.
- Semantic live Alarm notifications, opt-in routine activity, default-redacted
  detail, account-aware navigation and revocation cleanup.
- Real Alarm inventory/discovery refresh, sensor supervision, read-only siren and
  ticket power status, and service-owned pending/uncertain mode outcomes.
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
- Alarm REST uses the maintained ticket service and bounded authenticated JSON
  reads with coordinated token refresh. Normal mode approvals expire, require
  current inventory, and remain fenced at the socket send boundary.
- Unknown transition timestamp units no longer produce a guessed countdown.
- Updated the validated media stack to aiortc 1.15.0 / PyAV 17.1.0 with explicit
  single-thread RGB conversion and permanent real-media compatibility contracts.
- Recording previews now use verified, bounded HTTPS and a stoppable decode
  process; authenticated snapshot bodies are streamed under a separate cap.
- Device setting writes are ordered per camera and session; pending values,
  uncertain results and failed local saves have visible outcomes.
- Tray registration is asynchronous and Favorites reconciliation runs outside
  GTK callbacks with cached results. Deliberate media saves have tracked,
  bounded jobs with progress, cancellation and atomic publication.
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
- Live video backlog is bounded, source frame intervals and audio gaps are
  retained, and stale microphone results cannot change a newer talkback request.
- Microphone startup and late capture failures reset the control and show a
  failure outcome instead of leaving “Microphone on” while sending silence.
- Shared Alarm policy helpers preserve existing admission decisions and reason
  precedence, including service-only transport and bypass guards.
- Alarm mode requests revalidate the confirmed revision at the socket-write boundary;
  late connections and malformed protocol values cannot revive stopped state.
- Event selection, deletion, autoplay, account details, and siren confirmations retain
  the correct camera, request, and authenticated-session identity.
- Temporary device disappearance no longer deletes saved monitoring layouts, and
  Ring inventory refresh now discovers added/removed devices without restarting login.
- Failed listener, session, and microphone startup/teardown clean up their resources;
  push-to-talk discards captured samples that complete after release.
- Live playback renegotiates changed media formats and correctly converts unsigned PCM.
- Favorite downloads no longer hold the history index lock, and identically named
  cameras cannot overwrite each other's archived clips.
- Malformed settings/health data remain recoverable, unknown health stays unknown,
  and log cleanup preserves symlink targets.
- Installer and autostart paths use literal command escaping; launcher/desktop
  publication is atomic, process matching is exact, and XDG paths stay consistent.
- Refreshed HTTP, certificate, image, and related locked dependencies following
  the October 2026 advisory audit; documented the server-only OAuthlib exception.
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
