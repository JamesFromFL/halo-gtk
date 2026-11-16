# Maintenance and desktop baseline — 16 November 2026

This is the first item in the [saved improvement backlog](improvement-backlog.md),
following the GUI redesign commit `c9475b2`.

## Findings and changes

The last pushed GTK CI run failed only its dependency audit. Lint, application
tests and installed-wheel checks passed on that revision. The local redesigned
commit has not been pushed; this document records local qualification, not a new
GitHub Actions result.

- Updated the lock from dnspython 2.8.0 to 2.9.2 and added the compatible
  `>=2.9.2,<3` floor. aioice uses this parser for incoming mDNS packets during
  camera connection setup. Only dnspython changed version in the lock.
- Preserved the validated Ring 0.9.14, aiortc 1.15.0 and PyAV 17.1.0 pins.
- Replaced the audit command's hidden-advisory switch with explicit report
  validation. Every active exported dependency must have a matching audited
  result. Empty, skipped, malformed, duplicate or incomplete results fail.
- Retained raw findings. The exact OAuthlib 3.3.1 authorization-server exceptions
  and their removal triggers are documented in [SECURITY.md](../SECURITY.md).
  All other findings, new aliases and different package/version combinations fail.
- Added mDNS and audit-policy regressions, plus a native history playback test
  using a generated local video and silent audio sink.

## Qualification environment

Arch Linux desktop, Python 3.14.10, GTK 4.24.1, libadwaita 1.10.0, PyGObject
3.58.1, and GStreamer 1.28.9. The candidate virtual environment was created
separately from the existing checkout environment and installed from the frozen
lock. The built wheel was also installed in a second isolated environment for
dependency and package-data checks. The user's installed app was not replaced.

## What is verified

- Full offline suite: **1,672 passed**, with five native checks skipped there
  and all **five passed separately** on the isolated GTK display. The remaining
  warnings come from upstream deprecated aiohttp and PyGObject APIs.
- Exact frozen CI audit command on Python 3.11.15: **38 packages covered**,
  with only the two documented OAuthlib authorization-server advisory families.
  Both DNS parser regressions fail on 2.8.0 and pass on 2.9.2.
- Locked dependency resolution, applicability review and audit-policy checks.
- The complete offline regression suite on the candidate dependencies.
- Ruff checks using the project's frozen tool version, module imports, strict
  schema validation, desktop-file validation, source/wheel builds, wheel import
  and metadata validation, and installed dependency compatibility.
- Actual installed aiortc/PyAV compatibility contracts and local H.264/audio
  loopback, including three simulated talkback renewals and peer cleanup.
- Actual native GTK history playback of generated H.264/AAC: decoded frames,
  audio buffers sent only to `fakesink`, pause/resume, screenshot capture,
  end-of-stream, restart, stop and bus-handler cleanup.
- Native wide, intermediate and compact layouts on an isolated Broadway display.

## Live acceptance still required

Offline qualification does not prove live Ring login/MFA, event-service reconnect,
camera ICE/TURN negotiation, actual microphone or camera-speaker quality, desktop
keyring interaction, GPU/compositor behavior, or long-running hardware sessions.
Those checks belong to an explicitly coordinated session with the user's account.
Alarm mode changes, sirens, audible device tests and entrance release were not
performed.

The next larger feature is consistent camera privacy enforcement across live
view, previews, caches and background work. This maintenance pass does not add
privacy controls or change current camera behavior beyond the DNS parser update.

## Reproduce the native checks

Use a separate temporary XDG runtime/config directory and a loopback-only
`gtk4-broadwayd` listener. With that display selected, enable
`HALO_NATIVE_LAYOUT_TESTS=1` and `HALO_NATIVE_MEDIA_TESTS=1`, then run:

```sh
python -m pytest -q -p no:cacheprovider \
  tests/test_camera_native_layout.py tests/test_native_media_baseline.py
```

The native checks intentionally skip in a normal headless test run. They use
synthetic state/media and do not construct a Ring client or open audio hardware.
