"""Production media controls with fake widgets; no display, Ring or microphone."""

from halo_gtk import intercom_video_dialog as ui
from tests.test_intercom_media import fixture


class Widget:
    def __init__(self):
        self.active = False
        self.sensitive = False
        self.visible = True
        self.label = ""
        self.children = []

    def get_active(self):
        return self.active

    def set_active(self, value):
        self.active = value

    def get_sensitive(self):
        return self.sensitive

    def set_sensitive(self, value):
        self.sensitive = value

    def set_visible(self, value):
        self.visible = value

    def set_label(self, value):
        self.label = value

    def append(self, child):
        self.children.append(child)

    def remove(self, child):
        self.children.remove(child)


class View:
    instances = []

    def __init__(self, *, media_authority):
        self.authority = media_authority
        self.volumes = []
        self.mic_starts = self.mic_stops = self.stops = 0
        self.callback = None
        self.lease = None
        self.__class__.instances.append(self)

    def set_volume(self, volume):
        self.volumes.append(volume)

    def set_on_stream_state_changed(self, callback):
        self.callback = callback

    def start_for_device(self, device):
        self.lease = self.authority.acquire(device, "live")
        assert self.lease

    def start_talking(self, *, on_state_changed):
        self.mic_starts += 1
        self.talk_callback = on_state_changed
        on_state_changed("active")

    def stop_talking(self):
        self.mic_stops += 1

    def stop(self):
        self.stops += 1
        self.authority.release(self.lease)


class Dialog:
    def __init__(self, client):
        self._client, self._api_id, self._epoch = client, 42, 3
        self._view = self._authority = None
        self._running = self._talking = self._closed = False
        self._timer = None
        for name in (
            "_opt_in",
            "_placeholder",
            "_media",
            "_start_button",
            "_end_button",
            "_listen",
            "_talk",
            "_status",
        ):
            setattr(self, name, Widget())


for name in (
    "_tick",
    "_enabled_changed",
    "_start",
    "_stream_state",
    "_listen_changed",
    "_press_talk",
    "_talk_state",
    "_release_talk",
    "_key_pressed",
    "_key_released",
    "_end",
    "retire",
):
    setattr(Dialog, name, getattr(ui.IntercomVideoDialog, name))


def prepared(monkeypatch):
    from halo_gtk import live_stream

    class FakeView(View):
        instances = []

    monkeypatch.setattr(live_stream, "LiveStreamView", FakeView)
    client, _, _ = fixture()
    return Dialog(client), client, FakeView


def test_opening_or_enabling_never_starts_media_or_microphone(monkeypatch):
    dialog, _, views = prepared(monkeypatch)
    dialog._tick()
    assert not dialog._start_button.sensitive
    dialog._start()
    assert views.instances == []
    dialog._opt_in.active = True
    dialog._enabled_changed()
    assert dialog._start_button.sensitive and views.instances == []


def test_explicit_start_is_muted_and_mic_only_transmits_while_held(monkeypatch):
    dialog, _, views = prepared(monkeypatch)
    dialog._opt_in.active = True
    dialog._start()
    view = views.instances[0]
    assert view.volumes == [0] and view.mic_starts == 0
    assert dialog._running and not dialog._listen.sensitive and not dialog._talk.sensitive
    view.callback("active")
    assert dialog._listen.sensitive and dialog._talk.sensitive
    dialog._press_talk()
    dialog._press_talk()  # Key repeat does not create another microphone request.
    assert view.mic_starts == 1 and dialog._talking
    dialog._release_talk()
    assert view.mic_stops == 1 and not dialog._talking
    view.talk_callback("active")  # A late callback cannot claim a held microphone.
    assert dialog._status.label == "Microphone off."
    dialog._end()
    assert view.stops == 1 and view.authority._lease is None
    assert not dialog._listen.active and not dialog._talk.sensitive


def test_keyboard_release_opt_out_and_account_change_end_media(monkeypatch):
    dialog, client, views = prepared(monkeypatch)
    dialog._opt_in.active = True
    dialog._start()
    view = views.instances[0]
    view.callback("active")
    assert dialog._key_pressed(None, ui.Gdk.KEY_space, 0, 0)
    dialog._key_released(None, ui.Gdk.KEY_space, 0, 0)
    assert view.mic_starts == view.mic_stops == 1
    client.active = False
    dialog._tick()
    assert view.stops == 1 and not dialog._running
    assert not dialog._start_button.sensitive


def test_opt_out_and_closing_never_leave_stream_or_microphone_active(monkeypatch):
    dialog, _, views = prepared(monkeypatch)
    dialog._opt_in.active = True
    dialog._start()
    view = views.instances[0]
    view.callback("active")
    dialog._press_talk()
    dialog._opt_in.active = False
    dialog._enabled_changed()
    assert view.mic_stops >= 1 and view.stops == 1 and not dialog._running
    dialog.retire()
    dialog.retire()
    assert dialog._closed and not dialog._start_button.sensitive
