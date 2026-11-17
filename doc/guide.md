# Halo guide

Installation, everyday use, privacy, device support, and maintenance for the
native Linux desktop app. For the product tour, return to the
[Halo README](../README.md).

- [Requirements](#requirements)
- [Installation](#installation)
- [Updates and removal](#updates-and-removal)
- [Using Halo](#using-halo)
- [Device controls and limits](#device-controls-and-limits)
- [Privacy and local data](#privacy-and-local-data)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License and project information](#license-and-project-information)

## Requirements

Halo is a Linux desktop client that connects directly to Ring. It does not
require Halo Server, Homebridge, or Scrypted. It needs an internet connection
and a Ring account with access to the devices you want to use. Recording
availability and control permissions depend on the device, account, region,
subscription, and Ring service behavior.

The project is currently version **0.1.0 Alpha**. Ring's interfaces are private
and can change. Keep the official Ring app or keypad available for setup,
account management, and critical security workflows. Halo is not an emergency
monitoring service.

### Native desktop runtime

| Component | Requirement |
| --- | --- |
| Platform | Linux; current native qualification uses Arch Linux |
| Python | 3.11 through 3.14 |
| Dependency manager | Exactly `uv 0.11.24` |
| Interface | GTK 4 and libadwaita 1.5 or newer |
| GObject bindings | Distribution-provided PyGObject, GdkPixbuf, GioUnix, Graphene, libnotify, and libsecret |
| Media | GStreamer 1.20 or newer, common codecs, and `gtk4paintablesink` |
| Audio output | PulseAudio or PipeWire-compatible output |
| Microphone | A PulseAudio-compatible source for the current talkback path |
| Credentials | A desktop Secret Service provider, such as GNOME Keyring or KeePassXC with Secret Service enabled |
| Source installation | Git and the native runtime above |

Install GObject introspection libraries through your distribution. Python
packages alone cannot supply the complete native runtime. Package names vary;
a distribution with an older libadwaita needs a supported newer package source
or distribution release. The installer checks required namespaces and media
elements before publishing desktop integration.

Halo's media compatibility checks currently target `ring-doorbell 0.9.14`,
`aiortc 1.15.0`, and `PyAV 17.1.0`. Install the committed lockfile rather than
upgrading these packages independently; Halo has version-specific integration
contracts with them.

The native qualification environment and separate bundled-runtime evidence are
recorded in the [desktop baseline](desktop-baseline-2026-11-16.md) and
[Flatpak guide](flatpak-packaging.md). Other Linux distributions are not
automatically qualified by those checks. There is no native Windows or macOS
installer in this project.

## Installation

Choose one installation profile:

| Profile | Best fit | Important behavior |
| --- | --- | --- |
| Checkout installation | Straightforward current-user installation | The launcher uses the checkout's `.venv`; keep the checkout at the same path. |
| Managed installation | Staged updates and local code rollback | Each release has its own environment; activation waits for affected Halo instances to close. |
| Experimental Flatpak | Trying the bundled GNOME 50 runtime | Local x86_64 build, separate sandbox data, and no host login-startup integration. |

### Arch Linux prerequisites

Install the native dependencies:

```sh
sudo pacman -S git curl python-gobject gtk4 libadwaita libnotify libsecret \
  gstreamer gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly \
  gst-libav gst-plugin-gtk4 gst-plugin-pipewire
```

Install the pinned uv release with its official standalone installer. You can
inspect the installer URL before running it:

```sh
curl -LsSf https://astral.sh/uv/0.11.24/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

The reported version must be `0.11.24`. The project pin keeps dependency and
artifact behavior consistent with the lockfile.

### Checkout installation

```sh
git clone https://github.com/JamesFromFL/halo-gtk
cd halo-gtk
./scripts/install.sh
halo-gtk
```

Run the installer as your normal user. It checks the lockfile, stops a matching
Halo instance launched from this checkout, creates or reuses `.venv` with
system site-packages, and installs frozen production dependencies. It then
validates the native runtime and installs the launcher, desktop entry, icons,
and GSettings schema in your user directories.

This profile updates the environment in place; it does not provide managed
rollback. Keep the checkout where it was installed. If you move it, rerun its
installer to update the launcher. If your shell cannot find `halo-gtk`, use
`$HOME/.local/bin/halo-gtk` or add `$HOME/.local/bin` to your `PATH`.

### Managed installation

From a local checkout with the same prerequisites:

```sh
./scripts/install.sh --managed
```

To migrate an existing checkout launcher, first close Halo, then explicitly
permit the migration:

```sh
./scripts/install.sh --managed --replace-checkout
```

The managed installer stages the selected **committed local revision** in its
final release directory, installs frozen dependencies, and validates the
runtime before activation. Uncommitted and untracked files are not packaged.
The original checkout launcher and desktop entry are backed up during an
explicit migration. Managed installation does not pull Git changes, stop
applications, or contact Ring.

The default release root is `~/.local/share/halo-gtk/managed`. You still launch
the app with `halo-gtk`. Each release needs space for its source, environment,
assets, logs, and dependency cache. See the
[managed installation guide](staged-install-design.md) for staging, recovery,
custom paths, process leases, and the precise rollback boundary.

### Experimental Flatpak

The current package is a local **x86_64 GNOME 50** candidate using Python 3.13,
not a Flathub release. Install Flatpak, flatpak-builder, and the GNOME 50 SDK and
runtime through your distribution's supported tools. From the checkout:

```sh
flatpak-builder --arch=x86_64 --force-clean --repo=flatpak-repo \
  flatpak-build packaging/flatpak/io.github.JamesFromFL.HaloGtk.json
flatpak install --user ./flatpak-repo io.github.JamesFromFL.HaloGtk
flatpak run io.github.JamesFromFL.HaloGtk
```

The manifest uses the current source checkout, including uncommitted files;
build from a reviewed commit when distributing it. Flatpak uses separate
private XDG paths for settings, internal media, and cache; it does not migrate
those files from a native installation. Its credentials are not necessarily
separate: when the desktop provider permits access, both profiles use the same
Halo Secret Service entries. Sign-out or token changes can affect another
profile using those entries. Actual keyring prompting depends on the provider.
The host login-startup option is unavailable in this candidate. For sandbox
permissions, qualification results, isolated test
installation, and dependency-source regeneration, see
[Flatpak packaging](flatpak-packaging.md).

## Updates and removal

### Checkout profile

From the original checkout:

```sh
./scripts/update.sh
```

The script requires a clean worktree, pulls with fast-forward only, and reruns
the installer. Commit or stash work you want to keep before updating; do not
discard local changes just to bypass the check. Close active viewing sessions
before an update because the default installer stops the matching checkout app.

To remove its desktop integration:

```sh
./scripts/uninstall.sh
```

This removes the user-local launcher, desktop entry, icons, autostart entry,
and settings schema. It preserves the checkout, `.venv`, Ring session, settings,
cache, logs, Favorites, local history, and saved media. It is not a data-erasure
command.

### Managed profile

Advance your source checkout separately when you want a newer remote revision.
For example, on a clean checkout use `git pull --ff-only`, then:

```sh
./scripts/update.sh --managed
python3 scripts/managed_install.py status
```

Managed update stages and activates the checkout's committed revision; it never
fetches or includes uncommitted changes. If activation is deferred because Halo
is running, close it normally and activate the release identifier shown by
`status`:

```sh
python3 scripts/managed_install.py activate RELEASE_ID
```

To switch to the previous qualified release or uninstall managed integration:

```sh
python3 scripts/managed_install.py rollback
./scripts/uninstall.sh --managed
```

Rollback revalidates the previous runtime and switches code. It does not undo
OS library changes or restore shared user data. Uninstall restores the original
checkout launcher and desktop entry when migration saved them, otherwise it
removes managed integration. Owned release directories are removed; user data,
credentials, the private dependency cache, and recovery infrastructure are
preserved as described in the [managed guide](staged-install-design.md).

### Flatpak profile

Rebuild the reviewed source into the same `flatpak-repo` used for installation,
then update the installed app from its configured local repository:

```sh
flatpak update --user io.github.JamesFromFL.HaloGtk
```

To remove the user-installed application:

```sh
flatpak uninstall --user io.github.JamesFromFL.HaloGtk
```

This command does not request deletion of the application's sandbox data.
Native checkout and managed uninstall scripts do not manage Flatpak installs.

## Using Halo

Sign in through Halo's local account interface and complete any Ring
verification there. Do not put passwords, MFA codes, session tokens, or private
diagnostic material into an issue report.

The five main views are **Overview**, **Cameras**, **Activity**, **Devices**,
and **Security**. Navigation appears in the header on wide windows and a bottom
bar on narrow ones.

| View | What to use it for |
| --- | --- |
| Overview | Reported security state, camera snapshots with freshness labels, and recent events received while the view is open. Continue to Activity for history. |
| Cameras | A selected-camera monitoring wall, reusable layouts, camera filtering, and device health. Start or stop the wall together. |
| Activity | Filter camera events, review available recordings, or open the separate Alarm history view. |
| Devices | Browse cameras, Chimes, lighting, and recognized Intercoms; open supported controls and reported device details. |
| Security | Select a Ring location to review reported Alarm modes, transitions, sensor supervision, and health. |

### Live cameras

The camera wall offers Small, Medium, and Large densities, with Balanced
`4 / 2 / 1` or Dense `5 / 3 / 2` grid behavior. These describe layout columns,
not additional streaming capacity. Halo allows four simultaneous streams by
default; the six-stream setting in Advanced is experimental and reliability
depends on Ring, the devices, and your network.

Open focused live view for one camera. It shows an available still while the
stream connects, then offers volume, two-way talk, screenshots, and zoom/pan
from 100% to 250%. Available light and siren controls depend on the device's
reported capabilities. Listening and talkback also require current Ring audio
permission. The wall and focused view reuse compatible sessions where possible.

### Recordings and local media

Activity provides recording playback with previous/next navigation, scrubbing,
volume, fullscreen, and screenshots. Favorites, sharing, downloads, and deletion
are available where Ring supplies the recording and the account can perform
the action. Local Favorites retain selected recordings on this computer;
changing Ring's current camera privacy does not erase historical recordings or
files you already saved.

Recording previews use verified HTTPS and a bounded child process for download,
decode, and conversion. Preview inputs are limited to 8 MiB of recording body
data and 4 MiB of image data; output is at most 1280×720 PNG. Slow, oversized,
unsupported, or canceled previews fall back to an available bounded snapshot.
Full recording saves have a separate 512 MiB limit and one-hour job budget.
Save jobs can continue across navigation and offer cancellation. These process
limits are not a complete security sandbox.

### Settings and notifications

Settings categories are **General**, **Live View**, **Events**, **Storage**,
**Account**, and **Advanced**. Appearance can follow the system or use Light or
Dark, and the choice is saved locally. Native installations support optional
background behavior and login startup; desktop tray and notification behavior
also depend on your desktop environment.

Under **Events → Notification Rules → Configure**, choose quiet hours, a timed
snooze, and per-device rules. Quiet hours use this computer's local clock.
Enabled critical Alarm alerts bypass routine quiet-hour, snooze, and device
rules, but still respect the master notification and Alarm-alert switches.
Desktop policies may suppress them further. Rules affect Halo notifications,
not Ring recording, motion detection, lights, or Alarm modes. See
[notification rules](notification-rules.md) for exact behavior.

## Device controls and limits

Halo distinguishes a requested action, Ring accepting it, and a later reported
state. An accepted request is not proof of a physical change. A timeout or loss
of confirmation can leave an uncertain outcome even when Ring received the
command. Closing a view or signing out cannot recall a command already sent.

The expanded device controls below have synthetic protocol and lifecycle
coverage; that does not establish live compatibility with every model or
account. Read the focused guide before relying on a control.

| Device or workflow | Available behavior and important limits | Details |
| --- | --- | --- |
| Camera lights | Explicit On/Off requests with fresh reported outcomes. Native motion-light duration is read-only. There is no indefinite manual hold or repeating On keepalive. | [Camera lighting](camera-lighting.md) |
| Light Groups | Existing groups by Ring location, aggregate reports, members, and explicit 1-, 5-, or 15-minute On requests plus Off. Group reports do not prove every lamp's state; Off does not disable motion or schedules. Group creation/editing and schedules stay in Ring. | [Light Groups](light-groups-support.md) |
| Individual Beams lights | Qualified bridge-backed models have finite On choices and return-to-automatic behavior. Automatic is not forced Off. Controls default disabled; no permanent hold or brightness control is inferred from model names. | [Beams](beams-support.md) |
| Chimes | Qualified legacy Chime/Chime Pro models have volume Apply, explicit Ring/Motion sound tests, snooze/resume, and an automatic-nightlight setting when reported. After a sound request, confirm that the sound stopped before another setting or test. Unknown models and ownership stay read-only. | [Chimes](chime-support.md) |
| Alarm keypads | Qualified brightness Apply, a local control lock, and observed proximity. Controls start disabled and locked. The lock blocks Halo writes; it does not hold a brightness against other apps or users. Proximity expiry means no recent observed event, not a reported clear state. | [Keypads](keypad-support.md) |
| Intercom Audio and Video | Reported details, incoming call/unlock observations, and explicit entrance release with expiring confirmation. Acceptance does not prove opening or relocking. Audio-only calling, guest keys, and setup stay in Ring. | [Intercom](intercom-support.md) |
| Intercom Video | Qualified Video models offer explicit experimental Start/End, muted-by-default listening, and hold-to-talk. Starting can open or answer the media path. No background recording, automatic media start, or session renewal is provided; each session is capped at five minutes. | [Intercom Video](intercom-support.md#experimental-intercom-video) |

Camera, group, and Beams lighting actions share a bounded reservation: a busy
request is rejected rather than queued. Reported timers and timed requests still
need target-hardware validation; Halo does not claim a physical lamp switched
off merely because a requested interval elapsed.

### Ring Alarm

Security shares the authenticated Ring session with cameras and shows reported
modes, transitions, sensor supervision, and location health. Offline sensor
values remain last-known even if the hub is online. Reported hub battery power
and siren state are read-only.

Disarmed, Home, and Away requests require fresh inventory and discovery
evidence, time-limited confirmation, and a separate review of supported faulted
sensors to bypass. Halo sends once and waits for an explicit panel update.
Unknown authorization, active alarm/life-safety conditions, cellular backup,
and incomplete status remain read-only. Current shared-role permission is not
guessed from camera access.

Alarm integration uses undocumented Ring CLAP interfaces and has not been
validated against live Alarm hardware. Panic/dispatch, Alarm-siren commands,
generic lock/switch/valve controls beyond qualified Beams actions, access codes,
and other Alarm configuration are outside these controls. Keep the official
Ring app or keypad available for critical response. See
[Alarm implementation and acceptance notes](alarm-implementation-2026-10-26.md).

Recognized current Alarm signals can produce desktop alerts while Halo runs.
Routine mode, sensor, and health notifications are opt-in; details are off by
default because names can appear on the lock screen. Outages leave observation
gaps. These alerts do not replace Ring's monitoring or emergency notifications.

### Alarm history

Activity has a separate experimental **Alarm history** view. Ring cloud reads
occur on explicit request, show retrieval time and partial coverage, and stop
when pagination repeats or overlaps. The history is not guaranteed complete.
See [cloud coverage](alarm-history-coverage.md).

Open **Local history settings** from that view to retain copies of cloud events
you explicitly retrieve. The default is **Off**; choices are 7 or 30 days after
retrieval. **Saved locally** reads those copies without a new network request.
It requires the current account and a successful location-specific cloud
history read within five minutes. If permission verification expires, choose
**Ring cloud → Refresh**, then return to Saved locally.

Turning retention Off stops new collection and preserves existing copies until
their original expiry. **Clear saved history** removes copies for the current
account now. Expiry cleanup runs when local history is next accessed, not on an
independent timer. Old unbound sessions can continue using live Alarm but need
a fresh sign-in with Secret Service available before local history is enabled.
Saved copies are neither an offline authorization system nor a complete Alarm
archive. See [local history](local-history-design.md) for account binding,
storage bounds, cleanup, and older-version compatibility.

## Privacy and local data

Halo is a Ring cloud client, not an offline or cloud-free security system. It
uses Ring services and Google Firebase Cloud Messaging for live Ring events.
Its settings and saved media are local; the authenticated Ring session is
stored through desktop Secret Service.

### Current camera privacy

Halo's current-media privacy protection is always enabled. A fresh Ring read
must explicitly allow live video. Camera previews and notification images also
require motion detection enabled; listening and talkback require Ring's audio
setting enabled. Unknown or stale settings do not grant media access.

Permission receipts expire after at most 120 seconds, including computer
suspend. Revocation clears current imagery and queued media, stops microphone
samples, and retires the stream. Historical recordings and saved files remain
separate. During live viewing, previews reuse a qualified live frame or Ring's
cached snapshot rather than requesting a new capture. See
[camera privacy](camera-privacy.md) for setting names, freshness checks, and
revocation behavior.

### Default paths

These are native-installation defaults. Absolute `XDG_CONFIG_HOME`,
`XDG_DATA_HOME`, `XDG_CACHE_HOME`, and `XDG_STATE_HOME` values override their
respective roots; empty or relative values use the defaults.

| Data | Default path |
| --- | --- |
| Settings and notification preferences | `~/.config/halo-gtk/settings.json` |
| Monitoring layouts | `~/.config/halo-gtk/live-monitoring-layouts.json` |
| Camera nicknames | `~/.config/halo-gtk/device-nicknames.json` |
| Local keypad permissions and brightness lock | `~/.config/halo-gtk/keypad-settings.json` |
| Local Beams permissions and default durations | `~/.config/halo-gtk/beams-settings.json` |
| Favorites | `~/.local/share/halo-gtk/favorites/` |
| Preview cache | `~/.cache/halo-gtk/` |
| Logs | `~/.local/state/halo-gtk/halo-gtk.log` and rotated files |
| Optional saved Alarm history | `~/.local/state/halo-gtk/alarm-history/` |
| Managed releases, if selected | `~/.local/share/halo-gtk/managed/` |
| Screenshots | `~/Pictures/halo-gtk` |
| Recording downloads | `~/Videos/halo-gtk` |
| Ring session and history account binding | Desktop Secret Service; not a plaintext token file |

Screenshot and recording folders can be changed in Settings. Flatpak keeps
internal files in its private XDG directories and grants narrow access to the
default media folders; other directory selection uses the desktop file portal.
Its Secret Service schema and account attributes are shared with native Halo,
subject to the desktop provider's access policy; sandboxed file storage does not
isolate those credentials.

Logs rotate at approximately 1 MB with five backups. Diagnostic exports are
redacted before packaging, but review an archive before sharing it. Notification
images and descriptions can reveal camera activity on a lock screen; their
content is configurable. Reset App Settings preserves the Ring session and
saved media. Uninstalling desktop integration also preserves local data.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| `halo-gtk` is not found | Add `$HOME/.local/bin` to `PATH`, or run `$HOME/.local/bin/halo-gtk`. |
| Launcher fails after moving a checkout | Rerun that checkout's installer. The default launcher points to its absolute `.venv` path. |
| Missing `gi`, GTK namespace, libadwaita, or media element | Install the distribution's native package named by the installer. The environment needs system site-packages; a pip-only reinstall cannot supply GTK/GStreamer. |
| Native runtime breaks after an OS Python upgrade | Recreate/qualify the environment using the matching distribution Python and pinned lock. Managed rollback cannot undo system-library changes. Preserve the known-good release and inspect the reported failed check first. |
| Update refuses local changes | Review `git status`; commit or stash work you want to retain before the checkout updater runs. |
| Managed activation or uninstall is deferred | Close affected Halo instances normally, then retry. Use managed `status` to inspect staged releases or recovery information. |
| Sign-in cannot be restored | Check that a Secret Service provider is available and unlocked, then use Halo's account interface. Keep credentials and verification codes out of logs and issue reports. |
| Camera preview or audio is unavailable | Check the permission/freshness message and the relevant Ring live-view, motion, and audio settings. Old inventory does not grant access. Reduce concurrent streams when diagnosing connection quality. |
| Recording preview is absent | Ring may not supply an accessible recording; size, time, or codec limits can also trigger snapshot fallback. This does not by itself establish camera failure. |
| Device control is disabled or unconfirmed | Review model, ownership, freshness, and connection information in its focused guide. Refresh reported state; acceptance and physical success are different. Do not repeatedly resend uncertain actuator actions to test them. |
| Saved Alarm history is unavailable | Establish an account-bound sign-in if requested, then use Ring cloud → Refresh for the location. Saved reads do not silently renew permission. |
| Desktop alerts are missing | Check Halo's master/type switches, notification rules, background operation, and the desktop's notification policy. No alert is proof that no Ring event occurred. |

For a useful bug report, include Halo's revision/version, distribution and
installation profile, steps to reproduce, the exact visible error, and whether
the official Ring app behaves the same way. Export logs from
**Settings → Advanced → Export Debug Logs**, review them, and attach only the
relevant redacted material to an [issue](https://github.com/JamesFromFL/halo-gtk/issues).
Do not include passwords, MFA codes, tokens, private camera footage, or raw
account responses. Do not trigger alarms, sirens, audible tests, or entrance
release just to gather a diagnostic report.

## Development

Use the native dependencies above and the pinned uv release. Choose a supported
distribution Python that can import its installed GObject bindings:

```sh
uv venv --python "$(command -v python3)" --system-site-packages
uv lock --check
uv sync --frozen --no-group audit
.venv/bin/halo-gtk
```

The final command launches the application and can use your normal account
configuration. For automated qualification, isolate HOME/XDG directories and
use synthetic clients and media instead of the installed account.

Run the normal source checks:

```sh
.venv/bin/ruff check --no-cache src tests scripts
.venv/bin/ruff format --check --no-cache src tests scripts
.venv/bin/pytest -q -p no:cacheprovider
.venv/bin/python scripts/ci_import_check.py
.venv/bin/python scripts/flatpak_dependencies.py --check
glib-compile-schemas --strict --dry-run data
desktop-file-validate data/io.github.JamesFromFL.HaloGtk.desktop
uv build --out-dir dist
```

Native layout and generated-media tests are opt-in with
`HALO_NATIVE_LAYOUT_TESTS=1` and `HALO_NATIVE_MEDIA_TESTS=1` on an isolated display.
They deliberately skip in a normal headless run. See the
[desktop baseline](desktop-baseline-2026-11-16.md#reproduce-the-native-checks) for
the Broadway setup pattern. Local fixture success does not establish real Ring
login, physical sound quality, model-specific actuation, desktop portals, or
long-running live-service behavior.

CI checks formatting, tests, lock consistency, dependency advisories, generated
Flatpak inputs, package imports, schema/desktop validity, and installed-wheel
compatibility. A wheel includes Python code and its bundled icon; it does not
install the native desktop entry, GSettings schema, or hicolor assets. Use a
desktop installer for normal application installation.

For dependency work, read [security maintenance](../SECURITY.md) before changing
pins or audit policy. The policy retains raw results and permits only the exact
reviewed OAuthlib authorization-server advisory families for the pinned
version; it does not broadly ignore that package. Private SDK/media patches
need compatibility tests before their version pins change.

Further engineering references:

- [Current implementation and qualification report](backlog-completion-2026-11-16.md)
- [Interface design](gui-redesign-2026-11-16.md)
- [Receive-path reliability](receive-path-review-2026-11-16.md)
- [Integration evidence and hardware gates](backlog-research-gates-2026-11-16.md)
- [Improvement backlog](improvement-backlog.md)
- [Original audit](audit-2026-10-25.md)
- [Changelog](../CHANGELOG.md)

## License and project information

Halo is licensed under **GPL-3.0-or-later**; see [LICENSE](../LICENSE).

Halo is an independent, unofficial open-source project. It is not affiliated
with, endorsed by, or supported by Ring or Amazon. Ring and Amazon are trademarks
of their respective owners, used here to describe compatibility.
