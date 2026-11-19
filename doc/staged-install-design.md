# Managed installation, updates and rollback

Halo supports an optional managed profile that validates a new release before
changing the desktop launcher. The existing checkout installer remains the default.
Managed installation never pulls, resets, stashes or changes a Git checkout, and
never stops an application or sends commands to Ring devices.

## Commands

Run these from a local Halo checkout. `uv 0.11.24`, Git, a compatible system Python,
and Halo's native GTK/GStreamer dependencies must already be installed.

```sh
# Stage the committed HEAD and activate it after validation.
scripts/install.sh --managed

# Existing checkout installation: explicitly authorize replacing its launcher
# and desktop entry. Close that app first. Exact original files are backed up.
scripts/install.sh --managed --replace-checkout

# Stage a local commit without activating it.
python3 scripts/managed_install.py stage --ref HEAD

# Review staged/active releases, then activate a reported release identifier.
python3 scripts/managed_install.py status
python3 scripts/managed_install.py activate RELEASE_ID

# Build the checkout's current committed HEAD and activate it.
scripts/update.sh --managed

# Revalidate and switch to the previous release; repeating switches back.
python3 scripts/managed_install.py rollback

# Remove unused staged builds, keeping the current and previous releases and running copies.
python3 scripts/managed_install.py prune

# Restore backed-up checkout integration, or remove managed integration.
scripts/uninstall.sh --managed
```

`stage`, `install` and `update` accept `--repository PATH`, `--ref LOCAL_REF`,
`--python PATH` and `--uv PATH`. References must resolve to existing local commits.
A dirty checkout is left alone; uncommitted and untracked files are not packaged.
Fetch or advance the checkout separately when a newer remote revision is wanted.
No fetch occurs during staging, activation or rollback. The generated launcher
uses the internal `launch -- ARGS...` command and forwards arguments literally.

The default root is `$XDG_DATA_HOME/halo-gtk/managed`, falling back to
`~/.local/share/halo-gtk/managed` when the XDG value is empty or relative. Direct
commands also accept `--root ABSOLUTE_DATA_HOME/halo-gtk/managed` **before** the
subcommand. The normal `~/.local/bin/halo-gtk` launcher remains the entry point,
including for Halo's existing login-autostart preference.

## Release layout and validation

```text
halo-gtk/managed/
  install.lock
  installation.json             original and published desktop files
  transaction.json              present only during an incomplete transaction
  manager-<digest>.py            versioned launcher bootstrap
  build-cache/                  private uv dependency cache
  releases/<commit>-<lock>-<nonce>/
    candidate.json              ownership marker, including failed candidates
    source/                     archive of the selected committed revision
    .venv/                      created here, never relocated
    assets/schemas/              private compiled GSettings schema
    assets/icon.png              release icon
    release.json                successful qualification and format metadata
    logs/                       build and health command output
    use.lock                    running-release lease
  current -> releases/<active>
  previous -> releases/<last-good>
```

The installer rejects unexpected symlinks, foreign-owned managed boundaries,
unsafe release identifiers and malformed transaction metadata. The managed root
is private. Source archives cannot contain links or paths escaping the release,
and have a 256 MiB extraction limit. Each release records its commit, SHA-256 lock
digest, uv version, Python and native runtime versions, validation result and
local data-format compatibility marker. Activation verifies the lock digest.

Staging checks the pinned uv version and lock consistency, creates the environment
at its final absolute path with system site-packages, then performs a frozen,
noneditable production install. It compiles the release-local schema with strict
validation. Native qualification checks the required GI namespaces, libadwaita
minimum, media plugins, all application imports and the installed CLI help path.
Health checks use temporary HOME/XDG directories, an in-memory GSettings backend
and an unavailable session bus. They do not log in, contact Ring, instantiate
media capture elements or read the user's Secret Service credentials.

Build commands have deadlines up to ten minutes; native checks have 45/20-second
deadlines. Output goes to files, avoiding a pipe held open by a descendant.
Timeout cleanup signals only that subprocess's new process group. Logs are
bounded by command time, **not by a byte quota**; release logs and failed candidates
are retained for diagnosis. Staging needs enough disk space for source, a complete
second environment and the dependency cache.

## Activation and failure recovery

A staging lock serializes installer operations. A separate activation lock protects desktop publication and the handoff to a running release. The active application can launch while dependencies for another release are being built. Matching builds are reused after their interpreter identity, dependency lock and runtime health are checked. A managed launch resolves
`current` once, validates that release, acquires its shared lease and then executes
its concrete Python and package. Activation and uninstall require exclusive
leases. If Halo is running from an affected release, activation is deferred; the
candidate remains staged and the launcher still selects the old release. Close
Halo normally and retry `activate`. No process is stopped by the installer.

Explicit checkout migration also checks literal owned launcher/executable process
arguments and defers when a known checkout instance is running. Custom launchers
may not expose that executable, and a checkout has no managed lease: close all
checkout instances before migration and do not start another during it. No broad
process-name matching or forced shutdown is performed.

The activation journal records the old pointers and exact desktop-file content
and modes before publication. The launcher and desktop entry are atomically
replaced, then `previous` and `current` are switched. A second isolated health
check must succeed before the journal is committed. Handled failures restore the
old selection and original published files. Existing files require explicit
`--replace-checkout` on first installation; later outside edits are preserved and
cause a clear failure instead of being silently overwritten.

`status`, staging and other modifying commands first recover an interrupted
transaction. An uncommitted transaction restores its prior state; a committed
transaction retains the new release. Recovery is idempotent. A partial uninstall
keeps its journal until owned-release cleanup finishes. If a desktop file was
changed outside the transaction, recovery stops without overwriting that change.
Malformed journals also fail closed. The launcher refuses an unresolved journal
and explains how to run recovery.

Atomic replacement and fsync are used for publication, but this is not a claim of
power-loss durability across every filesystem. Process interruption at each
publication phase is tested; power-cut/VM filesystem testing remains a separate
qualification. Both releases still rely on system Python, GTK, GStreamer and
shared OS services. An OS upgrade can break both, and rollback reports a failed
native check instead of promising to undo OS changes.

## Assets, data and uninstall

Schemas and icons stay inside each immutable release. The launcher sets
`GSETTINGS_SCHEMA_DIR`; the desktop icon follows `current`. No shared icon/schema
cache or other application's desktop assets are rewritten. A desktop shell may
need to refresh an already displayed icon after activation.

Settings, Secret Service credentials, recordings, Favorites and caches remain in
their existing locations. They are not copied into releases, changed by staging,
or restored by rollback. This first format performs **no data migration**. Future
incompatible data migrations require a separately versioned rollback policy;
restoring code cannot undo a shared-data migration.

Uninstall refuses active leases or externally modified desktop files. It restores
exact original checkout launcher/desktop files when migration saved them;
otherwise it removes managed integration. A newly created autostart file is
removed only when it exactly matches Halo's generated entry for this launcher,
did not predate managed installation, and no original launcher is being restored.
Custom or pre-existing autostart entries are preserved. Only release directories
with installer ownership markers are deleted. User data and credentials remain.
The private lock, versioned bootstraps and dependency cache remain to preserve
concurrent-operation safety and allow a later reinstall. Use the explicit `prune` command to reclaim unused release directories. The plain install, update and uninstall scripts detect managed ownership and print the command for that installation before changing any files.

## Qualification

Synthetic tests use temporary Git repositories and fake runtimes: committed-ref
selection, normal/failed staging, exact migration backups, special-path argument
quoting, two-way rollback, managed/check-out running-process deferral, concurrent
locks, malformed journal rejection, disk/fsync failure, post-activation failure,
changed-file protection, source/path symlinks, autostart ownership and interrupted
uninstall cleanup. Actual subprocess exit at every publication checkpoint proves
that subsequent recovery is idempotent. A timeout test verifies the command's
process group stops while an unrelated synthetic process remains alive.

The first real qualification used committed `15fe841` in an isolated temporary
HOME, system Python, pinned uv and the already verified dependency cache. Frozen
installation, native capability discovery, offline entry-point smoke, two
activations and rollback passed. Real uninstall also removed the managed
integration, then a second migration/uninstall restored exact synthetic checkout
launcher and desktop contents and modes while preserving settings. This
demonstrates the local Arch runtime; it is not an assertion that all Linux
distributions or future system upgrades are qualified. No real desktop
installation, active app, Ring account or device was changed.

References: [venv path constraints](https://docs.python.org/3.14/library/venv.html),
[atomic same-filesystem replacement](https://docs.python.org/3.14/library/os.html#os.replace).
