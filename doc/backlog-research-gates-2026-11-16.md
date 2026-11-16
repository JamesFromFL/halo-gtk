# Remaining integration decisions and evidence

This note records the parts of the saved backlog that cannot be described as
ordinary ports of completed Halo Server features. Reviewed on 16 November 2026.
It complements the completed-item links in [the backlog](improvement-backlog.md).

## Durable manual camera-light hold

The Server's camera-light work does not implement or prove indefinite On. Its
engineering notes distinguish the plain manual command from Ring's native motion
timer. Neither `always_on` nor zero-valued duration fields prove a permanent hold.
Repeated On requests would also risk undoing an explicit Off from Ring.

The GTK implementation should expose qualified manual commands and separately
reported state. It must not add a keepalive or invent a permanent timer value.
Owner-operated acceptance needs On beyond the motion interval, motion during that
interval, explicit Off in Halo and Ring, and resumption of normal motion lighting,
repeated for each actual model. This is a hardware-evidence gate, not a missing
generic timer implementation. No such device actions were performed in this pass.

Source: Halo Server `fcf44f90e9fb54a00d386c5f0b89c84109ddc8b6`,
`docs/engineering/camera-light-controls-2026-11-15.md` and `camera_lighting.py`;
[Ring light settings](https://ring.com/support/articles/b7aj8/using-light-settings-for-ring-security-cameras).

## Intercom answering and live media

The pinned Python SDK exposes Intercom observation and entrance release but does
not expose a qualified Intercom stream interface. Current
[ring-client-api Intercom code](https://github.com/dgreif/ring/blob/main/packages/ring-client-api/ring-intercom.ts)
(blob `c0ff564147f1455727b8feea7fa2b560784ed0a6`) also does not provide calling.
[Homebridge's maintainers](https://github.com/dgreif/ring/wiki/Common-Feature-Requests#ring-intercom-audio-video-and-call-handling)
describe device variation and limited access to test hardware.

There is useful newer primary evidence: the
[Ring Intercom Video integration](https://github.com/cmos486/ring-intercom-video)
adds streaming specifically for `intercom_handset_video` using RingWebRtcStream.
Its initialization source (blob `5ac200d93a6d76cc48b337062475266fe6e98b6e`)
patches RingOther globally; GTK should instead use an owned adapter around the
existing Ring session. Its camera source (blob
`338532db833db45cbe45663716d238a938e5604b`) notes a single usable capture path,
snapshot/live contention, and a close-callback self-await workaround. These are
real lifecycle requirements; a generic camera capability flag is insufficient.

Proposed implementation: explicitly opt-in model-qualified calls; one owned live
session per Intercom; Start/End and push-to-talk; no automatic answering or
microphone activation; no snapshot-only WebRTC sessions; bounded signaling and
account-revocation teardown; confirmed entrance release remains a separate action.
Use the existing raw media pipeline. First qualify offer/answer, incoming-call
lifetime, reconnect, silence/mute, and final microphone/session teardown on the
actual target model. Video-model evidence must not enable audio-only models.

The necessary decision is the model to target and availability of an owner-operated
call test. A fixture can establish cleanup logic, but cannot establish that a real
building intercom answers or hangs up correctly. No call or entrance-release test
was run here.

## Ring group editing and schedules

The inspected current
[location adapter](https://github.com/dgreif/ring/blob/main/packages/ring-client-api/location.ts)
(blob `f94897ab5b76cdf6506f9bf2be2f3cb8d08f73d6`) supplies group discovery and a
timed `lights_on` command. It does not provide a verified create/delete, membership
edit or schedule-write contract. The previously captured Scrypted Ring location
adapter has the same timed-light command shape. This is evidence about those
implementations, not proof that Ring has no such private API.

Existing GTK group discovery and timed commands remain useful. Further editing
requires sanitized request/response evidence for each mutation, permission and
conflict behavior, and a reversible test group. Do not repurpose a read route or
infer write semantics from a generic settings endpoint. A Halo-only schedule is
a different feature that would run only while Halo is awake and connected; it
must not be advertised as a native Ring schedule.

## Optional Halo Server connection mode

Decision: the owner chose **Keep GTK direct-to-Ring for now** on 16 November 2026.
The architecture below is retained as a future proposal, excluded from this pass.

This is a separate architecture choice from adding direct-Ring controls. The
current Server plugin protocol v3 supports camera inventory, snapshots, camera
controls, events and a camera talkback socket named `intercom`. That socket does
not establish support for Ring's building Intercom devices. It does not supply a complete
desktop contract for all noncamera controls or cloud event history. The owner
web console uses session cookies, CSRF protection, operation IDs and step-up
authentication; a desktop client should not bypass those protections or scrape
the web interface.

Recommended scope for a separately approved change:

1. Keep direct Ring as the default and add an explicit **Connect to Halo Server**
   account type, with one active backend per profile.
2. Add a versioned desktop API to Server: capability discovery, scoped/revocable
   client credentials, account epochs, bounded inventory/event updates, typed
   command outcomes and media leases. Camera and noncamera support must be
   separately advertised. Do not give desktop clients owner-administration rights
   merely to read devices.
3. Store only the desktop credential in Secret Service. Require validated TLS or
   deliberate certificate pinning after owner verification; never silently disable
   certificate validation. Handle expiry, revocation and server replacement.
4. Introduce a GTK backend interface for inventory, observations, commands,
   history and media. Adapt existing direct-Ring services behind it, then add the
   Server implementation. Existing direct-Ring accounts must continue to work.
5. Test disconnects, stale epochs, absent capabilities, revoked credentials,
   simultaneous consumers and media cleanup before owner-operated acceptance.

Any future approval would cover this optional second backend and the corresponding
Server API work. No Server
code, deployment, credentials or pairing were changed by this backlog pass.

## Broader Linux packaging

The optional managed installer improves updates on an existing compatible host.
It does not bundle system GTK/GStreamer or establish support for another Linux
distribution. A Flatpak is the recommended next packaging candidate for this
GNOME-oriented app, subject to a separate sandbox/portal and runtime qualification:
Secret Service, microphone, PulseAudio/PipeWire, file dialogs, notifications,
background/tray behavior, and the pinned media dependencies all need acceptance.
Select the intended distribution/runtime before publishing installation promises.
