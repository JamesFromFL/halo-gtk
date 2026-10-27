from dataclasses import replace

from halo_gtk.alarm.events import AlarmEvent, AlarmEventBuffer, AlarmEventReducer
from halo_gtk.alarm.models import (
    AlarmAccountSnapshot,
    AlarmAssetSnapshot,
    AlarmDeviceKind,
    AlarmDeviceSnapshot,
    AlarmLocationSnapshot,
    AlarmMode,
    AlarmSignal,
    TriState,
)
from halo_gtk.alarm.models import (
    AlarmConnectionStatus as Connection,
)
from halo_gtk.alarm.models import (
    AlarmInventoryStatus as Inventory,
)


def snapshot(
    *,
    signal=AlarmSignal.NONE,
    contact=TriState.INACTIVE,
    complete=True,
    epoch=1,
    generation=1,
    online=True,
):
    panel = AlarmDeviceSnapshot(
        "home", "hub", "panel", kind=AlarmDeviceKind.SECURITY_PANEL, signal=signal, epoch=epoch
    )
    sensor = AlarmDeviceSnapshot(
        "home", "hub", "sensor", kind=AlarmDeviceKind.CONTACT_SENSOR, contact=contact, epoch=epoch
    )
    asset = AlarmAssetSnapshot(
        "home",
        "hub",
        connection=Connection.ONLINE if online else Connection.OFFLINE,
        stale=not online,
        devices=(panel, sensor),
        epoch=epoch,
    )
    location = AlarmLocationSnapshot(
        "home",
        "Private home",
        connection=Connection.ONLINE if online else Connection.OFFLINE,
        inventory=Inventory.COMPLETE if complete else Inventory.PARTIAL,
        assets=(asset,),
        mode=AlarmMode.DISARMED,
        epoch=epoch,
    )
    return AlarmAccountSnapshot(generation=generation, locations=(location,))


def test_initial_inventory_and_duplicate_are_silent():
    reducer = AlarmEventReducer()
    value = snapshot(contact=TriState.ACTIVE)
    assert reducer.update(value) == ()
    assert reducer.update(value) == ()


def test_transition_is_observation_not_remote_event_timestamp():
    reducer = AlarmEventReducer(clock=lambda: 12)
    reducer.update(snapshot())
    events = reducer.update(snapshot(contact=TriState.ACTIVE))
    assert len(events) == 1
    assert events[0].kind == "contact"
    assert events[0].detail == "Open"
    assert events[0].source == "Halo observation"
    assert events[0].observed_at == 12
    assert (events[0].asset_id, events[0].zid) == ("hub", "sensor")


def test_reconnect_rebaselines_routine_changes_without_clearing_critical_episode():
    reducer = AlarmEventReducer()
    assert reducer.update(snapshot(signal=AlarmSignal.FIRE))[0].critical
    reducer.update(snapshot(online=False, signal=AlarmSignal.FIRE))
    events = reducer.update(snapshot(epoch=2, signal=AlarmSignal.FIRE, contact=TriState.ACTIVE))
    assert [event.kind for event in events] == ["connection-restored"]
    assert reducer.update(snapshot(epoch=2, signal=AlarmSignal.FIRE))[-1].kind == "contact"


def test_critical_does_not_wait_for_optional_inventory():
    reducer = AlarmEventReducer()
    events = reducer.update(snapshot(signal=AlarmSignal.CARBON_MONOXIDE, complete=False))
    assert len(events) == 1
    assert events[0].critical
    assert events[0].detail == "Carbon monoxide"
    assert reducer.update(snapshot(signal=AlarmSignal.CARBON_MONOXIDE)) == ()


def test_unknown_does_not_clear_alarm_and_explicit_clear_allows_next_episode():
    reducer = AlarmEventReducer()
    reducer.update(snapshot(signal=AlarmSignal.FIRE))
    assert reducer.update(snapshot(signal=AlarmSignal.UNKNOWN)) == ()
    assert reducer.update(snapshot(signal=AlarmSignal.FIRE)) == ()
    cleared = reducer.update(snapshot())
    assert cleared[0].kind == "alarm-cleared"
    assert not cleared[0].critical
    assert reducer.update(snapshot(signal=AlarmSignal.FIRE))[0].critical


def test_different_critical_signal_is_not_deduplicated_as_previous_one():
    reducer = AlarmEventReducer()
    reducer.update(snapshot(signal=AlarmSignal.BURGLAR))
    event = reducer.update(snapshot(signal=AlarmSignal.FIRE))[0]
    assert event.critical and event.detail == "Fire"


def test_account_and_location_removal_clear_baselines():
    reducer = AlarmEventReducer()
    reducer.update(snapshot())
    assert reducer.update(snapshot(contact=TriState.ACTIVE, generation=2)) == ()
    reducer.update(AlarmAccountSnapshot(generation=2))
    assert reducer.update(snapshot(generation=2)) == ()


def test_queue_reserves_space_by_evicting_routine_before_critical():
    buffer = AlarmEventBuffer(capacity=2)
    routine = AlarmEvent(1, "home", "motion", "sensor", "Motion")
    fire = AlarmEvent(1, "home", "active-alarm", "alarm", "Fire", critical=True)
    buffer.put(routine)
    buffer.put(replace(routine, location_id="other"))
    buffer.put(fire)
    buffer.put(replace(fire, detail="Fire and CO"))
    events, suppressed = buffer.drain()
    assert len(events) == 2 and suppressed == 1
    assert events[-1].critical and events[-1].detail == "Fire and CO"
    assert buffer.drain() == ((), 0)


def test_revoke_removes_queued_sensitive_targets():
    buffer = AlarmEventBuffer()
    buffer.put(AlarmEvent(1, "home", "mode", "mode", "Mode", location_name="Private"))
    buffer.revoke("home")
    assert buffer.drain() == ((), 0)


def test_new_critical_supersedes_queued_clear_at_same_location():
    reducer, buffer = AlarmEventReducer(), AlarmEventBuffer()
    for signal in (AlarmSignal.FIRE, AlarmSignal.NONE, AlarmSignal.CARBON_MONOXIDE):
        for event in reducer.update(snapshot(signal=signal)):
            buffer.put(event)
    events, suppressed = buffer.drain()
    assert [(event.kind, event.detail) for event in events] == [("active-alarm", "Carbon monoxide")]
    assert suppressed == 0


def test_critical_then_explicit_clear_is_preserved_without_new_active_signal():
    reducer, buffer = AlarmEventReducer(), AlarmEventBuffer()
    for signal in (AlarmSignal.FIRE, AlarmSignal.NONE):
        for event in reducer.update(snapshot(signal=signal)):
            buffer.put(event)
    events, suppressed = buffer.drain()
    assert [event.kind for event in events] == ["active-alarm", "alarm-cleared"]
    assert suppressed == 0


def test_new_critical_does_not_remove_another_locations_queued_clear():
    buffer = AlarmEventBuffer()
    clear = AlarmEvent(1, "other", "alarm-cleared", "alarm", "Cleared")
    buffer.put(clear)
    buffer.put(AlarmEvent(1, "home", "active-alarm", "alarm", "Fire", critical=True))
    assert buffer.drain()[0][0] == clear


def test_stale_device_cannot_announce_new_critical_or_clear_previous_episode():
    reducer = AlarmEventReducer()
    active = snapshot(signal=AlarmSignal.FIRE)
    reducer.update(active)
    location = active.locations[0]
    asset = location.assets[0]
    stale_panel = replace(asset.devices[0], stale=True, signal=AlarmSignal.NONE)
    stale = replace(
        active,
        locations=(
            replace(location, assets=(replace(asset, devices=(stale_panel, *asset.devices[1:])),)),
        ),
    )
    assert reducer.update(stale) == ()
    assert reducer.update(active) == ()
    fresh = AlarmEventReducer()
    stale_fire = replace(stale_panel, signal=AlarmSignal.FIRE)
    stale = replace(
        active,
        locations=(
            replace(location, assets=(replace(asset, devices=(stale_fire, *asset.devices[1:])),)),
        ),
    )
    assert fresh.update(stale) == ()


def test_explicit_uncategorized_active_panel_alarm_produces_generic_critical():
    from halo_gtk.alarm.models import AlarmPhase

    value = snapshot(signal=AlarmSignal.UNKNOWN, complete=False)
    location = value.locations[0]
    asset = location.assets[0]
    panel = replace(asset.devices[0], phase=AlarmPhase.ALARMING)
    value = replace(
        value,
        locations=(
            replace(location, assets=(replace(asset, devices=(panel, *asset.devices[1:])),)),
        ),
    )
    reducer = AlarmEventReducer()
    events = reducer.update(value)
    assert len(events) == 1 and events[0].critical
    assert events[0].detail == "Alarm type unknown"
    assert reducer.update(value) == ()
