# Halo GTK improvement backlog

Saved recommendations from 16 November 2026. The native GNOME interface redesign
is committed as `c9475b2`; the items below preserve the separate feature work.
The [completion report](backlog-completion-2026-11-16.md) records the delivered
slices, verification and remaining acceptance gates.

## Progress

- Maintenance and isolated desktop qualification: complete in `3d310cb`;
  live-device acceptance remains separate.
- Device-write replay prevention prerequisite: complete in `8972a06`.
- Measured H.264 receive recovery repair: complete; see the
  [receive-path comparison](receive-path-review-2026-11-16.md).
- Local notification rules, snooze and quiet hours: complete; see
  [notification rules](notification-rules.md). Critical Alarm preferences retain
  their existing master controls.
- Camera privacy and live-preview coordination: complete; see
  [camera privacy](camera-privacy.md). Ring receipt delays and live-device
  acceptance remain explicit limitations.
- Qualified device controls: complete for the scope below; further model and
  protocol support remains subject to the recorded evidence gates.
- Individual Beams discovery and qualified finite/automatic controls: complete;
  see [Beams support](beams-support.md). Physical-device acceptance is separate.
- Camera light outcome reporting and read-only motion-timer information:
  complete; see [camera lighting](camera-lighting.md). Indefinite manual On is
  still an evidence gate, not an inferred capability.
- Chime snooze/resume and qualified automatic nightlight: complete; see
  [Chime support](chime-support.md). Physical-device acceptance remains separate.
- Alarm history pagination guard and coverage reporting: complete; see
  [history coverage](alarm-history-coverage.md).
- Optional persistent local Alarm history: complete, off by default with
  account-bound 7/30-day copies and explicit clear; see
  [local history](local-history-design.md). Saved copies retain coverage limits
  and require current account/location permission verification.
- Qualified keypad brightness, local control lock and observed proximity:
  complete; see [keypad support](keypad-support.md).
- Optional staged installer, rollback and interruption recovery: complete; see
  [managed installation](staged-install-design.md). System runtime dependencies
  remain shared; this does not establish broader distribution compatibility.
- Experimental x86_64 GNOME 50 Flatpak: built and tested in an isolated runtime;
  see [packaging qualification](flatpak-packaging.md). Desktop portal, real media
  and distribution acceptance remain separate; host login startup is unavailable
  in the sandbox candidate.
- Experimental Intercom Video media: complete for the evidence-qualified Video
  model, with explicit Start/End, listening and hold-to-talk; see
  [Intercom support](intercom-support.md). Audio-only calling and physical
  Intercom acceptance remain evidence gates. Camera/doorbell talkback is a
  separate existing feature.

## 1. Maintenance and desktop baseline

Completed: resolve the locked dependency-audit failure, document advisory
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

## Remaining qualification and deferred work

- Owner-operated acceptance of the qualified controls, camera privacy and media.
- Intercom Video hardware calling and audio acceptance; audio-only calling needs
  its own verified protocol.
- Permanent manual camera-light hold and native Ring group configuration,
  membership and schedules require the evidence described in
  [research gates](backlog-research-gates-2026-11-16.md).
- Flatpak desktop portals, credential prompts, audio hardware and distribution
  acceptance; sandbox host-startup integration remains future work.
- Optional Halo Server connection mode, as a separately reviewed architecture
  change. The owner chose to keep GTK direct-to-Ring on 16 November 2026; this
  item is deferred by that decision. See [research gates](backlog-research-gates-2026-11-16.md).

## Preserve throughout

Camera previews; monitoring layouts; focused video/audio and talkback; event
history; downloads, screenshots and Favorites; Alarm locations and guarded normal
mode controls; Chime volume/sound requests; timed Light Groups; confirmed Intercom
release; Secret Service credentials; bounded media jobs and account cleanup.

Alarm state changes, audible device tests, sirens and entrance release are not
automatic verification actions. The user will test normal Alarm states. A
general-purpose local video browser is not a priority.
