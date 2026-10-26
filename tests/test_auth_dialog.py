"""Authentication submission lifecycle regressions without contacting Ring."""

from types import SimpleNamespace

from halo_gtk.auth_dialog import AuthDialog


def test_loading_guard_rejects_programmatic_enter_key_resubmission():
    # Entry activation emits clicked directly, even when the button is insensitive.
    page = SimpleNamespace(_loading=True)

    AuthDialog._on_sign_in_clicked(page)


def test_loading_disables_and_reenables_the_verification_code_entry():
    changes = []
    page = SimpleNamespace(
        _loading=False,
        _sign_in_btn=SimpleNamespace(
            set_sensitive=lambda _value: None,
            set_label=lambda _value: None,
        ),
        _email_row=SimpleNamespace(set_sensitive=lambda _value: None),
        _password_row=SimpleNamespace(set_sensitive=lambda _value: None),
        _otp_row=SimpleNamespace(set_sensitive=changes.append),
        _otp_group=SimpleNamespace(get_visible=lambda: True),
    )

    AuthDialog._set_loading(page, True)
    assert page._loading is True
    AuthDialog._set_loading(page, False)

    assert page._loading is False
    assert changes == [False, True]
