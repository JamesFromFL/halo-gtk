"""Synthetic DNS parser and aioice contracts; no sockets, Ring, or devices."""

import asyncio

import dns.exception
import dns.message
import dns.name
import dns.rdata
import pytest
from aioice.mdns import MDNS_ADDRESS, MDNS_PORT, MDnsProtocol


class _Wire:
    def __init__(self):
        self.peer = None
        self.messages = []

    def sendto(self, packet, address):
        assert address == (MDNS_ADDRESS, MDNS_PORT)
        self.messages.append(dns.message.from_wire(packet))
        asyncio.get_running_loop().call_soon(
            self.peer.datagram_received, packet, ("192.0.2.1", MDNS_PORT)
        )


def _peers():
    requests, responses = _Wire(), _Wire()
    client, publisher = MDnsProtocol(requests), MDnsProtocol(responses)
    requests.peer, responses.peer = publisher, client
    return client, publisher, requests, responses


@pytest.mark.parametrize("address", ["192.0.2.9", "2001:db8::9"])
async def test_aioice_resolves_published_mdns_after_malformed_packet(address):
    client, publisher, requests, responses = _peers()
    try:
        publisher.datagram_received(b"bad", ("192.0.2.1", MDNS_PORT))
        client.datagram_received(b"bad", ("192.0.2.1", MDNS_PORT))
        await publisher.publish("halo-fixture.local", address)
        assert await client.resolve("halo-fixture.local", timeout=1) == address
        assert len(requests.messages) == len(responses.messages) == 1
        assert requests.messages[0].id == responses.messages[0].id == 0
        assert client.queries == {}
    finally:
        client.connection_lost(None)
        publisher.connection_lost(None)


async def test_aioice_mdns_timeout_cleans_up_pending_query():
    client, publisher, requests, responses = _peers()
    try:
        assert await client.resolve("missing.local", timeout=0.01) is None
        assert len(requests.messages) == 1
        assert not responses.messages
        assert client.queries == {}
    finally:
        client.connection_lost(None)
        publisher.connection_lost(None)


def _compressed_name_chain(hops):
    wire = bytearray(b"\x00")
    offset = 0
    for _ in range(hops):
        pointer = 0xC000 | offset
        offset = len(wire)
        wire.extend(pointer.to_bytes(2, "big"))
    return bytes(wire), offset


def test_dns_name_parser_accepts_bounded_compression_and_rejects_excessive_hops():
    wire, offset = _compressed_name_chain(16)
    assert dns.name.from_wire(wire, offset) == (dns.name.root, 2)
    wire, offset = _compressed_name_chain(17)
    with pytest.raises(dns.exception.FormError):
        dns.name.from_wire(wire, offset)


def test_dns_wks_parser_rejects_service_numbers_above_protocol_limit():
    valid = dns.rdata.from_text("IN", "WKS", "192.0.2.1 udp 65535")
    assert len(valid.bitmap) == 8192
    with pytest.raises(dns.exception.SyntaxError):
        dns.rdata.from_text("IN", "WKS", "192.0.2.1 udp 65536")
