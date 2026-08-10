"""Tests for the sanitized Ring power-inspection script."""

from __future__ import annotations

from scripts import inspect_ring_power


def test_public_properties_redact_sensitive_allowlisted_keys():
    class Device:
        address = "private address"
        name = "Front Door"

    properties = inspect_ring_power._public_properties(Device(), power_only=False)

    assert properties["address"] == "<redacted>"
    assert properties["name"] == "Front Door"
