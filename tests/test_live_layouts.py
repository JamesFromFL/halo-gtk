"""Tests for saved Live Monitoring layouts."""


def test_save_and_load_layouts(tmp_path, monkeypatch):
    from halo_gtk import live_layouts

    monkeypatch.setattr(live_layouts, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(live_layouts, "LAYOUTS_FILE", tmp_path / "live-monitoring-layouts.json")

    saved = live_layouts.save_new_layout(
        name="Front Yard",
        size="large",
        order=[3, 2, 1],
        visible=[3, 1],
    )

    loaded = live_layouts.load_layouts()
    assert loaded == [saved]
    assert loaded[0]["name"] == "Front Yard"
    assert loaded[0]["size"] == "large"
    assert loaded[0]["order"] == [3, 2, 1]
    assert loaded[0]["visible"] == [3, 1]


def test_next_custom_name_uses_highest_saved_number(tmp_path, monkeypatch):
    from halo_gtk import live_layouts

    monkeypatch.setattr(live_layouts, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(live_layouts, "LAYOUTS_FILE", tmp_path / "live-monitoring-layouts.json")

    live_layouts.save_layouts(
        [
            {"id": "one", "name": "Custom 1", "size": "small", "order": [], "visible": []},
            {"id": "two", "name": "Patio", "size": "medium", "order": [], "visible": []},
            {"id": "three", "name": "Custom 4", "size": "large", "order": [], "visible": []},
        ]
    )

    assert live_layouts.next_custom_name() == "Custom 5"


def test_update_layout_renames_and_replaces_state(tmp_path, monkeypatch):
    from halo_gtk import live_layouts

    monkeypatch.setattr(live_layouts, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(live_layouts, "LAYOUTS_FILE", tmp_path / "live-monitoring-layouts.json")

    live_layouts.save_layouts(
        [{"id": "layout", "name": "Old", "size": "small", "order": [1], "visible": [1]}]
    )
    updated = live_layouts.update_layout(
        "layout",
        name="New",
        size="medium",
        order=[2, 1],
        visible=[2],
    )

    assert updated is not None
    assert updated["name"] == "New"
    assert updated["size"] == "medium"
    assert updated["order"] == [2, 1]
    assert updated["visible"] == [2]
    assert live_layouts.load_layouts()[0] == updated


def test_delete_layout_removes_saved_layout(tmp_path, monkeypatch):
    from halo_gtk import live_layouts

    monkeypatch.setattr(live_layouts, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(live_layouts, "LAYOUTS_FILE", tmp_path / "live-monitoring-layouts.json")

    live_layouts.save_layouts(
        [
            {"id": "one", "name": "One", "size": "small", "order": [1], "visible": [1]},
            {"id": "two", "name": "Two", "size": "medium", "order": [2], "visible": [2]},
        ]
    )

    assert live_layouts.delete_layout("one") is True
    assert live_layouts.delete_layout("missing") is False
    assert [layout["id"] for layout in live_layouts.load_layouts()] == ["two"]


def test_clean_name_caps_length_and_falls_back():
    from halo_gtk import live_layouts

    assert live_layouts.clean_name("   ", "Custom 1") == "Custom 1"
    assert live_layouts.clean_name("  Front    Yard  ", "Custom 1") == "Front Yard"
    assert len(live_layouts.clean_name("x" * 60, "Custom 1")) == 50
