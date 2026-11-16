# Optional local Alarm history

This slice retains bounded copies of Alarm cloud-history events the user has
explicitly requested. It adds no subscription claim, automatic history polling,
Alarm command or completeness claim. Retention defaults to **Off**; the other
choices are **7 days** and **30 days after retrieval** for the current account.

## Account binding

Persistent history needs stronger ownership than the display-email cache.
A successful fresh authentication binds a random local profile identifier to
that candidate's authenticated username. A bounded account-to-profile mapping
lives in Secret Service, allowing the same account to reconnect to its profile.
Neither email addresses nor token hashes become disk directory names.

A reserved, versioned metadata member stores the profile binding atomically with
the OAuth fields in one Secret Service item. OAuth fields remain at the top level
so previous Halo versions can still read the token; current Halo strips local
metadata before calling the SDK. Current token refresh preserves the binding.
An older Halo release may discard that optional metadata during refresh, so a
later current release will require fresh sign-in for local history again.
Existing plain-token Secret Service values and the legacy-token migration remain
readable; an old unbound token
continues to authenticate but cannot read or save persistent history until a
fresh sign-in establishes ownership. That limitation is explained without
forcing logout. Profile persistence failure disables this optional feature and
does not attribute data to a cached email from another account.

Candidate binding is private until successful current-session publication.
Reads, writes, retention changes and clear operations require the published
client/generation and the corresponding account profile. Location data also
requires the current online location and a successful, uncached cloud-history
response for that location within the last five minutes of this session. General
inventory or socket traffic cannot extend that permission receipt. Saved reads
perform no network request: when the receipt expires, the view asks the user to
choose **Ring cloud → Refresh** to verify access. Offline, unknown, denied or
expired authority never grants access. Authoritative removal or an explicit
permission-denied response immediately revokes the in-memory permission receipt
and schedules private-file removal. A session-wide increasing response serial
prevents a removed/regranted location from reusing an older operation's authority.
Delayed work rechecks ownership and response revision at atomic commit and at
result publication. Queued and visible UI rows also recheck current access.
Cleanup failures remain retryable; unreadable revoked copies are not represented
as successfully deleted.

## Storage and retention

Normalized typed events are stored under the private state directory in opaque
profile directories and hashed location filenames. Files use mode 0600 and
directories 0700. Only allowlisted normalized fields are serialized; raw Ring
responses, credentials and media URLs are excluded. Reads validate size, schema,
ownership, paths and timestamps before constructing records.

The bounds are 16 account profiles, 20 locations per profile, 500 events per
location and 1 MiB per file. Files are atomically replaced under a private,
nonblocking storage lock. Recognized private temporary files left by interrupted
writes are removed under that lock on the next profile access, including Clear;
symlinks and unexpected ownership/permissions are rejected. This is fault-fixture
coverage, not a claim of physical power-loss testing.

Retention expiry uses the original successful retrieval time, never the
time a saved event is viewed. Matching normalized content shares the first
retrieval's expiry even if cloud pagination changes row positions. The store
retains the greatest observed same-page multiplicity, marks overlap as uncertain,
and does not mistake content fingerprints for verified occurrence identities.
It does not extend an existing cohort's expiry; a later explicit fetch after all
copies have expired can create a new copy. Expired entries are pruned when the
profile is accessed; no background network or polling task is introduced.

Turning retention Off stops new collection. Existing copies keep their original
expiry unless **Clear saved history** is chosen. Reducing or increasing retention
applies to newly saved records; it does not silently rewrite old expiry dates.
The UI states this distinction. Logout clears presentation and session authority,
while retained files remain private and expire according to their existing policy.

## Presentation and validation

Current-account preferences expose Off/7 days/30 days and an explicit clear action.
Alarm History offers a clearly labeled saved-history view, distinct from a fresh
Ring request. Saved data retains partial/experimental coverage labels and original
retrieval times. It cannot be used as evidence of current Alarm state or complete
cloud-history coverage.

Settings, saved reads, cloud-response persistence and membership cleanup run in
bounded workers. Bulk reads, scans, serialization and fsync hold neither the
shared session/Alarm locks nor the Ring/media asyncio loop. Only admission,
atomic replace/unlink and result publication briefly take current-account guards.
Routine sensor updates do not rescan files after successful membership cleanup;
membership changes coalesce and failed cleanup remains eligible for retry.
Closing a dialog fences late UI callbacks; a user-authorized operation already
committed for the same current account can finish after the dialog closes.

Validation uses fake Secret Service, synthetic authenticated clients, normalized
events and temporary state directories. Required cases include legacy-token
restore, backward-readable metadata, refresh binding, failed/superseded sign-in,
account switching, concurrent logout/revocation, denied-location purge, corrupt
or oversized storage, exact expiry boundaries, stale callbacks, disabled
collection, explicit clear, orphaned temporary copies, disk-write failures,
slow-I/O responsiveness, removed/regranted location races, and native narrow/wide
dialogs and Saved views. No real account, device,
Secret Service entry or installed application is changed by these tests.
