# Halo GTK improvement backlog

Saved recommendations from 16 November 2026. The native GNOME interface redesign
is committed as `c9475b2`; the items below preserve the separate feature work.

## 1. Maintenance and desktop baseline

Current work: resolve the locked dependency-audit failure, document advisory
applicability, retain raw audit evidence, and qualify a fresh isolated runtime and
installable wheel. Keep the validated Ring, aiortc and PyAV compatibility pins
unless a separate review justifies a change.

Use offline tests for sign-in/session lifecycle, reconnect, snapshots, event
playback, WebRTC audio/video, simulated talkback, and shutdown. Live account
sign-in, real camera playback, microphone quality and longer monitoring remain
user-assisted acceptance checks. Synthetic tests do not establish hardware
compatibility.

## 2. Camera privacy and reliability

- Centralize effective Ring video/audio privacy across live view, previews,
  cached media and background work. Explain stale, unknown and denied states.
- Evaluate Halo Server's applicable receive-path improvements: missing H.264
  fragments, keyframe recovery, source timing, snapshot coordination and audio
  correctness. Measure GTK behavior before porting changes; HomeKit output code
  is not part of the desktop's requirements.
- Preserve raw ambient audio. Any optional filtering must be deliberate and
  visible.

## 3. Device features

- Qualified keypad brightness, persistent local brightness lock and
  evidence-qualified proximity/motion.
- Individual supported Smart Lighting/Beams controls, finite overrides and
  return to automatic behavior.
- Chime snooze/resume and available automatic nightlight, with model qualification.
- Camera-light actions with pending, accepted, observed and uncertain outcomes,
  plus available motion-timer information.
- Research and validate a durable manual floodlight hold. Neither project
  currently guarantees indefinite On; it is not a completed server feature to port.

## Later work

- Per-device notification rules, snooze and quiet hours.
- Safer staged installation/update with known-good rollback, then broader Linux
  packaging.
- Intercom answering with audio/video; observation and confirmed release already
  exist.
- Group configuration, membership and schedules only where Ring interfaces and
  permissions are verified.
- Alarm history pagination/coverage and optional persistent local history.
- Optional Halo Server connection mode, as a separately reviewed architecture
  change. GTK currently connects directly to Ring.

## Preserve throughout

Camera previews; monitoring layouts; focused video/audio and talkback; event
history; downloads, screenshots and Favorites; Alarm locations and guarded normal
mode controls; Chime volume/sound requests; timed Light Groups; confirmed Intercom
release; Secret Service credentials; bounded media jobs and account cleanup.

Alarm state changes, audible device tests, sirens and entrance release are not
automatic verification actions. The user will test normal Alarm states. A
general-purpose local video browser is not a priority.
