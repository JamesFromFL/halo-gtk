"""Final application shutdown drains save cleanup before stopping its client."""

from types import SimpleNamespace

from halo_gtk import app, ring_client, save_jobs


def test_app_shutdown_drains_saves_before_stopping_ring_client(monkeypatch):
    calls = []
    monkeypatch.setattr(save_jobs, "shutdown", lambda: calls.append("save-cleanup") or True)
    monkeypatch.setattr(ring_client, "shutdown_client", lambda: calls.append("client"))
    monkeypatch.setattr(
        app,
        "Adw",
        SimpleNamespace(
            Application=SimpleNamespace(do_shutdown=lambda _app: calls.append("application"))
        ),
    )
    instance = SimpleNamespace(
        _quitting=False,
        _restore_thread=None,
        get_main_window=lambda: SimpleNamespace(
            stop_runtime_streams=lambda: calls.append("streams")
        ),
        _tray=SimpleNamespace(shutdown=lambda: calls.append("tray")),
    )
    app.RingApplication.do_shutdown(instance)
    assert instance._quitting
    assert calls == ["save-cleanup", "streams", "client", "tray", "application"]
