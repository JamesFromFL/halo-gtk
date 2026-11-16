# Flatpak packaging qualification

The first packaging target is x86_64 GNOME 50, using its Python 3.13 runtime.
This gives the application a consistent GTK/GStreamer stack without changing
the host's Python installation. It is a local development package, not a Flathub
submission or a promise of support for every desktop or architecture.

## Design and boundaries

- Reuse the runtime's GTK, libadwaita, PyGObject, Secret Service binding and
  GStreamer, including gtk4paintablesink. Bundle the application's locked Python
  dependencies. Prefer hash-pinned compatible wheels; the pure Python http-ece
  dependency is built from its locked source archive using the SDK tools.
- Resolve dependencies from the committed uv lock and export. Do not resolve
  newer versions during the Flatpak build. Build commands run without network.
- Grant network for Ring, PulseAudio for listening and deliberate microphone
  use, GPU rendering, and normal Wayland/X11 display access. Allow Secret Service,
  notifications and the existing status notifier interface. Do not grant host
  filesystem or unrestricted session-bus access.
- Settings and internal media use the Flatpak's private XDG paths. The default
  Pictures/halo-gtk and Videos/halo-gtk save directories have narrow access;
  explicit directory selection uses GTK's desktop file portal.
- The existing native login-startup writer cannot configure the host from a
  sandbox. Disable that control in this build until a Background portal
  implementation has been qualified. Do not silently report an internal sandbox
  desktop file as successful host startup configuration.
- Keep the checkout and managed installers. Do not install the development
  package into the user's normal Flatpak installation or migrate credentials.

## Build and update the candidate

Install Flatpak, flatpak-builder and the GNOME 50 SDK/runtime through your
distribution's supported tools. From the checkout, build the local repository:

```sh
flatpak-builder --arch=x86_64 --force-clean --repo=flatpak-repo \
  flatpak-build packaging/flatpak/io.github.JamesFromFL.HaloGtk.json
flatpak install --user ./flatpak-repo io.github.JamesFromFL.HaloGtk
flatpak run io.github.JamesFromFL.HaloGtk
```

The source manifest consumes this checkout, including uncommitted files. Build
from a reviewed commit when distributing an artifact. For isolated qualification,
set `FLATPAK_USER_DIR` to a disposable installation for **every** SDK, build,
install and run command. The normal user installation is not used by this pass.

Dependency source lists are generated offline with the repository's development
environment and pinned uv 0.11.24:

```sh
python scripts/flatpak_dependencies.py
python scripts/flatpak_dependencies.py --check
```

Regenerate after a deliberate lockfile change, review the artifact choices, and
repeat runtime qualification. CI rejects drift between the lock and generated
sources. Python 3.13 and x86_64 are checked explicitly at build time. The build
includes app distribution metadata and installs all locked dependencies under
`/app`, even when the SDK happens to contain another copy.

## Runtime evidence

The installed GNOME 50 runtime commit
`4337a389125f814785bd83f4ace01da5b6baa80fdcf5a9c81e60dcd03896c067`
reports Python 3.13.16, glibc 2.42, GTK 4.22, libadwaita 1.9 and GStreamer 1.26.11.
Factory discovery found gtk4paintablesink, playbin, appsrc/appsink,
PulseAudio source/sink, H.264/AAC decoders and Opus decoder.

The completed build was installed only in the task-local installation and run
against **Platform**, not the build SDK. All 93 application modules imported,
version metadata reported 0.1.0, the schema was available, and all 43 selected
Python distributions (runtime plus build tools) were verified under `/app`.
The packaged command also passed its `--help` smoke check.

The expanded runtime suite passed 198 checks: generated H.264/AAC playback
through the actual GTK paintable, pause/resume, frame capture, end of stream and
cleanup; synthetic WebRTC media/renewal/talkback; narrow/wide native layouts;
qualified device controls; Intercom lifecycle; and local-history account/storage
behavior. The local packaging, startup and settings checks passed as well.

Qualification uses an isolated Broadway display, fake audio and no Ring network
access. GNOME's image loader needs `org.freedesktop.portal.Flatpak` to launch its
own sandbox. The expanded run enables the filtered session bus for that broker,
retains the sandbox's normal `XDG_RUNTIME_DIR`, and explicitly denies Secret
Service and the separate host-command `org.freedesktop.Flatpak` service. Other
HOME/XDG locations use temporary directories. Normal layout tests require a real
decoded packaged logo. An additional run with no session bus passed the
failure-injection startup/navigation check: a failed logo decoder produces a
symbolic icon without preventing Halo from opening.

These checks do not prove real Ring sessions, microphone/speaker quality, portal
directory grants, credential prompts, notifications, tray behavior or support
across other distributions. Host login startup is deliberately unavailable in
this sandbox candidate until its Background portal flow is implemented and
tested. The native installers retain their startup support.

The build SDK is installed in a separate task-local installation. No system
packages, normal Flatpak applications, Ring credentials or real media devices
are modified by package qualification.

Primary references: [Flatpak dependencies](https://docs.flatpak.org/en/latest/dependencies.html),
[build manifests](https://docs.flatpak.org/en/latest/manifests.html),
[sandbox permissions](https://docs.flatpak.org/en/latest/sandbox-permissions.html),
[Python source selection](https://github.com/flatpak/flatpak-builder-tools/blob/master/pip/readme.md).
