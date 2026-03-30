"""Authentication dialog — collects Ring credentials and optional OTP."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, GLib, Gtk  # noqa: E402


class AuthDialog(Adw.Dialog):
    def __init__(self) -> None:
        super().__init__(title="Sign in to Ring", content_width=360)
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

        # OTP group (revealed after first auth attempt if needed)
        self._otp_group = Adw.PreferencesGroup(
            title="Two-Factor Authentication",
            description="Enter the code sent to your email or phone.",
            visible=False,
        )
        box.append(self._otp_group)

        self._otp_row = Adw.EntryRow(title="Verification code")
        self._otp_row.set_input_purpose(Gtk.InputPurpose.DIGITS)
        self._otp_group.add(self._otp_row)

        # Status label (errors)
        self._status_label = Gtk.Label(
            visible=False,
            wrap=True,
            css_classes=["error"],
        )
        box.append(self._status_label)

        # Sign-in button
        self._sign_in_btn = Gtk.Button(
            label="Sign In",
            css_classes=["suggested-action", "pill"],
            halign=Gtk.Align.CENTER,
        )
        self._sign_in_btn.connect("clicked", self._on_sign_in_clicked)
        box.append(self._sign_in_btn)

    # ------------------------------------------------------------------
    # Auth flow
    # ------------------------------------------------------------------

    def _on_sign_in_clicked(self, *_) -> None:
        email = self._email_row.get_text().strip()
        password = self._password_row.get_text()

        if not email or not password:
            self._show_error("Email and password are required.")
            return

        self._sign_in_btn.set_sensitive(False)
        self._sign_in_btn.set_label("Signing in…")
        self._status_label.set_visible(False)

        otp_code = self._otp_row.get_text().strip() or None

        import threading

        threading.Thread(
            target=self._authenticate,
            args=(email, password, otp_code),
            daemon=True,
        ).start()

    def _authenticate(self, email: str, password: str, otp: str | None) -> None:
        try:
            from ring_gtk.ring_client import init_client

            otp_cb = (lambda: otp) if otp else None
            client = init_client(email, password, otp_cb)
            client.start()
            GLib.idle_add(self._on_auth_success)
        except Exception as exc:
            msg = str(exc)
            if "two factor" in msg.lower() or "otp" in msg.lower() or "mfa" in msg.lower():
                GLib.idle_add(self._show_otp_prompt)
            else:
                GLib.idle_add(self._show_error, msg)

    def _on_auth_success(self) -> bool:
        self.close()
        # Trigger a window refresh
        from ring_gtk.ring_client import get_client  # noqa: F401

        return GLib.SOURCE_REMOVE

    def _show_otp_prompt(self) -> bool:
        self._otp_group.set_visible(True)
        self._sign_in_btn.set_label("Verify")
        self._sign_in_btn.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _show_error(self, message: str) -> bool:
        self._status_label.set_text(message)
        self._status_label.set_visible(True)
        self._sign_in_btn.set_label("Sign In")
        self._sign_in_btn.set_sensitive(True)
        return GLib.SOURCE_REMOVE
