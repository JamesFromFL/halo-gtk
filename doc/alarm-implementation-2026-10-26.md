# Native Alarm implementation

This extends the existing Python Alarm adapter. It adds no Node service, MQTT
broker, persistent Alarm journal, or actuator controls beyond the existing normal
mode/bypass interface. All live Ring/account/hardware compatibility remains
experimental. Fixture tests establish local behavior, not successful hardware
operation or monitoring/dispatch outcomes.

## Protocol and authentication

The maintained reference is `dgreif/ring` release 14.3.0 at
`3bcc624acc35b6f97c391089a12bff582bb6ed4a`, cross-checked against main
`c72026d72c7b9ccb9ef45b4f64c6affd86d85df2`. History consumer evidence also comes
from ring-mqtt 5.9.3 at `d28fdbd56a850d57cce2fdf43a438033e78f1e6b`.

The ticket route uses `https://prd-api-us.prd.rings.solutions/api/v1/clap/tickets`.
The returned WSS host receives the ticket on the exact `/ws` path. Its separate
session has no cookies, OAuth headers or environment proxy credentials. There is
no Socket.IO dependency or speculative v3 discovery migration.

Alarm REST accepts fixed researched GET operations only. It preserves the pinned
Ring Auth user-agent, hardware identity and token preparation, using the shared
refresh-generation coordinator. Failed responses close before refresh; safe reads
can replay once after coordinated token rotation. Token-save failures stop replay.
Camera requests and SDK-wide headers are unchanged.

JSON response policy: 1 MiB accepted bytes, depth 32, 50,000 nodes, verified TLS,
no redirects or compressed responses, ten-second requests, at most two active
reads. History uses one active request and at most two pending/coalesced keys;
recovery reads retain admission priority. Whole-operation deadlines include slot
waits. These are application-level bounds, not strict RSS limits or force-killable
operating-system DNS deadlines. The SDK still owns its OAuth response reader.

## Current state and normal commands

Hub transport, inventory freshness and per-device `commStatus` are separate.
Missing device communication status is Unknown, not an inferred Online. Sensor
values remain last-known when supervision is lost. A ticket's battery flag is not
a battery percentage; its observation metadata is separate from fresh inventory.
Reported siren state is visibility only.

Inventory refresh registers its completion waiter before request admission and
requires a complete matching current-epoch list after sending. Identical lists
refresh receipt metadata without fabricating semantic changes. Partial initial
sync gets bounded retries; healthy connections reconcile inventory every five
minutes. Manual refresh has a five-second cooldown. Manual refresh/preflight also
reconcile bounded Alarm-only device/location discovery without replacing the
camera SDK's device objects. A failed/malformed read is not authoritative removal.

Mode dialogs use service-issued approval tokens, valid for sixty seconds, and
inventory evidence no older than thirty seconds at the send boundary. Linux
boottime is used where available so suspend does not extend those ages. Client
generation, location/asset/panel identity, epoch, revision, authorization and
reviewed bypass IDs are checked again immediately before sending.

Only one mode operation per location is admitted. Pending/terminal feedback
belongs to the service, so page changes do not lose an operation that may have
reached Ring. Target-bearing panel documents confirm the reported target;
unrelated patches and cached matching mode do not. Exit-delay state remains
distinct from fully armed. A possibly sent command is never automatically retried
or rolled back. Outcome unknown is terminal for scheduling but retained as
feedback; another request requires fresh state and fresh confirmation.

Owner evidence retains its prior behavior; shared authorization is not inferred
from successful reads or camera permissions. A history denial applies to history,
not all device controls. Unknown subscription metadata does not invent either
entitlement or a subscription-specific error. Active alarm/life-safety, cellular,
unknown authorization and incomplete state remain read-only. Transition timestamp
units are unverified, so Halo displays a transition without a guessed countdown.

## History and live observations

`alarm/history.py` reads the separate `rs/history` route, location-scoped with
`category=alarm`. Source-grounded impulse fields receive known labels; other
supported envelopes remain generic/partial. Missing timestamps and actors are
not fabricated. Local row keys are positional fingerprints, not Ring event IDs.
Overlapping/repeated pages and mutable offset pagination have explicit limits;
coverage is never advertised as complete.

History pages contain up to fifty rows; a multi-page load is capped at five pages
and thirty seconds. Session cache policy is five minutes, 1,000 normalized rows
or 8 MiB of logical payload, whichever limit is reached first. This is not a bound
on Python allocator/RSS overhead. Explicit Refresh bypasses a fresh cache. Logout,
account replacement and confirmed access revocation clear affected data and
cancel owned work; transient failures may retain visibly stale data.

The Alarm History presenter bypasses camera fetch/playback/save/delete paths. It
has location/type/device filters, separate request generations and cancellation
on hide/selection/account change. Publication rechecks current account and access
even if revocation's GTK callback has not run yet.

`alarm/events.py` derives typed live observations, never events from a history
page. Initial/reconnected inventories establish routine baselines. Valid current
critical signals, including an explicitly alarming panel with unknown category,
remain eligible even when unrelated inventory is incomplete. Stale values cannot
clear an incident; unknown gaps do not prove a clear. Repeated current signals
deduplicate across reconnects while different signals remain visible.

Current-state rendering coalesces GTK callbacks. Pending notifications are bounded
and coalesced by semantic key, with routine work evicted before critical locations.
Omitted work increments a visible activity-notification gap indicator. Ordinary
mode/sensor/health events are opt-in; the legacy generic FCM Alarm prefix renderer
is suppressed because that parser does not establish a complete Alarm schema.
Camera FCM notifications remain separate.

New preferences live in authoritative `settings.json`, not a second GSettings
store. `alarm_notification_details` defaults to false. Enabling it may expose
household names on the desktop/lock screen; custom Alarm text is included only
with detailed alerts enabled. Notification actions navigate only, never Disarm
or Bypass. Generation/target checks and best-effort displayed-alert withdrawal
protect account replacement and revocation. OS Do Not Disturb and delivery are
outside Halo's guarantee.

## Validation gates

Automated coverage includes bounded REST/auth races, refresh send-order races,
stale/unknown state, approval expiry/admission, history pagination/cache/cancellation,
revocation races, notification baseline/urgency/redaction and account-safe routing.
The source and installed wheel must also pass lint/format, lock/schema/desktop
validation, full regression tests and import checks.

Before changing the experimental status, obtain separate consent for read-only
comparison against the official app: hardware generation/Pro, region, role/plan,
inventory, normal sensor observations, permissions, history schemas/timestamps,
token renewal and reconnect. Capture only redacted structural fixtures. A normal
mode trial requires another explicit checkpoint after read-only parity; no panic,
fire/CO, siren or monitoring response is triggered as a test. A 24-hour observer
soak and multi-hour desktop session remain acceptance targets, not completed
measurements. Keep the official Ring app/keypad available for critical response.
