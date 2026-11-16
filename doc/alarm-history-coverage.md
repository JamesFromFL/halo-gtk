# Alarm history coverage

Halo reads Ring's location-scoped cloud history on explicit request. The current
adapter uses bounded offset pages (up to 50 rows), with a maximum accepted offset
of 10,000. It does not assume this provides every event Ring possesses.

The history view reports the most recently fetched page's source-row count,
displayable events, unsupported rows and retrieval time. Retrieval time is not
the event's occurrence time. Missing or unfamiliar fields remain visibly partial.
Multi-page service reads preserve aggregate unsupported-row counts.

Repeated full pages stop pagination. Partial overlaps also stop pagination while
preserving the returned occurrences. The local fingerprints are comparison aids,
not verified Ring event identifiers: equal fingerprints can represent distinct
events, and shifting offset pages can repeat an event. Halo therefore does not
silently deduplicate partial overlaps or label the result complete. A full page
at the final local offset stops with an explicit incomplete-coverage message.

The current source reference remains
[ring-client-api's location history adapter](https://github.com/dgreif/ring/blob/main/packages/ring-client-api/location.ts),
reviewed blob `f94897ab5b76cdf6506f9bf2be2f3cb8d08f73d6`. Its flexible query wrapper
does not establish stable event IDs or snapshot-consistent pagination. Confirming
coverage needs account-authorized read-only comparisons against Ring's own history.

Validation: 138 history, presenter and REST tests pass, including shifted pages,
exact repetition, unsupported rows, offset limits, access revocation, cancellation
and bounded transport. No Alarm state-changing request is part of these checks.

Durable local history is a separate feature from this session cache. It needs
explicit opt-in, bounded retention, account-scoped storage, local-source labels,
revocation behavior and deletion controls; no durable log is enabled by this fix.
