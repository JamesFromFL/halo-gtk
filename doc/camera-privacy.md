# Camera privacy

Halo requires explicit Ring privacy settings before displaying current camera
media. This protection is always enabled. SDK convenience defaults and a cached
device list cannot grant access.

| Current media | Required Ring settings |
| --- | --- |
| Live video | `live_view_disabled` is explicitly `false` |
| Camera previews and notification images | Live video allowed and `motion_detection_enabled` explicitly `true` |
| Live listening and microphone talkback | Live video allowed and `enable_audio_recording` explicitly `true` |

The audio setting remains independent of motion detection. Missing audio
permission disables listening and talkback while allowing qualified live video.
Historical recordings and files already saved by the user remain accessible;
current privacy settings do not erase earlier recordings.

## Freshness and changes

Settings are copied immediately after a successful Ring inventory read and remain
valid for at most 120 seconds, including time the computer spends suspended. A
background check runs every 30 seconds and refreshes inventory older than 60
seconds. Failed reads do not extend permission. Unknown, expired, and explicitly
denied settings appear on camera cards and in focused view.

Each operation carries a grant tied to its exact client, device object, and
settings revision. A replacement account or device, changed settings, or expired
receipt retires older work. Delayed previews cannot repopulate the cache after
privacy is disabled and re-enabled. Unchanged fresh receipts preserve active
sessions. Revocation flushes queued live media, suppresses queued microphone
samples, clears displayed current imagery, and closes the stream. A stream can be
started again once its required settings are verified.

A desktop camera alert captures its account and preview grant when the push event
arrives. Both the alert and its View action expire with that ownership. Halo
withdraws affected visible alerts on sign-out, account replacement, a changed
grant, or receipt expiry. An event received without a preview grant may still
show a basic motion or doorbell alert. Its enrichment never starts later if Ring
grants access. Halo rechecks the preview and summary preferences at display time.

Changing Motion Detection in Halo temporarily withdraws that camera's media
grant. Inventory reads are serialized with the existing setting write; only a
read begun after completion can restore permission. If Ring accepts the write
but its settings cannot be read back, Halo reports that uncertainty and keeps
media hidden until a successful refresh.

While a live session exists, a current preview reuses its latest qualified frame.
Before the first live frame, Halo asks only for Ring's cached snapshot rather than
issuing a fresh snapshot request that could interrupt streaming.

## Verification

`tests/test_camera_privacy.py` exercises strict settings parsing, suspend expiry,
account and device replacement, delayed results, cache revocation, audio gates,
motion-write readback ordering, and media cleanup. Related media tests use local
synthetic WebRTC streams. Native layout checks are opt-in through
`HALO_NATIVE_LAYOUT_TESTS=1` on an isolated GTK display. These tests make no Ring
requests and do not open a real microphone, speaker, or camera; live-service
acceptance and the delay before Ring reports an external setting change remain
outside their coverage.
