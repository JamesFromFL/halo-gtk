# Camera light outcomes

Camera cards and focused view distinguish a pending request, Ring's acceptance,
and a matching light state reported by Ring. Acceptance alone does not establish
that the lamp changed. A failed or mismatching readback displays an unconfirmed
outcome; the next valid inventory read can establish the current state.

Light observations use only the exact `led_status` values `on` and `off`, copied
immediately after an actual successful inventory GET. SDK convenience defaults,
mutable wrapper attributes, cached inventory access and the old optimistic command
value cannot certify current camera light state. Observations expire after 90
seconds, including computer suspend time, and remain tied to the exact client and
device object.

One no-queue reservation covers camera, group and Beams lighting intents. A
mutation revision advances at send admission and again after camera/group HTTP
acceptance. Inventory GETs carry the revision sampled before they start, so a GET
started before or during the write cannot masquerade as a post-acceptance
observation. A camera request performs one bounded readback begun after acceptance.
It does not repeatedly poll or resend the write. A group or Beams mutation
conservatively invalidates previous camera lighting observations.

Camera writes use Halo's shared bounded JSON transport and the fixed pinned-SDK
PUT route `/clients_api/doorbots/{device_api_id}/floodlight_light_on|off`. They do
not call the retry-capable SDK setter. The controller retains its reservation
through actual cleanup when its presentation consumer detaches, and client shutdown
joins owned requests before closing authentication resources.

The native motion-light duration is read-only: only integer
`settings.floodlight_settings.duration` values from 30 through 900 seconds are
shown. A conflicting or malformed duplicate `light_settings` surface makes it
unknown. Manual timer fields do not establish an indefinite hold, a motion policy,
or an observed On state. Halo does not write these timer fields.

When an accepted On request is unconfirmed, the toggle retains the explicitly
labelled requested value so the user can issue an explicit Off request. The label
continues to distinguish the request from the actual reported state.

Verification uses synthetic transport/SDK fixtures and isolated native GTK renders.
It proves local parsing, bounds, cancellation and ordering, not live Ring acceptance
or the physical lamp's behavior. No real Ring account, camera or actuator is used.
