"""Focused regression tests for the adaptive Settings routes."""

from __future__ import annotations

from types import SimpleNamespace

from halo_gtk.settings_page import SettingsPage


class _SplitView:
    def __init__(self, *, collapsed: bool = False) -> None:
        self.collapsed = collapsed
        self.show_content = False

    def get_collapsed(self) -> bool:
        return self.collapsed

    def set_show_content(self, show_content: bool) -> None:
        self.show_content = show_content


def test_category_activation_selects_detail_and_opens_content_route():
    shown = []
    titles = []
    split_view = _SplitView(collapsed=True)
    page = SimpleNamespace(
        _stack=SimpleNamespace(set_visible_child_name=shown.append),
        _content_title=SimpleNamespace(set_title=titles.append),
        _split_view=split_view,
    )
    page._on_category_selected = lambda list_box, row: SettingsPage._on_category_selected(
        page,
        list_box,
        row,
    )
    row = SimpleNamespace(
        _settings_page_name="storage",
        _settings_page_title="Storage",
    )

    SettingsPage._on_category_activated(page, object(), row)

    assert shown == ["storage"]
    assert titles == ["Storage"]
    assert split_view.show_content is True


def test_local_back_route_returns_to_categories_and_restores_focus():
    focused = []
    split_view = _SplitView(collapsed=True)
    page = SimpleNamespace(
        _split_view=split_view,
        _category_list=SimpleNamespace(grab_focus=lambda: focused.append(True)),
    )
    split_view.show_content = True

    SettingsPage._on_back_clicked(page, object())

    assert split_view.show_content is False
    assert focused == [True]


def test_collapsed_state_exposes_back_route_and_complete_sidebar_header():
    visibility = []
    title_buttons = []
    page = SimpleNamespace(
        _back_button=SimpleNamespace(set_visible=visibility.append),
        _sidebar_header=SimpleNamespace(set_show_end_title_buttons=title_buttons.append),
    )

    SettingsPage._on_split_collapsed_changed(page, _SplitView(collapsed=True), None)
    SettingsPage._on_split_collapsed_changed(page, _SplitView(collapsed=False), None)

    assert visibility == [True, False]
    assert title_buttons == [True, False]
