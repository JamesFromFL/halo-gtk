# Individual Beams lights

Halo uses the existing authenticated Ring hub connection for individual Beams
lights. Devices → Lighting lists these lights separately from Light Groups.
No device state is inferred from group state.

Supported writes are limited to `switch.beams`, `switch.multilevel.beams`, and
`switch.transformer.beams` attached to an exact `beams_bridge_v1` asset. Other
reported Beams types remain read only. The presence of a multilevel type does
not enable brightness control.

An explicit request can turn a light on for 30 seconds, 1 minute, 5 minutes, or
15 minutes, or return it to Ring automatic behavior. Automatic is not forced
Off: motion or other Ring behavior may keep the light on. Halo does not claim
that an On/Off report proves automatic mode or the remaining override time.
There is no permanent hold, repeating write, or software auto-Off timer.

The protocol is a typed `DeviceInfoSet` / `light-mode.set` command:
`{"lightMode":"on","duration":seconds}` or `{"lightMode":"default"}`.
The latter has no duration. This contract matches the qualified local server
implementation and the pinned [Homebridge Ring Beams implementation](https://github.com/dgreif/ring/blob/152ea689e3bdc14632dd0e98dd14358cb376f8cc/packages/homebridge-ring/beam.ts).

## Permission and observation

- Controls default disabled. The local enable switch and default duration are
  stored in a private `beams-settings.json`, scoped to account, location,
  bridge, and light. Saving preferences never actuates a device.
- A fresh authenticated devices response must contain a unique bridge `id`,
  exact location, and explicit `role: owner`. That positive integer ID must
  match the current ticket asset's unique `doorbot_id`. Missing, ambiguous,
  shared, or stale ownership cannot authorize a write. Owning a base station
  does not establish ownership of a bridge, or vice versa.
- Bridge and light must be online, with a complete current inventory, exact
  boolean `on`, and `commStatus: ok`. Inventory, field receipts, and owner
  evidence expire after 120 seconds, including computer suspend.
- Camera, group, and individual-light requests share one reservation. A busy
  request is rejected rather than queued. Every admitted lighting write
  invalidates older individual-light observations through a shared revision.
- Admission rechecks the account, runtime, epoch, light revision, local
  preference, owner evidence, and reservation at the socket handoff. The
  command is never replayed. Outcome publication rechecks account, runtime,
  owner, connection, current observation, and lighting revision.
- A subsequent On report can establish only that On was reported after the
  request. Socket completion alone leaves the outcome unknown. Unknown
  outcomes require another explicit light report before another request.
  Closing the view revokes an unsubmitted intent; it cannot recall sent bytes.

## Bounds and verification

The existing discovery path permits at most 1,024 combined hub records and 128
locations. Beams retains and displays at most 1,024 light observations, and
stores at most 512 local preferences in a 128 KiB file. Commands have a
10-second send/observation deadline; discovery plus command has a 22-second
overall deadline. Closing the service cancels and joins its owned requests.

Fixture tests cover exact wire payloads, bridge-only accounts, ownership
joins, stale data, account/runtime changes, duplicate IDs, unsupported models,
shared lighting contention, missing readback, removal during a send, and local
policy. Native GTK tests use synthetic snapshots and futures at 420 and 700
pixels. These checks do not establish live Ring acceptance or physical light
behavior. No live device command is part of this verification.
