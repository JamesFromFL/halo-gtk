# Light Groups support

Devices lists existing Light Groups for the selected Ring location. Locations
are discovered independently of cameras or Alarm, so lighting-only locations can
appear. Logical groups do not increase the physical-device count and never enter
camera media workflows. Group creation and setup remain in the Ring app.

Opening group details reads its reported aggregate state and available members.
Missing, malformed or old state stays Unknown or stale. **Group reports On/Off**
does not establish that every physical lamp switched. Member details show
validated names and types; group state never fills missing individual values.
Unknown permissions are distinct from an actual denial.

## Explicit controls

On offers 1-, 5- and 15-minute presets, with one minute selected initially.
Changing the preset stays local until the On action. Off is a separate request
and does not disable Ring motion rules, schedules or linked devices. Those
automations or another app can activate lights again.

The presets are Halo policy, not a documented Ring API range or a guaranteed
physical shutoff time. No local timer issues Off or declares that the group is
physically off when a requested duration expires.

One POST is followed by at most two fresh group report reads within a 30-second
total budget. A matching report is identified as an observed group report;
acceptance alone is not confirmation of physical lighting. Failed readback,
timeout or cancellation after possible transmission can leave an unknown result.
No automatic write retry, alternate payload or rollback is performed.

Unknown operation rights do not become an invented owner-only restriction.
Deliberate manual requests are authorized by Ring. An actual 403 is scoped to
the relevant group/location and needs deliberate refresh/retry or a session
change. There are no automatic writes to probe permissions.

## Ownership and limits

Camera and group light actions share one client-local reservation with no
actuator queue. A competing action returns Busy. Each owned camera light action
also has a 30-second deadline including SDK auth waits. Closing a view prevents
an unsent group intent; an admitted operation remains owned until its transport
work settles. Cancellation cannot undo a request Ring may already have received.
Camera completion commits its local override before releasing the reservation;
later GTK presentation cannot restore a value invalidated by a group action.

Group/member identity includes the current account, location, group and identity
epoch. Invalid or incomplete lists preserve stale evidence; only complete,
validated results authoritatively remove data in their exact scope. Deletion
markers, duplicates, membership changes and reappearing IDs cannot revive an
old intent. Private addresses, coordinates, owner emails, MACs and raw payloads
are not needed in the interface or logs, and no command journal is persisted.

Details refresh at most every 15 seconds while visible, with no overlap or poll
backlog. Hidden pages/windows stop their read consumers and timers. Permission
and authentication failures pause automatic polling; transient failures back
off and server rate-limit delays are honored. Cached Devices refresh never
starts network work. Manual refresh has a five-second local admission limit;
controlled command preflight/readbacks bypass that local cache/cooldown while
retaining server backoff and the whole-operation deadline.

The feature limits discovery to 16 locations, 128 groups per location, 512 cached
groups, 128 members per group and 8192 cached members. Exceeding a limit does not
truncate data into authority. Requests use the client-owned JSON transport's
two aggregate slots, one non-recovery slot and Alarm recovery priority. Group
HTTP has a ten-second request budget, 1 MiB read limit, depth 32/node 50,000 JSON
limits and 64 KiB write-reply limit. Camera SDK/media/OAuth limits are separate.
Command evidence expires after 30 seconds measured across suspend.

## Experimental coverage

This implementation is verified with synthetic responses and offline lifecycle,
transport, UI and package checks. Real Ring schema, role/model coverage,
physical illumination, duration and native presentation must be validated on
the target account and hardware before declaring live support. Live reads and
specific normal On/Off actions are a separate supervised checkpoint.

Group brightness, color/temperature, rename, membership editing, schedules,
linked-device rules, per-member commands, Bridge provisioning and Alarm
automation editing are not implemented by these controls. Existing individual
camera-light support remains separate from group aggregate reports.
