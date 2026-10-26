# Proposed staged installation and rollback

Status: design for review. No managed installer, migration or rollback command is
implemented by this document. The existing checkout workflow remains supported.

## Problem and acceptance condition

The current updater advances Git before dependency/runtime validation; a later
failure can leave the launched environment unusable. Desktop publication can also
succeed before a cache command fails. The proposed optional managed installation
keeps an independently runnable last-good release until candidate validation and
activation complete. A failed update must leave the launcher pointing at that
release, with a clear recoverable status and no change to user media/credentials.

The lab established atomic pointer replacement, final-path venv construction,
four handled failure/rollback points and concurrent readers. It did not establish
power-loss recovery, Halo runtime checks, asset transactions or data migration.
Those require the acceptance tests below; the prototype is not an installer.

## Installation layout and ownership

Use an opt-in managed profile separate from a developer checkout. Illustrative
layout under the existing absolute XDG data directory:

```text
halo-gtk/managed/
  install.lock
  transaction.json
  releases/<source-id>-<lock-id>-<runtime-id>/
    source/
    .venv/
    release.json
    assets/
  current -> releases/<validated-release>
  previous -> releases/<last-good-release>
```

Release IDs are installer generated, path-safe and collision resistant. Build
each venv at its final absolute path, never rename or relocate a completed venv.
Use a pinned uv version and a hash-checked frozen lock. Record source revision,
lock digest, Python executable/version, system GI/GStreamer versions and health
check results in `release.json`. A release is immutable after validation.

Config, Secret Service credentials, media, Favorites and caches retain their
current locations. They are not copied into releases or included in rollback
snapshots without a separate explicit policy. Relative/empty XDG variables use
the application's current fallback; reject invalid managed paths, foreign-owned
directories and unexpected symlinks in installer-owned staging/manifest paths.
Retain the shell/Desktop Exec quoting and symlink-safe atomic publishing already
present in the installer. File modes and fsync failures are part of the result.

The stable launcher resolves `current` once to a concrete validated release and
executes that release's Python/module by absolute path. Do not perform separate
pointer lookups for interpreter and source. Continue using the release's Python
after activation changes; never garbage-collect releases while an instance or
installer uses them. Prefer a per-release shared lease (`flock`) held across
exec, with an exclusive nonblocking lease required for deletion.

## Transaction and activation

1. Acquire an exclusive installer lock and validate owned paths. Recover any
   recorded interrupted transaction before starting another update. Verify source
   provenance and lock integrity. A developer checkout with local modifications
   must never be reset or overwritten by managed update.
2. Create a uniquely named candidate at its final release path. Persist an atomic
   transaction manifest identifying prior current/previous releases, candidate
   and every installer-owned temporary path. Build dependencies and stage assets
   without altering active release files or user data.
3. Run no-account checks in isolated XDG paths: imports/GI, plugin capabilities,
   schema validation, launcher quoting and a bounded offline application smoke
   test. No Ring login, camera/Alarm command or live microphone is a health check.
   A checked subprocess must have a deadline and its children must be reaped.
4. Request graceful shutdown of the existing owned app and wait for its jobs and
   configuration writes to resolve. If it cannot stop within the stated limit,
   leave the active pointer unchanged and report that activation is deferred;
   do not broadly match or forcibly kill unrelated Python processes.
5. Prepare stable launcher/desktop entries and versioned assets, validate them,
   then activate using same-directory temporary symlinks and `os.replace`.
   Fsync the relevant files/directories and journal each completed phase. Only
   `current` is the launch selector; `previous` and the manifest are recovery
   metadata, not a second independently authoritative activation switch.
6. Run a bounded post-activation offline health check. On handled failure restore
   the old pointer and any owned asset manifest, retain diagnostics, and return
   failure. On success mark committed and retain the previous release. Starting
   the UI is a separate explicit final action, not an unbounded health check.

Atomic rename alone does not make the whole installation transactional or
power-loss durable. On restart inspect the journal and validated release
manifests; choose the last committed healthy release, reconcile incomplete
asset publication and retain the candidate for diagnosis until cleanup is safe.
Define and test each interruption point before claiming crash recovery.

## Assets, data compatibility and rollback

One pointer cannot atomically replace several desktop files, schemas, icons and
cache databases. Keep the stable launcher/desktop interface backward compatible;
prefer immutable versioned asset directories and a journaled, idempotent asset
publisher. Cache regeneration failure should have an explicit warning/recovery
policy instead of falsely claiming the entire release is unusable. Document which
asset changes require session refresh. Verify rollback after each asset failure.

Rollback must verify that the prior release's recorded runtime and data format
remain supported before switching. No application schema migration is included
in this proposal's first implementation. Future data migrations need explicit
versioning, compatibility windows, backups and a separately reviewed rollback
policy; restoring code cannot undo an irreversible shared-data migration.
User recordings/Favorites and credentials must never be deleted by a rollback.

Both releases depend on system Python/GI/GStreamer and shared OS services. An OS
upgrade can break both; report that limitation and the exact failed health check.
Do not claim this design is a container or a complete system rollback.

Uninstall removes only manifest-owned managed releases/assets after releasing
owned app jobs, preserving media/config unless the user explicitly requests their
removal. Reuse literal process matching. Never follow a replaced launcher or
desktop symlink to delete its target. Clean stale candidates only after acquiring
the installer lock, checking ownership and excluding active/leased releases.

## Required tests and rollout gates

- Existing and new installation paths with spaces, quotes, percent signs,
  backslashes, relative/empty XDG values and malicious symlinks.
- Fail dependency resolution/sync, health checks, disk writes/fsync, full disk,
  every desktop publication/cache step and post-activation validation; verify the
  old launcher still runs and settings/media hashes remain unchanged.
- Kill the installer before/after each journal or pointer fsync, restart recovery,
  repeat recovery, and inject competing update/uninstall attempts. Power-loss
  durability requires a filesystem/VM test, not only process kill tests.
- Readers and already running releases during repeated activation; release-lease
  protection against cleanup; blocked graceful stop and unresponsive subprocess
  termination/reaping; no unrelated process affected.
- First managed installation and explicit migration from a checkout, rollback
  through at least two versions, missing/broken system dependencies, compatible
  desktop assets, autostart and complete uninstall ownership verification.

Approval requested after reviewing this design: implement the optional managed
profile, its migration/asset/recovery policy and test suite in a separate change.
Keep production scripts unchanged until that implementation is validated. The
current UI/runtime upgrades do not depend on approval of this installation model.

References: [venv path constraints](https://docs.python.org/3.14/library/venv.html),
[atomic same-filesystem replacement](https://docs.python.org/3.14/library/os.html#os.replace).
