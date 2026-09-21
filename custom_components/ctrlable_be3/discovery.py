"""Finding gateways on the network.

Uses the legacy ``Z-SEARCH`` probe rather than the ``C4Z-SEARCH`` one: the
legacy probe asks a gateway to describe itself over UDP, while the other asks
it to *connect*, which would steal it from whichever controller currently owns
it. Discovery must be safe to run at any time, including against a site still
running Control4.

No Home Assistant imports, so this is testable against a fake responder.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from collections.abc import Sequence
from dataclasses import dataclass

from .protocol import DISCOVERY_PORT, LEGACY_SEARCH, parse_discovery_reply

_LOGGER = logging.getLogger(__name__)

DISCOVERY_TIMEOUT = 3.0

#: Gateways identify themselves with this name.
GATEWAY_NAME = "ESPSUBLIMEBE3"


@dataclass(frozen=True)
class DiscoveredGateway:
    host: str
    mac: str
    model: str | None = None
    name: str | None = None
    version: str | None = None

    @property
    def title(self) -> str:
        """Label for a config flow, distinct when several are on site."""
        return f"BE3 gateway ({self.host})"


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.replies: dict[str, DiscoveredGateway] = {}

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        fields = parse_discovery_reply(data)
        mac = fields.get("SN") or fields.get("MOD", "").removeprefix("ESPSUBLIME-")
        name = fields.get("NAME")
        if not mac or name != GATEWAY_NAME:
            # Something else answered on this port; ignore it rather than
            # offering the user a device we cannot talk to.
            _LOGGER.debug("Ignoring non-BE3 reply from %s: %s", addr[0], fields)
            return
        gateway = DiscoveredGateway(
            host=addr[0],
            mac=mac,
            model=fields.get("MOD"),
            name=name,
            version=fields.get("VER"),
        )
        # Replies can arrive more than once; last one wins, keyed by identity.
        self.replies[gateway.mac] = gateway


async def async_discover(
    *,
    timeout: float = DISCOVERY_TIMEOUT,
    targets: Sequence[str] = ("255.255.255.255",),
    port: int = DISCOVERY_PORT,
) -> list[DiscoveredGateway]:
    """Probe for gateways and collect replies for ``timeout`` seconds.

    ``targets`` normally holds the broadcast address; pass a specific host to
    confirm one gateway without disturbing the rest of the network.
    """
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        _DiscoveryProtocol,
        local_addr=("0.0.0.0", 0),
        allow_broadcast=True,
    )
    assert isinstance(protocol, _DiscoveryProtocol)

    try:
        with contextlib.suppress(OSError):
            transport.get_extra_info("socket").setsockopt(
                socket.SOL_SOCKET, socket.SO_BROADCAST, 1
            )
        for target in targets:
            with contextlib.suppress(OSError):
                transport.sendto(LEGACY_SEARCH, (target, port))
        await asyncio.sleep(timeout)
    finally:
        transport.close()

    found = sorted(protocol.replies.values(), key=lambda item: item.host)
    _LOGGER.debug("Discovery found %d gateway(s)", len(found))
    return found


async def async_probe(
    host: str, *, timeout: float = DISCOVERY_TIMEOUT, port: int = DISCOVERY_PORT
) -> DiscoveredGateway | None:
    """Ask one host to identify itself. Returns None if it does not answer."""
    for gateway in await async_discover(timeout=timeout, targets=(host,), port=port):
        if gateway.host == host:
            return gateway
    return None
