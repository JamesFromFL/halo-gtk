# Native interface redesign — 16 November 2026

The approved GNOME-inspired design is integrated into Halo's existing GTK 4 and
libadwaita application. The separate visual prototype was a design reference;
production views use the existing Ring clients, media sessions and device actions.

## View map

| View | Production behavior |
| --- | --- |
| Overview | Cached reported security status, the first three ordered camera previews, up to three events received while this view is open, and shortcuts to the full views. |
| Cameras | Adaptive preview/monitoring grid, camera filters, saved layouts, stream limits and a selected-camera inspector. Opening a camera enters the focused live player. |
| Activity | Date-grouped event timeline and adaptive list/detail playback. Existing filters, favorites, downloads, sharing, deletion and Alarm history remain available. |
| Devices | Search and device-type filters over the discovered inventory, with the existing Chime, Light Groups and Intercom detail/control flows. Camera and Security shortcuts lead to their dedicated views. |
| Security | Reported Alarm state and transitions, location selection, sensor groups, health and confirmed mode-request workflow. |

Wide windows use a native header view switcher. Compact windows use a bottom
switcher. Focused playback has a Back action and keeps the image clear of its
wrapping controls. Follow system, Light and Dark are saved appearance preferences.

## Data and lifecycle decisions

- Device types are used instead of fabricated rooms: the existing inventory does
  not provide a reliable room assignment for every supported device.
- Overview consumes cached Alarm snapshots and incoming events, without adding
  a history polling loop. Activity remains the complete history workflow. Recent
  Overview events are scoped to the account and cleared on account replacement.
- Security summaries show unavailable, stale and transition states explicitly.
  An alarm mode never establishes whether doors are closed or cameras are private.
- Preview labels say when Halo loaded the image, rather than claiming its age is
  a verified camera capture timestamp.
- Page navigation retires subscriptions and pending UI operations. Returning
  from focused monitoring retains the existing shared-session ownership rules.
- Closing to background stops media. Reopening resumes observation; a closed
  focused player returns to its source view without restarting the live camera.
- Theme resets synchronize both the menu and the style manager. A failed save
  restores the previous selection and reports the failure.
- Existing command confirmations, capability checks, uncertain outcomes and
  security guards remain in their underlying action paths. No new hardware
  action is triggered by rendering a page or selecting an appearance.

## Validation scope

The complete regression suite, Ruff lint/format checks and package import/data
checks were run. Added regressions cover Overview account and callback lifecycle,
event routing, stale security summaries, compact navigation, theme reset/save
failure, focused-view cleanup and the adapted page presentations.

Production GTK widgets were rendered on an isolated local Broadway display at
desktop and compact widths, with light and dark appearance checks. Visual fixtures
used fictional local scenes and synthetic state, with no Ring account. Window
allocation and inspector state were checked separately from screenshots because
headless Broadway can retain an old frame when a window is repeatedly reused.

These checks do not establish live Ring camera, microphone or alarm operation,
or a complete compositor/screen-reader input pass. No physical device commands,
alarm mode changes, installation, commit or push were performed for this redesign.

## Deferred feature work

The earlier capability, privacy and reliability recommendations remain separate
from the visual redesign. Adding new Ring integrations, backend controls or media
features requires their own implementation and validation; the interface does not
present speculative capabilities as working controls.
