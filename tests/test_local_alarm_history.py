"""Private saved history never fetches or controls Ring devices."""

import json
import stat
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from halo_gtk import local_alarm_history as local
from halo_gtk.alarm.history import AlarmHistoryEvent, AlarmHistoryPage, AlarmHistoryStatus

NOW = datetime(2026, 11, 16, tzinfo=UTC).timestamp()


@pytest.fixture
def store(tmp_path):
    return local.LocalAlarmHistory("a" * 32, root=tmp_path / "history", clock=lambda: NOW)


def page(*, location="one", key="row:0", retrieved=NOW, actor="Test actor"):
    event = AlarmHistoryEvent(
        key,
        location,
        "mode",
        "Disarmed",
        datetime.fromtimestamp(NOW - 20, UTC),
        datetime.fromtimestamp(retrieved, UTC),
        actor=actor,
        impulse_types=("security-panel.mode-switched.none",),
    )
    return AlarmHistoryPage(
        location,
        events=(event,),
        status=AlarmHistoryStatus.READY,
        retrieved_at=event.retrieved_at,
        source_rows=1,
    )


def test_off_by_default_and_opt_in_only_saves_normalized_fields(store):
    assert store.preferences() == {"available": True, "days": 0}
    store.save(page())
    assert not store.load("one").events
    store.set_retention(7)
    store.save(page())
    saved = store.load("one")
    assert len(saved.events) == 1
    assert saved.events[0].provenance == "Saved Ring cloud history"
    assert saved.status is AlarmHistoryStatus.PARTIAL and saved.stale and saved.from_cache
    assert saved.coverage == local.SAVED_COVERAGE and saved.next_offset is None
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o700
    path = store._location_path("one")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "one" not in path.name
    data = json.loads(path.read_text())
    assert set(data["entries"][0]["event"]) == local._FIELDS
    assert "token" not in path.read_text() and "url" not in path.read_text()


@pytest.mark.parametrize("operation", ["clear", "preferences"])
def test_crash_leftover_private_event_copy_is_removed_on_next_access(store, operation):
    store.preferences()
    leftover = store.path / ".history-abcdefgh"
    leftover.write_bytes(b"a prepared event copy")
    leftover.chmod(0o600)
    unrelated = store.path / "notes.txt"
    unrelated.write_text("unrelated")
    getattr(store, operation)()
    assert not leftover.exists() and unrelated.read_text() == "unrelated"


def test_crash_leftover_symlink_is_never_followed_or_removed(store, tmp_path):
    store.preferences()
    target = tmp_path / "outside"
    target.write_text("keep")
    leftover = store.path / ".history-abcdefgh"
    leftover.symlink_to(target)
    with pytest.raises(local.LocalHistoryError, match="Temporary history"):
        store.clear()
    assert leftover.is_symlink() and target.read_text() == "keep"


def test_disable_keeps_existing_until_original_expiry_and_clear_is_explicit(store):
    store.set_retention(7)
    store.save(page())
    store.set_retention(0)
    store.save(page(key="new:1"))
    assert [event.key for event in store.load("one").events] == ["row:0"]
    store.clear()
    assert not store.load("one").events
    assert store.preferences()["days"] == 0


def test_retention_changes_and_repeated_reads_do_not_extend_existing_expiry(store):
    store.set_retention(7)
    store.save(page())
    store.clock = lambda: NOW + 6 * 86400
    store.set_retention(30)
    store.save(page(retrieved=NOW + 6 * 86400))
    assert store.load("one").events[0].retrieved_at.timestamp() == NOW
    store.clock = lambda: NOW + 7 * 86400 - 0.1
    assert store.load("one").events
    store.clock = lambda: NOW + 7 * 86400
    assert not store.load("one").events
    assert not store._location_path("one").exists()


def test_shifted_cloud_positions_share_original_expiry_and_preserve_page_multiplicity(store):
    store.set_retention(7)
    original = page(key="same-digest:0")
    store.save(
        replace(
            original, events=(original.events[0], replace(original.events[0], key="same-digest:1"))
        )
    )
    store.clock = lambda: NOW + 6 * 86400
    shifted = page(key="same-digest:10", retrieved=NOW + 6 * 86400)
    store.save(
        replace(
            shifted, events=(shifted.events[0], replace(shifted.events[0], key="same-digest:11"))
        )
    )
    saved = store.load("one")
    assert len(saved.events) == 2 and saved.truncated
    assert all(event.retrieved_at.timestamp() == NOW for event in saved.events)
    more = replace(
        shifted,
        events=tuple(
            replace(shifted.events[0], key=f"same-digest:{index}") for index in range(20, 23)
        ),
    )
    store.save(more)
    assert len(store.load("one").events) == 3
    store.clock = lambda: NOW + 7 * 86400
    assert not store.load("one").events


def test_reversed_overlapping_keys_can_grow_uncertain_same_page_multiplicity(store):
    store.set_retention(7)
    first = page(key="digest:1")
    store.save(first)
    incoming = replace(first, events=(replace(first.events[0], key="digest:2"), first.events[0]))
    store.save(incoming)
    assert {event.key for event in store.load("one").events} == {"digest:1", "digest:2"}


@pytest.mark.parametrize("days", [True, -1, 1, 8, 31, "7", None])
def test_only_documented_retention_values_allowed(store, days):
    with pytest.raises(local.LocalHistoryError):
        store.set_retention(days)
    assert store.preferences()["days"] == 0


def test_profiles_and_location_revocations_are_isolated(store):
    second = local.LocalAlarmHistory("b" * 32, root=store.root, clock=lambda: NOW)
    for owner in (store, second):
        owner.set_retention(7)
        owner.save(page())
    store.save(page(location="two"))
    store.clear("one")
    assert not store.load("one").events
    assert store.load("two").events
    assert second.load("one").events


@pytest.mark.parametrize(
    "status",
    [
        AlarmHistoryStatus.EMPTY,
        AlarmHistoryStatus.UNAVAILABLE,
        AlarmHistoryStatus.PERMISSION_DENIED,
        AlarmHistoryStatus.UNSUPPORTED,
    ],
)
def test_failed_unsupported_and_cached_results_do_not_extend_storage(store, status):
    store.set_retention(7)
    store.save(replace(page(), status=status))
    store.save(replace(page(), from_cache=True))
    assert not store.load("one").events


def test_event_limit_retains_recent_copies_and_reports_truncation(store, monkeypatch):
    monkeypatch.setattr(local, "MAX_EVENTS", 2)
    store.set_retention(7)
    store.save(page(key="first", retrieved=NOW - 20, actor="First actor"))
    store.save(page(key="second", retrieved=NOW - 10, actor="Second actor"))
    store.save(page(key="third", actor="Third actor"))
    result = store.load("one")
    assert [event.key for event in result.events] == ["third", "second"]
    assert result.truncated


def test_location_limit_preserves_existing_records(store, monkeypatch):
    monkeypatch.setattr(local, "MAX_LOCATIONS", 1)
    store.set_retention(7)
    store.save(page())
    with pytest.raises(local.LocalHistoryError, match="location limit"):
        store.save(page(location="two"))
    assert store.load("one").events


def test_byte_bound_trims_and_reports_limited(store, monkeypatch):
    monkeypatch.setattr(local, "MAX_BYTES", 1000)
    store.set_retention(7)
    store.save(page(key="one", actor="a" * 250))
    store.save(page(key="two", actor="b" * 250))
    assert store._location_path("one").stat().st_size <= 1000
    assert len(store.load("one").events) == 1
    assert store.load("one").truncated


@pytest.mark.parametrize(
    "corruption",
    [
        "invalid-json",
        "wrong-location",
        "future-retrieval",
        "extra-field",
        "invalid-impulse",
        "bad-expiry",
        "duplicate-row",
        "oversized",
    ],
)
def test_corrupt_data_rejected_without_publication_and_clear_can_remove_it(store, corruption):
    store.set_retention(7)
    store.save(page())
    path = store._location_path("one")
    value = json.loads(path.read_text())
    if corruption == "invalid-json":
        path.write_text("{")
    elif corruption == "oversized":
        path.write_bytes(b"x" * (local.MAX_BYTES + 1))
    else:
        event = value["entries"][0]["event"]
        if corruption == "wrong-location":
            event["location_id"] = "two"
        elif corruption == "future-retrieval":
            event["retrieved_at"] = NOW + 86400
        elif corruption == "extra-field":
            event["raw_token"] = "must-never-publish"
        elif corruption == "invalid-impulse":
            event["impulse_types"] = ["invalid\nvalue"]
        elif corruption == "bad-expiry":
            value["entries"][0]["expires"] = NOW + 31 * 86400
        else:
            value["entries"].append(value["entries"][0])
        path.write_text(json.dumps(value))
    with pytest.raises(local.LocalHistoryError):
        store.load("one")
    store.clear("one")
    assert not path.exists()


@pytest.mark.parametrize("boundary", ["root", "profile", "file", "lock"])
def test_symlink_paths_are_not_followed(store, tmp_path, boundary):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("untouched")
    if boundary == "root":
        store.root.symlink_to(outside)
    else:
        store.root.mkdir(mode=0o700)
        if boundary == "profile":
            store.path.symlink_to(outside)
        elif boundary == "lock":
            (store.root / "store.lock").symlink_to(secret)
        else:
            store.path.mkdir(mode=0o700)
            store._location_path("one").symlink_to(secret)
    with pytest.raises((local.LocalHistoryError, OSError)):
        store.load("one")
    assert secret.read_text() == "untouched"


def test_nonprivate_file_is_rejected_and_not_deleted(store):
    store.set_retention(7)
    store.save(page())
    path = store._location_path("one")
    path.chmod(0o644)
    with pytest.raises(local.LocalHistoryError):
        store.load("one")
    with pytest.raises(local.LocalHistoryError):
        store.clear()
    assert path.exists()


def test_busy_store_does_not_wait_indefinitely(store):
    with store._locked(), pytest.raises(local.LocalHistoryError, match="busy"):
        store.preferences()


def test_write_failure_keeps_existing_cloud_result_and_saved_file(store, monkeypatch):
    store.set_retention(7)
    store.save(page())
    before = store._location_path("one").read_bytes()

    def fail(*_):
        raise OSError("synthetic disk full")

    monkeypatch.setattr(local.os, "replace", fail)
    source = page(key="next")
    result = store.annotate_save(source)
    assert result.events == source.events and result.status == source.status
    assert "could not be saved" in result.message
    assert store._location_path("one").read_bytes() == before
    assert not list(store.path.glob(".history-*"))


def test_invalid_input_location_or_timestamp_cannot_be_saved(store):
    store.set_retention(7)
    source = page()
    with pytest.raises(local.LocalHistoryError):
        store.save(replace(source, events=(replace(source.events[0], location_id="other"),)))
    with pytest.raises(local.LocalHistoryError):
        store.save(page(retrieved=NOW + 301))
    with pytest.raises(local.LocalHistoryError):
        store.load("../bad\nlocation")
    assert not store.load("one").events
