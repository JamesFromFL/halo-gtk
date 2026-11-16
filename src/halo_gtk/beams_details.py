"""Individual Beams reports and explicit finite lighting controls."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")
from gi.repository import Adw, Gtk  # noqa: E402

from halo_gtk.alarm.beams import ON_DURATIONS, BeamsIntent  # noqa: E402
from halo_gtk.alarm.models import TriState  # noqa: E402
from halo_gtk.alarm.provider import AlarmRefreshStatus  # noqa: E402
from halo_gtk.beams_preferences import BeamsPreference, preferences  # noqa: E402
from halo_gtk.device_view_session import DeviceViewSession  # noqa: E402
from halo_gtk.ring_client import get_cached_account_email, get_client  # noqa: E402


class BeamsViewSession(DeviceViewSession):
    snapshot_method = "get_beams_snapshot"
    add_method = "add_alarm_callback"
    remove_method = "remove_alarm_callback"
    cancel_method = "cancel_beams_intent"
    snapshot_freshness = staticmethod(
        lambda snapshot: tuple(
            (light.identity, light.reason, light.reported_on) for light in snapshot.lights
        )
    )


def reported_text(light):
    return {TriState.ACTIVE: "Reported On", TriState.INACTIVE: "Reported Off"}.get(
        light.reported_on, "Light state unknown"
    )


def reason_text(reason):
    return {
        "model-unsupported": "This bridge or light model is read only in Halo.",
        "owner-unavailable": "Fresh ownership of this bridge is not established. Refresh status.",
        "light-unavailable": "The bridge or light is offline, stale, or incompletely reported.",
        "session-unavailable": "Sign in again to load this light.",
        "refresh-required": "Refresh status before changing this light.",
    }.get(reason, "")


def refresh_text(result):
    return {
        AlarmRefreshStatus.REFRESHED: (
            "Status refreshed. Controls still require fresh owner and light reports."
        ),
        AlarmRefreshStatus.INCOMPLETE: (
            "Refresh was incomplete. Some reports or ownership evidence may be unavailable."
        ),
        AlarmRefreshStatus.COOLDOWN: (
            "Refresh is cooling down. Existing reports were kept; try again shortly."
        ),
        AlarmRefreshStatus.TIMED_OUT: "Refresh timed out. Existing reports may be stale.",
    }.get(getattr(result, "status", None), "Refresh unavailable. Existing reports may be stale.")


class BeamsDetails(Adw.Dialog):
    def __init__(self, client, identity):
        super().__init__(title="Beams light", content_width=460, content_height=600)
        self._identity = identity
        self._account = (get_cached_account_email() or "").strip().casefold()
        self._updating = False
        self._preference = BeamsPreference()
        self._session = BeamsViewSession(self._changed, self.retire, provider=lambda: get_client())
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        for edge in ("top", "bottom", "start", "end"):
            getattr(content, f"set_margin_{edge}")(18)
        group = Adw.PreferencesGroup(title="Reported status")
        self._reported = Adw.ActionRow(title="Light", use_markup=False)
        self._reported.set_subtitle_lines(0)
        group.add(self._reported)
        content.append(group)
        policy = Adw.PreferencesGroup(
            title="Controls in Halo",
            description=(
                "Enable these controls locally. Ring and other apps can still change this light. "
                "Saving preferences does not change the light."
            ),
        )
        self._enabled = Adw.SwitchRow(title="Enable light controls")
        self._duration = Adw.ComboRow(
            title="On duration",
            model=Gtk.StringList.new(["30 seconds", "1 minute", "5 minutes", "15 minutes"]),
        )
        policy.add(self._enabled)
        policy.add(self._duration)
        content.append(policy)
        self._on = Gtk.Button(label="Turn on for 1 minute", css_classes=["suggested-action"])
        self._automatic = Gtk.Button(label="Return to automatic")
        self._refresh = Gtk.Button(label="Refresh status")
        self._on.connect("clicked", lambda *_: self._request(self._preference.duration))
        self._automatic.connect("clicked", lambda *_: self._request(None))
        self._refresh.connect("clicked", self._refresh_clicked)
        for button in (self._on, self._automatic, self._refresh):
            content.append(button)
        content.append(
            Gtk.Label(
                label=(
                    "Automatic restores Ring's normal behavior. Motion may keep the light on. "
                    "Reported On or Off does not confirm automatic mode "
                    "or the remaining override time."
                ),
                xalign=0,
                wrap=True,
                css_classes=["dim-label", "caption"],
            )
        )
        self._reason = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        self._result = Gtk.Label(xalign=0, wrap=True)
        content.append(self._reason)
        content.append(self._result)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(content)
        toolbar.set_content(scroller)
        self.set_child(toolbar)
        self._enabled.connect("notify::active", self._save)
        self._duration.connect("notify::selected", self._save)
        self.connect("closed", lambda *_: self._session.close())
        self._session.open(client)
        self._changed(self._session.current(), {})

    def retire(self):
        self._session.close()
        self.close()

    def _preference_now(self):
        try:
            return preferences.get(self._account, *self._identity)
        except ValueError:
            return BeamsPreference()

    def _changed(self, snapshot, completed):
        light = snapshot.find(self._identity) if snapshot else None
        if light is None:
            self.retire()
            return
        self.set_title(light.device.name or "Beams light")
        self._reported.set_subtitle(
            f"{reported_text(light)} · {light.location_name or 'Ring location'}"
        )
        self._preference = self._preference_now()
        self._updating = True
        self._enabled.set_active(self._preference.enabled)
        self._duration.set_selected(ON_DURATIONS.index(self._preference.duration))
        self._updating = False
        self._enabled.set_sensitive(bool(self._account))
        self._duration.set_sensitive(bool(self._account))
        busy = self._session.pending("command")
        allowed = self._preference.enabled and not light.reason and not busy
        self._on.set_sensitive(allowed)
        self._automatic.set_sensitive(allowed)
        self._refresh.set_sensitive(not self._session.pending("inventory") and not busy)
        label = {30: "30 seconds", 60: "1 minute", 300: "5 minutes", 900: "15 minutes"}
        self._on.set_label(f"Turn on for {label[self._preference.duration]}")
        self._reason.set_label(
            reason_text(light.reason)
            or (
                "Enable controls to send an explicit lighting request."
                if not self._preference.enabled
                else ""
            )
        )
        for key, future in completed.items():
            try:
                value = future.result() if future is not None else None
                if key == "command":
                    text = {
                        "reported-on": (
                            "On was reported after the request. Override timing remains unverified."
                        ),
                        "automatic-unverified": (
                            "Automatic was requested. Ring does not report the mode; "
                            "check the light's behavior."
                        ),
                        "reported-off": (
                            "Off was reported after the On request. "
                            "The requested outcome is unconfirmed."
                        ),
                        "lighting-busy": (
                            "Another lighting request is in progress. Nothing was queued."
                        ),
                    }.get(
                        getattr(value, "code", None),
                        (
                            "The request may have been sent; its outcome is unknown. "
                            "Refresh before another request."
                            if getattr(value, "possibly_sent", False)
                            else "No lighting request was sent. Refresh status and try again."
                        ),
                    )
                    self._result.set_label(text)
                elif key == "inventory":
                    self._result.set_label(refresh_text(value))
            except Exception:
                self._result.set_label(
                    "Status or outcome unavailable. Refresh before trying again."
                )

    def _save(self, *_args):
        if self._updating or self._session.current() is None:
            return
        try:
            preferences.set(
                self._account,
                *self._identity,
                enabled=self._enabled.get_active(),
                duration=ON_DURATIONS[self._duration.get_selected()],
            )
            self._result.set_label(
                "Preferences saved on this computer. No lighting request was sent."
            )
        except (ValueError, OSError):
            self._result.set_label("Could not save preferences. Controls remain restricted.")
        self._changed(self._session.current(), {})

    def _refresh_clicked(self, *_args):
        session = self._session
        if session.current() is not None:
            session.request("inventory", lambda: session.client.refresh_alarm(self._identity[0]))

    def _request(self, duration):
        session = self._session
        snapshot = session.current()
        light = snapshot.find(self._identity) if snapshot else None
        preference = self._preference_now()
        if light is None or light.reason or not preference.enabled or session.pending("command"):
            return
        intent = BeamsIntent(lambda: self._preference_now() == preference)
        if session.request(
            "command",
            lambda: session.client.request_beams_light(
                *self._identity, duration, expected_revision=light.device.revision, intent=intent
            ),
            intent=intent,
        ):
            self._result.set_label("Waiting for a reported outcome…")
