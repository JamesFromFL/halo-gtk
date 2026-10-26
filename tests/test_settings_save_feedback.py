"""Local save failures preserve persisted controls and expose an outcome."""

from types import SimpleNamespace

import pytest
from ring_doorbell import RingError

from halo_gtk import settings_page as module


def test_disk_failure_reverts_tray_switch_without_app_side_effect(monkeypatch):
    page = module.SettingsPage.__new__(module.SettingsPage)
    page._refreshing = False
    switch = SimpleNamespace(active=False)
    switch.get_active = lambda: switch.active
    messages, applied = [], []
    persisted = {"show_tray_icon": True}
    page.refresh = lambda: setattr(switch, "active", persisted["show_tray_icon"])
    page._show_save_error = messages.append
    page._apply_app_settings = lambda: applied.append(True)
    monkeypatch.setattr(module._config, "load", lambda: dict(persisted))

    def fail(_cfg):
        raise OSError("disk full")

    monkeypatch.setattr(module._config, "save", fail)
    page._on_tray_toggled(switch, None)
    assert switch.active is True and persisted == {"show_tray_icon": True}
    assert len(messages) == 1 and "Could not save" in messages[0]
    assert applied == []


def test_entry_save_failure_restores_persisted_text(monkeypatch):
    page = module.SettingsPage.__new__(module.SettingsPage)
    page._refreshing = False
    row = SimpleNamespace(text="changed")
    row.get_text = lambda: row.text
    messages = []
    page._show_save_error = messages.append
    page.refresh = lambda: setattr(row, "text", "saved")
    monkeypatch.setattr(module._config, "load", lambda: {"custom_motion_message": "saved"})
    monkeypatch.setattr(
        module._config, "save", lambda _cfg: (_ for _ in ()).throw(OSError("read only"))
    )
    page._on_custom_message_changed(row, None, "custom_motion_message")
    assert row.text == "saved" and len(messages) == 1


def test_failed_config_save_restores_autostart_file(monkeypatch):
    page = module.SettingsPage.__new__(module.SettingsPage)
    messages, writes, refreshes = [], [], []
    page._show_save_error = messages.append
    page.refresh = lambda: refreshes.append(True)
    monkeypatch.setattr(module.autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(
        module.autostart,
        "set_enabled",
        lambda enabled, *, background: writes.append((enabled, background)),
    )
    monkeypatch.setattr(
        module._config, "save", lambda _cfg: (_ for _ in ()).throw(OSError("disk full"))
    )
    assert (
        page._save_autostart_config(
            {"autostart_login": True, "autostart_background": True},
            {"autostart_login": False, "autostart_background": False},
        )
        is False
    )
    assert writes == [(True, True), (False, False)]
    assert messages and refreshes


def test_autostart_write_failure_does_not_publish_config(monkeypatch):
    page = module.SettingsPage.__new__(module.SettingsPage)
    messages, saved = [], []
    page._show_save_error = messages.append
    page.refresh = lambda: None
    monkeypatch.setattr(module.autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(
        module.autostart,
        "set_enabled",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("permission denied")),
    )
    monkeypatch.setattr(module._config, "save", saved.append)
    assert (
        page._save_autostart_config(
            {"autostart_login": True, "autostart_background": False},
            {},
        )
        is False
    )
    assert saved == [] and len(messages) == 1


def test_directory_paths_and_account_email_are_rendered_literally(monkeypatch):
    values = []
    client = SimpleNamespace(is_authenticated=True)
    page = module.SettingsPage.__new__(module.SettingsPage)
    page._account_email_client = client
    page._account_email_row = SimpleNamespace(set_subtitle=values.append)
    page._snapshot_dir_row = SimpleNamespace(set_subtitle=values.append)
    page._video_dir_row = SimpleNamespace(set_subtitle=values.append)
    page._camera_subfolders_switch = object()
    page._open_folder_after_save_switch = object()
    page._set_switch_active = lambda *_args: None
    page._display_path = lambda text: text
    monkeypatch.setattr(module, "get_client", lambda: client)
    page._finish_load_account_email("a&<b>@example.test", client)
    page._refresh_directories({"snapshot_dir": "/tmp/a&b", "video_dir": "/tmp/<front>"})
    assert values == ["a&amp;&lt;b&gt;@example.test", "/tmp/a&amp;b", "/tmp/&lt;front&gt;"]


def test_dynamic_status_error_text_is_escaped():
    values = []
    page = module.SettingsPage.__new__(module.SettingsPage)
    row = SimpleNamespace(set_subtitle=values.append, set_visible=lambda _value: None)
    for name in ("storage", "advanced", "account", "live_monitoring"):
        setattr(page, f"_{name}_message_row", row)
        getattr(page, f"_set_{name}_message")("Error: <folder> & permission")
    assert values == ["Error: &lt;folder&gt; &amp; permission"] * 4


@pytest.mark.parametrize("operation", ["refresh", "logout", "rejected_logout"])
def test_account_worker_error_does_not_render_signed_query(monkeypatch, operation):
    page = module.SettingsPage.__new__(module.SettingsPage)
    displayed = []
    page._account_message_row = SimpleNamespace(
        set_subtitle=displayed.append,
        set_visible=lambda _value: None,
    )
    page._set_account_buttons_sensitive = lambda _value: None
    page.refresh = page._refresh_main_window = page._show_sign_in_dialog = lambda: None

    def fail(*_args, **_kwargs):
        raise RingError("https://api.ring.invalid/request?X-Amz-Signature=secret-token")

    monkeypatch.setattr(module, "logout_client", fail)
    monkeypatch.setattr(module.GLib, "idle_add", lambda callback, *args: callback(*args))
    if operation == "refresh":
        page._refresh_session(SimpleNamespace(refresh_session_token=fail))
    elif operation == "logout":
        page._logout_worker()
    else:
        page._rejected_session_logout_worker(object())
    assert len(displayed) == 1
    assert "secret-token" not in displayed[0] and "Signature" not in displayed[0]
    assert "https://" not in displayed[0]
