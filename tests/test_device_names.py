from __future__ import annotations

from halo_gtk import device_names


class _Device:
    id = 123
    name = "Front Door"


def _redirect_store(monkeypatch, tmp_path):
    monkeypatch.setattr(device_names, "CONFIG_DIR", tmp_path / "halo-gtk")
    monkeypatch.setattr(
        device_names,
        "NICKNAMES_FILE",
        tmp_path / "halo-gtk" / "device-nicknames.json",
    )


def test_display_name_uses_ring_name_without_nickname(monkeypatch, tmp_path):
    _redirect_store(monkeypatch, tmp_path)

    assert device_names.display_name(_Device()) == "Front Door"


def test_set_nickname_persists_and_overrides_display_name(monkeypatch, tmp_path):
    _redirect_store(monkeypatch, tmp_path)

    device_names.set_nickname(_Device(), "  Driveway   Door  ")

    assert device_names.display_name(_Device()) == "Driveway Door"
    assert device_names.display_name_for_id(123, "Front Door") == "Driveway Door"


def test_empty_nickname_clears_saved_value(monkeypatch, tmp_path):
    _redirect_store(monkeypatch, tmp_path)

    device_names.set_nickname(_Device(), "Driveway")
    device_names.set_nickname(_Device(), "")

    assert device_names.display_name(_Device()) == "Front Door"
    assert device_names.get_nickname(_Device()) == ""


def test_dict_device_key_supports_archived_events(monkeypatch, tmp_path):
    _redirect_store(monkeypatch, tmp_path)

    device_names.set_nickname("456", "Back Yard")

    assert (
        device_names.display_name({"device_id": "456", "camera_name": "Backyard Cam"})
        == "Back Yard"
    )
