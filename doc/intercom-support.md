# Intercom support

Devices lists recognized Ring Intercom Audio and Video units with reported
connection, battery, firmware and ring-to-open configuration. Missing values
stay Unknown; old values are labeled stale. A zero battery remains reported
zero. These are experimental cloud reports, not proof of reachability or door
position. Physical lock and door state always stay Unknown.

Entrance release requires an explicit action followed by confirmation naming
the current entrance. Cancel is the default. Confirmation is single-use and
expires after 30 seconds across Linux suspend. It is checked against account,
API ID, location, kind, hardware identity, revision and identity epoch before
final HTTP admission. There is one client-local release operation and no queue.

The fixed RPC follows the pinned Python SDK: one PUT, unique UUID request,
JSON-RPC unlock_door, door 0, default user -1. The reply has a separate bounded
JSON read. Only a strict integer result code zero is acceptance. HTTP acceptance
alone, missing/malformed replies and timeout can mean an uncertain result.
No response establishes physical opening, door position or subsequent locking.
No write is automatically replayed on authentication, rate limit, network or
server errors; there is no rollback, timer release, relock or alternate payload.

After accepted or possibly-sent uncertain results, check the entrance and
acknowledge that exact request before another release. This gate survives
closing details and navigation within the client session. It does not detect
other apps' actions and does not survive a new session or crash. Acknowledgment
clears only a local gate. Logout or view close cannot undo a request already
received by Ring. Some systems require a visitor to buzz before release, and
Ring warns that success notification can occur without opening the intended door.

Inventory has a five-second attempt cooldown, including canceled reads, and
30-second command freshness. Selected visible details poll no more frequently
than every 15 seconds, with no overlap/backlog. Authentication failures pause
polling; actual denial pauses the affected detail until deliberate reload.
Transient failures back off and rate limits are honored, capped at 300 seconds.
Inventory admits at most 512 other-device rows and 128 Intercoms. Invalid or
oversized inventories retain stale evidence rather than authoritatively removing
it. Shared HTTP limits are 10 seconds, 1 MiB reads and 64 KiB write replies,
depth 32 and 50,000 JSON nodes. Whole release ownership is bounded by 30 seconds
including auth/slot waits and joined cleanup. Only the current view's read
consumer is canceled; admitted release work remains owned by the service.

Existing FCM call and unlock events create bounded, deduplicated observations
(16 per device, 128 recent identities) in memory. Four event consumers at most
share one bounded discovery read, including when Devices has not been opened.
Delayed, malformed, wrong-kind and update events are discarded. No event,
notification click, startup or refresh releases an entrance. Notifications
respect the global and doorbell preferences; their sole action opens current
Intercom details and never opens a camera or enriches camera media. Logout
withdraws notifications for the retired account. Missing events do not imply
that no calls or unlocks occurred.

## Experimental Intercom Video

Account-visible devices with the exact `intercom_handset_video` kind can open an
experimental media dialog from Intercom details. Enable the local per-view
checkbox, then press Start when you intend to connect. Starting may open or
answer the building intercom's media path. Opening details, incoming events,
notifications, polling and enabling the checkbox alone never start media.

Listening starts muted. Listen enables received audio; Hold to talk requests
the microphone only while the pointer or Space/Enter key is held. Releasing,
canceling the gesture, leaving button focus, ending or closing the view stops
talkback. End closes the media session. Entrance release remains a separate
confirmed operation and is never triggered by these media controls.

The media authority is deliberately separate from camera privacy: Ring's
camera GET flags do not establish Intercom permissions and are never fabricated.
Admission requires the pinned SDK contract, explicit local opt-in, a current
client, fresh complete Intercom inventory, the exact model and identity epoch,
a matching current SDK wrapper. This establishes current account access, not
an owner role; Ring decides permission for the media request. The pinned SDK
constructs every Intercom wrapper with `shared=True`, so that synthetic flag is
not used as an ownership or permission claim. Unknown, stale and denied current
account evidence cannot start media.
The existing selected-details inventory poll runs every 15 seconds, including
while its media child is open. These metadata reads do not initiate calls or
release the entrance. Lost or stale access evidence ends media; a later fresh
receipt cannot resurrect that lease. Freshness and the five-minute session cap
use the Linux suspend-aware clock. An expired session requires another explicit
Start. A read failure or cooldown can end a session before five minutes.

Only one capture consumer is admitted per Intercom in the client. Ownership
continues through microphone, peer and Ring teardown, including natural Ring
reader closure. Failed or canceled local teardown keeps that ownership reserved;
close Halo before trying again when cleanup cannot be verified. There are no
automatic snapshots, background media consumers, recording saves or stream
renewals for Intercom Video.

The instance-owned adapter reuses `RingWebRtcStream` from `ring-doorbell==0.9.14`
and the existing Halo live media pipeline. It does not patch `RingOther`, change
dependencies, replace the backend, or change ordinary camera and doorbell
privacy/talkback behavior. The source evidence is the upstream integration's
[video camera adapter](https://github.com/cmos486/ring-intercom-video/blob/52f2888fdc51c4c01369ccd9536ac2d090094e06/custom_components/ring_intercom_camera/camera.py)
and [SDK integration](https://github.com/cmos486/ring-intercom-video/blob/52f2888fdc51c4c01369ccd9536ac2d090094e06/custom_components/ring_intercom_camera/__init__.py).
Its global patch is not reproduced. Source compatibility and synthetic fixtures
do not prove video, received audio or talkback on a physical Intercom.

Audio-only Intercom calling, guest keys, access invitations, setup,
configuration writes and per-door selection remain in the Ring app. Existing
camera-doorbell talkback is independent of this building-intercom experiment.

## Qualification limits

Offline tests cover qualification, fresh/expired receipts, account/identity
changes, one consumer, cancellation and natural-reader cleanup, failed cleanup,
worker-submission failure, delayed talkback, explicit controls and parent polling.
Native GTK layout and closed-state checks use synthetic inventory with no media
session. Live account schema, permissions, actual entrance behavior, physical
Intercom video/audio/talkback, and compositor input/overlay behavior still require
supervised target-hardware testing. No real Ring request, door-release action,
call or microphone capture is part of this implementation's development checks.
