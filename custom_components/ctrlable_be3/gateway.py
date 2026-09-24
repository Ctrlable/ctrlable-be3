"""Connection manager for a single BE3 gateway.

Like :mod:`protocol`, this module avoids Home Assistant imports so it can be
tested on loopback sockets.

The gateway is the TCP client, so this class runs a server and invites the
gateway to connect by broadcasting a search datagram while nothing is attached.
Once connected it tracks liveness, exposes gateway state, and turns raw button
reports into gestures.

Two behaviours come from observing real hardware (docs/BE3-protocol-findings.md):

* A gateway may open **two connections at once**, so a second connection from
  the same peer replaces the first rather than being rejected as an intruder.
* A gateway sends no keepalive of its own, so a half-dead socket is only
  detected by the idle timeout on our side.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .protocol import (
    DEFAULT_TCP_PORT,
    DISCOVERY_PORT,
    IDLE_TIMEOUT,
    ButtonAction,
    ButtonReport,
    ButtonTracker,
    DeviceInfo,
    Heartbeat,
    LineReader,
    Message,
    build_search,
    encode,
    parse_message,
)

_LOGGER = logging.getLogger(__name__)

SEARCH_INTERVAL = 5.0

#: Read size for the TCP stream. Messages are far smaller than this.
_READ_SIZE = 4096

#: How long a connected gateway may go without a heartbeat before we say
#: so. Heartbeats arrive every ~5s, so this is many missed in a row.
BUS_SILENCE_TIMEOUT = 60.0

#: Idle drops in a row that mean the gateway's bus loop has stopped rather
#: than the network having a bad day.
WEDGE_DROPS = 3


@dataclass(frozen=True)
class GatewayState:
    """Everything known about the gateway, refreshed from its heartbeats."""

    connected: bool = False
    host: str | None = None
    firmware: str | None = None
    model: str | None = None
    #: The gateway's own COTP address, which it appends to its address list.
    gateway_address: int | None = None
    #: Addresses of the components on the bus.
    device_addresses: tuple[int, ...] = ()
    ota_pending: bool = False
    ota_state: str | None = None


@dataclass(frozen=True)
class ButtonEvent:
    """A gesture derived from the gateway's raw button reports."""

    address: int
    button: int
    action: ButtonAction


MessageListener = Callable[[Message], None]
StateListener = Callable[[GatewayState], None]
ButtonListener = Callable[[ButtonEvent], None]


class BE3Gateway:
    """Owns the discovery broadcast, the TCP server and the gateway session."""

    def __init__(
        self,
        *,
        port: int = DEFAULT_TCP_PORT,
        expected_host: str | None = None,
        bind_host: str = "0.0.0.0",
        broadcast_addresses: Sequence[str] = ("255.255.255.255",),
        discovery_port: int = DISCOVERY_PORT,
        search_interval: float = SEARCH_INTERVAL,
        idle_timeout: float = IDLE_TIMEOUT,
    ) -> None:
        #: Port advertised in search datagrams. 0 lets the OS choose, which is
        #: only useful in tests; read :attr:`port` back after :meth:`start`.
        self._configured_port = port
        self._bind_host = bind_host
        self._broadcast_addresses = tuple(broadcast_addresses)
        self._discovery_port = discovery_port
        self._search_interval = search_interval
        self._idle_timeout = idle_timeout

        #: When set, only this address may connect. Left unset, the first
        #: gateway to answer claims the session and is remembered.
        self.expected_host = expected_host

        self._state = GatewayState()
        self._server: asyncio.AbstractServer | None = None
        self._port: int | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._session: int = 0
        self._last_seen: float | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._buttons = ButtonTracker()
        self._send_lock = asyncio.Lock()
        self._last_heartbeat: float | None = None
        self._bus_warned = False
        self._idle_drops = 0
        self._wedge_warned = False
        self._heartbeats_this_session = 0

        self._message_listeners: list[MessageListener] = []
        self._state_listeners: list[StateListener] = []
        self._button_listeners: list[ButtonListener] = []

    # ------------------------------------------------------------------
    # Public surface
    # ------------------------------------------------------------------

    @property
    def state(self) -> GatewayState:
        return self._state

    @property
    def connected(self) -> bool:
        return self._state.connected

    @property
    def port(self) -> int:
        """The port actually bound, available once started."""
        return self._port if self._port is not None else self._configured_port

    def add_message_listener(self, listener: MessageListener) -> Callable[[], None]:
        return _subscribe(self._message_listeners, listener)

    def add_state_listener(self, listener: StateListener) -> Callable[[], None]:
        return _subscribe(self._state_listeners, listener)

    def add_button_listener(self, listener: ButtonListener) -> Callable[[], None]:
        return _subscribe(self._button_listeners, listener)

    async def start(self) -> None:
        """Bind the server and begin inviting the gateway to connect."""
        if self._server is not None:
            return

        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self._bind_host,
            port=self._configured_port,
            reuse_address=True,
        )
        sockets = self._server.sockets or ()
        if sockets:
            self._port = sockets[0].getsockname()[1]

        self._spawn(self._discovery_loop(), "discovery")
        self._spawn(self._idle_watchdog(), "watchdog")
        self._spawn(self._bus_watchdog(), "bus-watchdog")
        _LOGGER.debug("BE3 gateway listening on %s:%s", self._bind_host, self.port)

    async def stop(self) -> None:
        """Close the session and stop all background work."""
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

        await self._close_session(notify=False)

        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        self._port = None

    async def send(self, payload: dict[str, Any]) -> bool:
        """Send one message. Returns False when no gateway is attached."""
        writer = self._writer
        if writer is None or writer.is_closing():
            _LOGGER.debug("Dropping message, no gateway connected: %s", payload)
            return False

        data = encode(payload)
        async with self._send_lock:
            try:
                # Logged as bytes: half of what this protocol does wrong is
                # about a value's type, and a dict repr hides the difference
                # between 1 and "1".
                _LOGGER.debug("BE3 <- %s", data)
                writer.write(data)
                await writer.drain()
            except (OSError, ConnectionError) as err:
                _LOGGER.debug("Send failed: %s", err)
                await self._close_session()
                return False
        return True

    async def search_now(self) -> None:
        """Broadcast a search datagram immediately.

        Useful after a configuration change, when waiting up to a full interval
        would feel unresponsive.
        """
        await asyncio.get_running_loop().run_in_executor(None, self._broadcast)

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        host = peer[0] if peer else None

        if not self._accepts(host):
            _LOGGER.warning("Rejecting BE3 connection from unexpected host %s", host)
            await _close_writer(writer)
            return

        if self._writer is not None and not self._writer.is_closing():
            # The gateway opens a second socket rather than reusing the first.
            # The newest one is the live one, so retire the old quietly.
            _LOGGER.debug("Replacing existing connection from %s", host)
            await _close_writer(self._writer)

        self._session += 1
        session = self._session
        self._writer = writer
        self._buttons.reset()
        self._touch()
        self._update_state(connected=True, host=host)
        _LOGGER.info("BE3 gateway connected from %s", host)

        line_reader = LineReader()
        try:
            while True:
                data = await reader.read(_READ_SIZE)
                if not data:
                    break
                self._touch()
                for payload in line_reader.feed(data):
                    self._dispatch(parse_message(payload))
        except (OSError, ConnectionError) as err:
            _LOGGER.debug("BE3 connection error from %s: %s", host, err)
        finally:
            await _close_writer(writer)
            # A replaced connection must not clear the session that replaced it.
            if session == self._session:
                await self._close_session()

    def _accepts(self, host: str | None) -> bool:
        if self.expected_host is None:
            return True
        return host == self.expected_host

    async def _close_session(self, *, notify: bool = True) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            await _close_writer(writer)
        self._last_seen = None
        # Counted per session: one heartbeat on connect proves nothing.
        self._heartbeats_this_session = 0
        self._buttons.reset()
        if self._state.connected:
            _LOGGER.info("BE3 gateway disconnected")
        if notify:
            self._update_state(connected=False, host=None)
        else:
            self._state = GatewayState()

    def _touch(self) -> None:
        """Record proof of life; any inbound byte counts."""
        self._last_seen = time.monotonic()

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    async def _discovery_loop(self) -> None:
        """Invite the gateway to connect while nothing is attached."""
        loop = asyncio.get_running_loop()
        while True:
            if not self.connected:
                with contextlib.suppress(OSError):
                    await loop.run_in_executor(None, self._broadcast)
            await asyncio.sleep(self._search_interval)

    def _broadcast(self) -> None:
        datagram = build_search(self.port)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for address in self._broadcast_addresses:
                with contextlib.suppress(OSError):
                    udp.sendto(datagram, (address, self._discovery_port))

    async def _bus_watchdog(self) -> None:
        """Warn when the gateway is connected but its bus has gone quiet.

        The firmware runs its bus polling in a loop that stops for good if
        anything in it throws, while the TCP and UDP sides stay up. The gateway
        then answers discovery and holds its connection while reporting nothing
        at all — indistinguishable from a quiet site unless someone is looking
        for heartbeats specifically. Only a power cycle brings it back.
        """
        while True:
            await asyncio.sleep(BUS_SILENCE_TIMEOUT / 2)
            if not self.connected or self._last_heartbeat is None:
                continue
            silence = time.monotonic() - self._last_heartbeat
            if silence < BUS_SILENCE_TIMEOUT or self._bus_warned:
                continue
            self._bus_warned = True
            _LOGGER.warning(
                "The BE3 is connected but has sent no heartbeat for %.0fs. Its "
                "firmware polls the bus in a loop that stops permanently if it "
                "errors, leaving the network side running — so buttons and "
                "panels will report nothing until the gateway is power-cycled.",
                silence,
            )

    async def _idle_watchdog(self) -> None:
        """Drop a session that has gone quiet.

        The gateway never pings, so a socket that survives a network partition
        would otherwise look healthy forever.
        """
        interval = max(self._idle_timeout / 4, 0.05)
        while True:
            await asyncio.sleep(interval)
            last_seen = self._last_seen
            if last_seen is None or not self.connected:
                continue
            if time.monotonic() - last_seen > self._idle_timeout:
                _LOGGER.warning(
                    "No traffic from BE3 for %.1fs, dropping connection",
                    self._idle_timeout,
                )
                self._idle_drops += 1
                self._warn_wedged()
                await self._close_session()

    def _warn_wedged(self) -> None:
        """Name the failure when a gateway accepts sockets but says nothing.

        The firmware has a state where its bus loop stops while its network
        stack keeps running: it accepts a connection, sends one heartbeat, then
        goes quiet, and we drop it and take it again forever. Nothing here
        recovers it — only power. Said once, with the remedy, because the loop
        otherwise reads as an ordinary network problem for as long as anyone
        cares to watch it.
        """
        if self._idle_drops < WEDGE_DROPS or self._wedge_warned:
            return
        self._wedge_warned = True
        _LOGGER.error(
            "The BE3 gateway has accepted %s connections in a row and then "
            "gone silent, which means its bus loop has stopped while its "
            "network stack keeps answering. Nothing in software recovers this: "
            "power cycle the gateway. Devices on the bus keep working from "
            "their own buttons in the meantime.",
            self._idle_drops,
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(self, message: Message) -> None:
        if isinstance(message, Heartbeat):
            # Logged only when the bus changes: they arrive every few seconds
            # and would otherwise bury everything else.
            if message.device_addresses != self._state.device_addresses:
                _LOGGER.debug("BE3 -> %s", message)
        else:
            _LOGGER.debug("BE3 -> %s", message)

        if isinstance(message, Heartbeat):
            self._last_heartbeat = time.monotonic()
            self._bus_warned = False
            # Two heartbeats in one session is a bus loop that is running: a
            # wedged gateway manages exactly one, on connect.
            if self._heartbeats_this_session:
                self._idle_drops = 0
                self._wedge_warned = False
            self._heartbeats_this_session += 1
            self._update_state(
                firmware=message.version,
                gateway_address=message.gateway_address,
                device_addresses=message.device_addresses,
                ota_pending=message.ota_pending,
                ota_state=message.ota_state,
            )
        elif isinstance(message, DeviceInfo):
            self._update_state(model=message.model, firmware=message.version)
        elif isinstance(message, ButtonReport):
            for action in self._buttons.feed(message):
                event = ButtonEvent(message.address, message.button, action)
                _notify(self._button_listeners, event, "button listener")

        _notify(self._message_listeners, message, "message listener")

    def _update_state(self, **changes: Any) -> None:
        # Heartbeats repeat constantly; only wake listeners on real changes.
        current = self._state
        updated = GatewayState(
            connected=changes.get("connected", current.connected),
            host=changes.get("host", current.host),
            firmware=changes.get("firmware", current.firmware) or current.firmware,
            model=changes.get("model", current.model) or current.model,
            gateway_address=changes.get("gateway_address", current.gateway_address),
            device_addresses=changes.get(
                "device_addresses", current.device_addresses
            ),
            ota_pending=changes.get("ota_pending", current.ota_pending),
            ota_state=changes.get("ota_state", current.ota_state),
        )
        if updated == current:
            return
        self._state = updated
        _notify(self._state_listeners, updated, "state listener")

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=f"be3-{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def _subscribe(listeners: list[Any], listener: Any) -> Callable[[], None]:
    listeners.append(listener)

    def unsubscribe() -> None:
        with contextlib.suppress(ValueError):
            listeners.remove(listener)

    return unsubscribe


def _notify(listeners: Iterable[Any], payload: Any, what: str) -> None:
    for listener in list(listeners):
        try:
            listener(payload)
        except Exception:  # noqa: BLE001 - one bad listener must not stop the rest
            _LOGGER.exception("Error in BE3 %s", what)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    if writer.is_closing():
        return
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
