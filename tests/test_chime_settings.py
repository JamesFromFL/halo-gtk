"""Snooze/nightlight route, authority and authoritative-readback tests."""

import os

import pytest

from halo_gtk.chime import ChimeOperationStatus as Status
from halo_gtk.chime import parse_chime_inventory
from halo_gtk.chime_details import _operation_text
from halo_gtk.chime_rest import ChimeRestError, RingChimeRestGateway
from tests.test_alarm_rest import Response, fake_auth
from tests.test_chime_service import Gateway, intent_for, ready, row


class SettingsGateway(Gateway):
    def __init__(self):
        super().__init__(
            [
                row(
                    settings={"volume": 2, "night_light_settings": {"light_sensor_enabled": False}},
                    do_not_disturb={"seconds_left": 0},
                )
            ]
        )
        self.apply_setting = True

    async def set_snooze(self, api_id, minutes, **kwargs):
        receipt = await self._write(api_id, None, ("snooze", minutes), **kwargs)
        if self.apply_setting:
            self.payload["chimes"][0]["do_not_disturb"]["seconds_left"] = minutes * 60
        return receipt

    async def set_nightlight(self, api_id, enabled, **kwargs):
        receipt = await self._write(api_id, None, ("nightlight", enabled), **kwargs)
        if self.apply_setting:
            self.payload["chimes"][0]["settings"]["night_light_settings"][
                "light_sensor_enabled"
            ] = enabled
        return receipt


async def command(service, setting, value):
    intent = intent_for(service)
    return await getattr(service, "request_" + setting)(
        10, value, expected_revision=intent.revision, intent=intent
    )


@pytest.mark.parametrize(
    "setting,value", [("snooze", 15), ("snooze", 0), ("nightlight", True), ("nightlight", False)]
)
async def test_settings_readback_not_volume_or_sound(setting, value):
    service, gateway, _ = await ready(SettingsGateway())
    try:
        result = await command(service, setting, value)
        assert result.status is Status.REPORTED
        assert gateway.writes == [(10, None, (setting, value))]
        device = service.snapshot.find_device(10)
        assert device.volume == 2 and device.sound_operation_id is None
        assert device.operation.requested_value == value
        assert "Ring reports" in _operation_text(device.operation)
    finally:
        await service.close()


@pytest.mark.parametrize("setting,value", [("snooze", 30), ("nightlight", True)])
async def test_accepted_but_unconfirmed_setting_is_unknown_and_not_retried(setting, value):
    gateway = SettingsGateway()
    gateway.apply_setting = False
    service, _, _ = await ready(gateway)
    try:
        result = await command(service, setting, value)
        assert result.status is Status.UNKNOWN and result.code == "readback-unconfirmed"
        assert len(gateway.writes) == 1
        device = service.snapshot.find_device(10)
        assert (
            device.stale
            and device.snooze_seconds_left == 0
            and device.automatic_nightlight is False
        )
    finally:
        await service.close()


@pytest.mark.parametrize(
    "setting,value",
    [
        ("snooze", True),
        ("snooze", -1),
        ("snooze", 1441),
        ("snooze", "15"),
        ("nightlight", 1),
        ("nightlight", "true"),
    ],
)
async def test_invalid_setting_cannot_send(setting, value):
    service, gateway, _ = await ready(SettingsGateway())
    try:
        assert (await command(service, setting, value)).status is Status.REJECTED
        assert not gateway.writes
    finally:
        await service.close()


@pytest.mark.parametrize("setting,value", [("snooze", 15), ("nightlight", True)])
async def test_missing_reported_capability_does_not_authorize_setting(setting, value):
    service, gateway, _ = await ready()
    try:
        result = await command(service, setting, value)
        assert result.status is Status.REJECTED and not gateway.writes
    finally:
        await service.close()


async def test_revocation_at_send_boundary_blocks_new_setting():
    gateway = SettingsGateway()
    service, _, _ = await ready(gateway)

    async def revoke():
        gateway.payload["chimes"][0]["owned"] = False
        await service.refresh(force=True)

    gateway.before_admission = revoke
    try:
        result = await command(service, "snooze", 15)
        assert result.status is Status.REJECTED and not gateway.writes
    finally:
        await service.close()


async def test_exact_setting_routes_and_empty_clear_body():
    auth, calls, _ = fake_auth(Response(), Response(), Response())
    gateway = RingChimeRestGateway(auth)
    await gateway.set_snooze(10, 15)
    await gateway.set_snooze(10, 0)
    await gateway.set_nightlight(10, True)
    assert [(method, url, kwargs["json"]) for method, url, kwargs in calls] == [
        ("POST", "https://api.ring.com/clients_api/chimes/10/do_not_disturb", {"time": 15}),
        ("POST", "https://api.ring.com/clients_api/chimes/10/do_not_disturb", None),
        (
            "PATCH",
            "https://api.ring.com/devices/v1/devices/10/settings",
            {"night_light_settings": {"light_sensor_enabled": True}},
        ),
    ]
    assert "Content-Type" not in calls[1][2]["headers"]
    assert all(len(options["middlewares"]) == 1 for _, _, options in calls)


@pytest.mark.parametrize(
    "setting,value",
    [("snooze", True), ("snooze", -1), ("snooze", 1441), ("nightlight", 1), ("nightlight", None)],
)
async def test_gateway_rejects_invalid_settings(setting, value):
    auth, calls, _ = fake_auth()
    with pytest.raises(ChimeRestError):
        await getattr(RingChimeRestGateway(auth), "set_" + setting)(10, value)
    assert not calls


@pytest.mark.parametrize("value", [None, "true", 0, 1, {}, []])
def test_nightlight_unknown_is_not_false(value):
    device = parse_chime_inventory(
        {
            "chimes": [
                row(settings={"volume": 2, "night_light_settings": {"light_sensor_enabled": value}})
            ]
        }
    )[0]
    assert device.automatic_nightlight is None


@pytest.mark.skipif(
    os.environ.get("HALO_NATIVE_LAYOUT_TESTS") != "1", reason="Isolated GTK display required"
)
def test_native_chime_snooze_button_has_explicit_intent_and_no_sound(monkeypatch):
    from gi.repository import Adw, GLib

    from halo_gtk import chime_details as ui
    from tests.test_chime_details import Client, account, device

    Adw.init()
    client = Client(account(device(snooze_seconds_left=0, automatic_nightlight=False)))
    client.request_chime_setting = lambda *a, **k: client._request("setting", *a, **k)
    monkeypatch.setattr(ui, "get_client", lambda: client)
    monkeypatch.setattr(ui, "chime_monotonic_time", lambda: 100)
    window = Adw.Window(default_width=420, default_height=720)
    window.present()
    dialog = ui.ChimeDetails(client, 42)
    try:
        dialog.present(window)
        client.futures[0].set_result(client.snapshot)
        loop = GLib.MainLoop()
        GLib.timeout_add(100, lambda: (loop.quit(), False)[1])
        loop.run()
        assert dialog.get_width() <= 420
        assert dialog._snooze_apply.get_sensitive()
        assert dialog._nightlight_toggle.get_sensitive()
        dialog._snooze_choice.set_selected(1)
        dialog._snooze_apply.emit("clicked")
        name, args, kwargs = client.calls[-1]
        assert (name, args) == ("setting", (42, "snooze", 30))
        assert kwargs["expected_revision"] == 4
        intent = kwargs["intent_id"]
        dialog.retire(close=False)
        assert intent.cancelled
        assert all(call[0] != "sound" for call in client.calls)
    finally:
        dialog.retire(close=False)
        dialog.close()
        window.destroy()
