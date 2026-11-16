"""Explicit qualified receipts for non-privacy media lifecycle fixtures."""

from types import SimpleNamespace

from halo_gtk.camera_privacy import CameraPrivacy, privacy_time, ring_privacy_receipt


def grant(client, *devices):
    if type(client) is object:
        client = SimpleNamespace()
    for index, device in enumerate(devices, 1):
        if not hasattr(device, "id"):
            device.id = index
        device._attrs = {
            "settings": {
                "live_view_disabled": False,
                "motion_detection_enabled": True,
                "enable_audio_recording": True,
            }
        }
    if not hasattr(client, "camera_privacy"):
        client.camera_privacy = CameraPrivacy()
    if not hasattr(client, "_preview_is_current"):
        client._preview_is_current = lambda: True
    ring = getattr(client, "_ring", None)
    if isinstance(ring, SimpleNamespace) and not hasattr(ring, "devices"):
        ring.devices = lambda: SimpleNamespace(all_devices=list(devices))
    client.camera_privacy.observe(ring_privacy_receipt(devices, privacy_time()))
    return client


def grant_current(monkeypatch, device, client=None):
    from halo_gtk import ring_client

    client = grant(client if client is not None else SimpleNamespace(), device)
    monkeypatch.setattr(ring_client, "get_client", lambda: client)
    return client.camera_privacy.acquire(device, "preview")
