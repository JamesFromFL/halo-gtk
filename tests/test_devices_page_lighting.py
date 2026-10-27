"""Location-scoped lighting lists preserve existing Chime and physical counts."""

from types import SimpleNamespace

import pytest

from halo_gtk import devices_page as ui
from halo_gtk.devices_page import DevicesPage, _device_counts
from tests import test_light_group_details as helpers
from tests.test_chime_details import account as chime_account
from tests.test_devices_page import PageHarness as ChimePageHarness
from tests.test_light_group_details import Widget, group, location, snapshot

lighting = helpers.lighting


class PageHarness(ChimePageHarness):
    def __init__(self, context):
        super().__init__(SimpleNamespace(current=context.current))
        self._lighting_session = ui._LightViewSession(
            self._lighting_changed, self._lighting_retired, provider=lambda: context.current[0]
        )
        self._light_location_ids = ()
        self._light_selected_id = None
        self._light_location_epoch = None
        self._light_updating = False
        self._light_rows = []
        self._light_needs_groups = self._light_force_groups = False
        for name in ("_light_group", "_light_refresh", "_light_location", "_light_message"):
            setattr(self, name, Widget())


for name in (
    "_refresh_lighting",
    "_lighting_retired",
    "_lighting_changed",
    "_render_lighting",
    "_add_light_row",
    "_light_location_changed",
    "_open_light_group",
):
    setattr(PageHarness, name, getattr(DevicesPage, name))


@pytest.fixture
def page(lighting, monkeypatch):
    monkeypatch.setattr(ui, "get_client", lambda: lighting.current[0])
    monkeypatch.setattr(ui.Adw, "ActionRow", Widget)
    monkeypatch.setattr(ui.Gtk, "Image", Widget)
    monkeypatch.setattr(ui.Gtk.StringList, "new", lambda values: list(values))
    return PageHarness(lighting)


def test_hidden_refresh_reads_cache_only(page, lighting):
    for _ in range(10):
        page.refresh()
    assert not lighting.client.calls
    assert not lighting.client.light_callbacks
    assert not lighting.timer.sources
    assert page._light_rows[0].title == "Front <b>lights</b>"


def test_groups_are_not_added_to_physical_device_count():
    sdk = [SimpleNamespace(family=family) for family in ("doorbots", "chimes", "groups", "groups")]
    assert _device_counts(sdk, chime_account()) == (2, 1, ["chimes", "doorbots"])


def test_shown_loads_locations_then_only_selected_list_and_is_idempotent(page, lighting):
    page.on_page_shown()
    page.on_page_shown()
    assert len([call for call in lighting.client.calls if call[0] == "locations"]) == 1
    lighting.timer.drain()
    assert not any(call[0] == "groups" for call in lighting.client.calls)
    future = next(
        future
        for call, future in zip(lighting.client.calls, lighting.client.futures, strict=True)
        if call[0] == "locations"
    )
    future.set_result(lighting.client.light_snapshot)
    lighting.timer.drain()
    assert [call[1] for call in lighting.client.calls if call[0] == "groups"] == [("home",)]


def test_location_change_cancels_old_list_and_closes_old_dialog(page, lighting):
    lighting.client.light_snapshot = snapshot(
        location(), location(group(location_id="garden"), location_id="garden", name="Garden")
    )
    page.on_page_shown()
    for call, future in zip(lighting.client.calls, lighting.client.futures, strict=True):
        if call[0] == "locations":
            future.set_result(lighting.client.light_snapshot)
    lighting.timer.drain()
    old_future = lighting.client.futures[-1]
    retired = []
    page._dialog = SimpleNamespace(retire=lambda: retired.append(True))
    page._light_location.set_selected(1)
    page._light_location_changed(page._light_location, None)
    assert old_future.cancelled()
    assert retired == [True]
    assert lighting.client.calls[-1][0:2] == ("groups", ("garden",))
    assert page._light_selected_id == "garden"


def test_equal_group_ids_in_other_location_cannot_activate_old_row(page, lighting, monkeypatch):
    page.on_page_shown()
    lighting.client.light_snapshot = snapshot(
        location(group(location_id="garden"), location_id="garden")
    )
    monkeypatch.setattr(ui, "LightGroupDetails", lambda *args: pytest.fail("wrong location opened"))
    page._open_light_group(None, 5, "home", 3, "lights", 2)


def test_incomplete_member_count_is_unknown_and_names_literal(page, lighting):
    lighting.client.light_snapshot = snapshot(location(group(membership_complete=False)))
    page.refresh()
    assert page._light_rows[0].use_markup is False
    assert "Member count unknown" in page._light_rows[0].subtitle
    assert "Group reports Off" in page._light_rows[0].subtitle


def test_hide_cancels_reads_and_clears_location_selector(page, lighting):
    page.on_page_shown()
    page.on_page_hidden()
    assert not lighting.client.light_callbacks
    assert page._light_location_ids == ()
    assert page._light_location.model == []
    assert not lighting.timer.sources
    assert all(future.cancelled() for future in lighting.client.futures)


def test_account_retirement_replaces_old_private_location_names(page, lighting):
    page.on_page_shown()
    lighting.current[0] = None
    page.refresh()
    assert page._light_location.model == []
    assert page._light_location_ids == ()
    assert page._light_rows[0].title == "Lighting locations not loaded"


def test_authoritative_location_removal_retires_selection(page, lighting):
    page.on_page_shown()
    lighting.client.publish_lighting(
        helpers.LightGroupsSnapshot(5, complete=True, has_inventory=True)
    )
    lighting.timer.drain()
    assert page._light_selected_id is None
    assert page._light_rows[0].title == "No lighting locations reported"
