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

Calling, live audio/video, guest keys, access invitations, setup, configuration
writes and per-door selection remain in the Ring app. The pinned SDK lacks
Intercom WebRTC methods; an external custom integration's monkey patch and
Audio-only prerelease do not establish Halo hardware compatibility. No SDK
class patch, media backend replacement or dependency change is introduced here.

See the accompanying research plan for source links. Live account schema,
permissions, actual entrance behavior and native dialog overlay/focus require
supervised target-hardware testing. Development tests use synthetic responses;
no real Ring account read or door-release action is part of implementation.
