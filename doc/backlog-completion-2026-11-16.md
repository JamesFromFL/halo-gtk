# GTK backlog implementation and qualification

This pass follows the native interface redesign (`c9475b2`) and the saved
[improvement backlog](improvement-backlog.md). GTK remains a direct Ring client,
as requested. The optional Halo Server backend was not started. Each completed
implementation slice was tested and committed locally; no push, deployment or
replacement of the installed desktop application was performed.

## Delivered slices

| Commit | Change | Details |
| --- | --- | --- |
| `3d310cb` | Dependency audit repair and isolated desktop baseline | [Baseline](desktop-baseline-2026-11-16.md) |
| `8972a06` | Writes through RingJsonTransport cannot be automatically replayed | Single-attempt transport with uncertain outcomes |
| `5c9cf1c` | Quiet hours, notification snooze and per-device rules | [Notification rules](notification-rules.md) |
| `c492573` | Bounded H.264 loss recovery and keyframe requests | [Receive-path review](receive-path-review-2026-11-16.md) |
| `3ba0328` | Qualified Chime snooze/resume and automatic nightlight | [Chime support](chime-support.md) |
| `570eba4` | Alarm history pagination bounds and honest coverage | [History coverage](alarm-history-coverage.md) |
| `15fe841` | Keypad brightness, local lock and observed proximity | [Keypad support](keypad-support.md) |
| `4e06927` | Fresh Ring privacy throughout current camera media | [Camera privacy](camera-privacy.md) |
| `a541906` | Managed installation, rollback and interruption recovery | [Installer design](staged-install-design.md) |
| `a51052f` | Camera light outcomes and native motion-timer information | [Camera lighting](camera-lighting.md) |
| `684556d` | Individual Beams controls and bridge-only discovery | [Beams support](beams-support.md) |
| `ac8f2c4` | Locked x86_64 GNOME 50 Flatpak candidate | [Flatpak qualification](flatpak-packaging.md) |
| `3f92e69` | Explicit Intercom Video sessions, listening and hold-to-talk | [Intercom support](intercom-support.md) |
| `74462a8` | Optional account-bound local Alarm history | [Local history](local-history-design.md) |
| `603dd77` | Startup survives a failed packaged-logo decoder | Native startup and navigation regression |

## Changes made during implementation

- Camera permissions come from explicit successful Ring reads, not permissive
  SDK defaults. Missing or expired permission prevents current media. Existing
  historical recordings retain their separate access path.
- Preview refresh during a live session reuses an authorized frame or a cached
  Ring snapshot; it does not ask the camera to take another snapshot.
- Camera, group and Beams light commands share one admission guard. Camera/group
  GETs begun before command acknowledgment cannot confirm the command's outcome.
  Beams uses later pushed state after socket admission and does not infer
  automatic-mode confirmation from an On/Off report.
- Beams ownership is qualified per bridge. A Smart Lighting bridge does not
  confer Alarm authority and does not require an Alarm base station.
- Managed installation stages committed source, verifies its final-path runtime
  and preserves recovery information before activation. Shared system libraries
  remain outside the rollback boundary.
- The Flatpak builds a real Python distribution and installs every selected locked
  runtime and build dependency inside `/app`; build-SDK packages cannot
  accidentally satisfy a runtime dependency. Unsupported host autostart is visibly disabled.
- The Ring SDK marks all building Intercom wrappers as shared, regardless of
  account role. Intercom media therefore uses fresh account-visible inventory
  and exact device identity, and leaves request authorization to Ring. Tests
  exercise the actual SDK wrapper instead of assuming that flag proves ownership.
- Intercom capture ownership is retained until cleanup succeeds. Failed startup
  submission releases unused admission; partial SDK shutdown retains the original
  socket/task handles for cleanup. Video's protocol does not enable audio-only
  models automatically.
- Local Alarm history remains off by default and requires a fresh authenticated
  account binding plus recent, location-specific history permission. Bulk storage
  work runs outside the media event loop and shared session locks. Delayed results
  cannot revive revoked location access or cross account boundaries.
- Credential metadata keeps the existing OAuth fields readable by older Halo
  releases. Current Halo strips local metadata before SDK authentication; an older
  release may discard the optional history binding during token refresh.
- Saved history retains original retrieval-based expiry across shifted cloud
  pagination. Interrupted-write cleanup and account-revocation races have fault
  fixtures; saved copies never imply complete Ring coverage.

## Final verification

The final source checks cover the implementation through `603dd77`. All tests
used fixtures, generated media or isolated local environments; live-device
acceptance remains separate.

| Check | Result |
| --- | --- |
| Full host pytest suite | 2,271 passed; 14 opt-in native checks skipped in this run |
| Opt-in native checks | All 14 passed separately, including generated playback and narrow/wide dialogs |
| Ruff lint and formatting | Passed across 215 Python files |
| Source/wheel build and isolated installed-wheel imports | Passed; all 39 installed production packages compatible |
| Frozen lock, strict settings schema and desktop entry | Passed |
| Isolated GNOME 50 Flatpak build and Platform imports | Passed; 93 application modules and 43 selected distributions under `/app` |
| Expanded Flatpak runtime suite | 198 passed, including native UI and synthetic media |
| Flatpak startup with image loading unavailable | Separate failure-injection startup/navigation check passed |
| Managed installer | Isolated staging, activation, update, rollback, uninstall and checkout migration restoration passed |

The expanded Flatpak run initially exposed a harness restriction: the GNOME
image loader needs the sandbox-spawn portal and the normal sandbox runtime
socket location. Qualification now enables that broker through a filtered bus;
Secret Service and host command execution remain unavailable. The application
also gained a native symbolic fallback so a failed logo decoder cannot prevent
startup. The normal-path tests still require a real decoded logo, so fallback
cannot conceal a packaging failure.

The host run used Arch Linux with Python 3.14.10, GTK 4.24.1, libadwaita 1.10 and
GStreamer 1.28.9. Flatpak used Python 3.13.16, GTK 4.22, libadwaita 1.9 and
GStreamer 1.26.11. Existing upstream deprecation warnings remain; these runs had
no test failures. No production dependency version changed during the feature
slices after the initial maintenance baseline. The packaging group adds build
inputs without changing the native application's production dependency set.

GitHub CI was not rerun because this pass was not pushed. Test packages were
installed only in isolated environments; the user's installed application was
not replaced.

## Remaining evidence gates

- **Permanent manual floodlight hold:** ordinary On/Off and fresh outcomes are
  available. Native indefinite On still needs actual-model evidence. Repeated
  On commands could fight an Off request from another app, so no keepalive was
  added.
- **Native Ring group editing and schedules:** the inspected references provide
  discovery and timed actions, but no qualified mutation contract for creation,
  membership or schedules. These writes were not guessed.
- **Audio-only Intercom calling:** Video's streaming evidence cannot establish
  another model's protocol. Existing status and confirmed release remain.
- **Hardware and desktop acceptance:** synthetic media/control tests do not
  establish Ring account behavior, actual sound quality, physical light timers,
  building Intercom hangup, keyring prompts, desktop portals or every Linux
  distribution.
- **Halo Server connection mode:** deferred by the owner's explicit decision.

The supporting evidence and next acceptance steps are in
[integration research notes](backlog-research-gates-2026-11-16.md). No Alarm mode,
siren, panic, entrance-release or audible device tests were sent. No real camera
or microphone session was opened by these tests.
