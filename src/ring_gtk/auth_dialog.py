"""Authentication dialog — collects Ring credentials and optional OTP."""

from __future__ import annotations

import threading

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk  # noqa: E402


class AuthDialog(Adw.Dialog):
    def __init__(self) -> None:
        super().__init__(title="Sign in to Ring", content_width=360)
        self._email: str = ""
        self._password: str = ""
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=24,
            margin_top=24,
            margin_bottom=24,
            margin_start=24,
            margin_end=24,
        )
        toolbar_view.set_content(box)

        # Credentials group
        creds_group = Adw.PreferencesGroup(title="Ring Account")
        box.append(creds_group)

        self._email_row = Adw.EntryRow(title="Email")
        self._email_row.set_input_purpose(Gtk.InputPurpose.EMAIL)
        creds_group.add(self._email_row)

        self._password_row = Adw.PasswordEntryRow(title="Password")
        creds_group.add(self._password_row)

        # OTP group — hidden until Ring asks for it
        self._otp_group = Adw.PreferencesGroup(
            title="Two-Factor Authentication",
            description="Enter the code sent to your email or phone.",
            visible=False,
        )
        box.append(self._otp_group)

        self._otp_row = Adw.EntryRow(title="Verification code")
        self._otp_row.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self._otp_group.add(self._otp_row)

        # Error banner
        self._error_label = Gtk.Label(
            visible=False,
            wrap=True,
            halign=Gtk.Align.START,
            css_classes=["error"],
        )
        box.append(self._error_label)

        # Sign-in button
        self._sign_in_btn = Gtk.Button(
            label="Sign In",
            css_classes=["suggested-action", "pill"],
            halign=Gtk.Align.CENTER,
        )
        self._sign_in_btn.connect("clicked", self._on_sign_in_clicked)
        box.append(self._sign_in_btn)

        # Allow Enter key to submit from any entry row
        for row in (self._email_row, self._password_row, self._otp_row):
            row.connect("entry-activated", lambda *_: self._sign_in_btn.emit("clicked"))

    # ------------------------------------------------------------------
    # Auth flow
    # ------------------------------------------------------------------

    def _on_sign_in_clicked(self, *_) -> None:
        email = self._email_row.get_text().strip()
        password = self._password_row.get_text()

        if not email or not password:
            self._show_error("Email and password are required.")
            return

        # Snapshot credentials so the OTP retry reuses them.
        self._email = email
        self._password = password

        otp_code = self._otp_row.get_text().strip() or None

        self._set_loading(True)
        self._error_label.set_visible(False)

        threading.Thread(
            target=self._authenticate,
            args=(email, password, otp_code),
            daemon=True,
        ).start()

    def _authenticate(self, email: str, password: str, otp_code: str | None) -> None:
        try:
            from ring_doorbell import Requires2FAError

            from ring_gtk.ring_client import init_client

            client = init_client(email, password, otp_code)
            client.start()
            GLib.idle_add(self._on_auth_success)

        except Exception as exc:
            from ring_doorbell import Requires2FAError

            if isinstance(exc, Requires2FAError):
                GLib.idle_add(self._show_otp_prompt)
            else:
                GLib.idle_add(self._show_error, str(exc))

    def _on_auth_success(self) -> bool:
        self.close()
        # Refresh the main window device list.
        app = Gtk.Application.get_default()
        if app is not None:
            win = app.get_active_window()
            if hasattr(win, "refresh"):
                win.refresh()
        return GLib.SOURCE_REMOVE

    def _show_otp_prompt(self) -> bool:
        self._otp_group.set_visible(True)
        self._otp_row.grab_focus()
        self._sign_in_btn.set_label("Verify")
        self._set_loading(False)
        return GLib.SOURCE_REMOVE

    def _show_error(self, message: str) -> bool:
        self._error_label.set_text(message)
        self._error_label.set_visible(True)
        self._set_loading(False)
        return GLib.SOURCE_REMOVE

    def _set_loading(self, loading: bool) -> None:
        self._sign_in_btn.set_sensitive(not loading)
        self._sign_in_btn.set_label("Signing in…" if loading else (
            "Verify" if self._otp_group.get_visible() else "Sign In"
        ))
        self._email_row.set_sensitive(not loading)
        self._password_row.set_sensitive(not loading)
