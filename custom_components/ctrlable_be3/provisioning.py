"""Provisioning operations for components on the CoTP bus.

These are the calls that *change* hardware: they relabel a component's address,
rewrite its bindings, or start a firmware update. The gateway accepts all of
them silently and reports no result, so the guard rails live here.

Two rules shape this module:

* **Refuse what cannot be undone easily.** Re-addressing onto an occupied
  address, or configuring a component the gateway has never mentioned, is
  rejected rather than sent and hoped for.
* **Verify by observation.** The only feedback channel is the heartbeat's
  address list, so :meth:`Provisioner.set_address` watches it to confirm the
  change actually took.
"""

from __future__ import annotations

import asyncio
import logging

from .gateway import BE3Gateway, GatewayState
from .protocol import (
    DEFAULT_BUTTONS,
    MAX_ADDRESS,
    MAX_BUTTONS,
    MAX_DEVICE_INDEX,
    MAX_NAME_BYTES,
    MIN_ADDRESS,
    MOLD_BUTTON,
    MOLD_DIMMER,
    MOLD_SHADE,
    SHADE_BOTTOM_TO_TOP,
    build_blank_config,
    build_clear_config,
    build_configure_buttons,
    build_configure_dimmer,
    build_configure_shade,
    build_identify,
    build_led,
    build_ota_start,
    build_set_address,
)

_LOGGER = logging.getLogger(__name__)

#: How long to wait for a heartbeat to confirm a re-address.
VERIFY_TIMEOUT = 20.0

#: How long to allow for the restart that follows writing a personality.
#: Panels reboot when their configuration changes, which takes seconds, not
#: milliseconds.
RESTART_TIMEOUT = 45.0

#: How long a write waits for a component that is not on the bus. A
#: panel restart takes most of a minute, and writing is what causes one.
ADDRESS_WAIT = 90.0

KNOWN_MOLDS = (MOLD_BUTTON, MOLD_DIMMER, MOLD_SHADE)


class ProvisioningError(Exception):
    """Base class for provisioning failures."""


class NotConnected(ProvisioningError):
    """No gateway session is available."""


class InvalidRequest(ProvisioningError):
    """The request is malformed or outside supported limits."""


class AddressInUse(ProvisioningError):
    """The requested address already belongs to something on the bus."""


class UnknownComponent(ProvisioningError):
    """The gateway has never reported this address."""


class UpdateNotAvailable(ProvisioningError):
    """No firmware update is pending."""


class VerificationFailed(ProvisioningError):
    """The change was sent but the gateway never confirmed it."""


def _validate_link(link: str) -> None:
    """A link is two numbers the panel stores and quotes back to us."""
    parts = str(link).split(",")
    if len(parts) != 2 or not all(part.strip().isdigit() for part in parts):
        raise InvalidRequest(
            f"A link must be '<devId>,<devAtrId>', got {link!r}"
        )


def truncate_name(name: str) -> str:
    """Shorten a label to what the gateway will actually store.

    Cuts on a character boundary so a multi-byte character is never split into
    invalid UTF-8.
    """
    encoded = name.encode("utf-8")
    if len(encoded) <= MAX_NAME_BYTES:
        return name
    return encoded[:MAX_NAME_BYTES].decode("utf-8", "ignore")


class Provisioner:
    """High-level provisioning operations against one gateway."""

    def __init__(
        self, gateway: BE3Gateway, *, known_timeout: float = ADDRESS_WAIT
    ) -> None:
        self._gateway = gateway
        #: How long to wait for a component to reappear before refusing to
        #: write to it. Shortened in tests.
        self._known_timeout = known_timeout

    # ------------------------------------------------------------------
    # Non-destructive
    # ------------------------------------------------------------------

    async def identify(self, address: int) -> None:
        """Make a component flash so an installer can find it physically."""
        self._validate_address(address)
        await self._send(build_identify(address))

    async def set_led(self, address: int, button: int, on: bool) -> None:
        """Set a keypad backlight.

        The gateway never reports LED state, so callers must treat this as
        assumed rather than confirmed.
        """
        self._validate_address(address)
        self._validate_button(button)
        await self._send(build_led(address, button, on))

    # ------------------------------------------------------------------
    # Destructive
    # ------------------------------------------------------------------

    async def configure_buttons(
        self,
        address: int,
        index: int,
        count: int,
        name: str | None = None,
        *,
        require_known: bool = True,
    ) -> None:
        """Provision a component as a keypad

        **The panel restarts** when its configuration changes, so it drops off
        the bus for a few seconds and anything sent in that window is lost..

        This **overwrites** the component's existing bindings, including any
        written by another control system, so a component still configured as a
        dimmer stops reporting brightness and starts reporting button events.
        """
        self._validate_address(address)
        self._validate_index(index)
        if not 1 <= count <= MAX_BUTTONS:
            raise InvalidRequest(
                f"Button count must be between 1 and {MAX_BUTTONS}, got {count}"
            )
        if count > DEFAULT_BUTTONS:
            # The vendor documents 1-6. Larger panels may well work — their own
            # driver hints at 8 — but nothing here has been tested above six.
            _LOGGER.warning(
                "Provisioning %s buttons on component %s; the vendor documents "
                "at most %s, so treat this as untested",
                count,
                address,
                DEFAULT_BUTTONS,
            )
        if require_known:
            await self._await_known(address)

        _LOGGER.info(
            "Configuring component %s as a %s-button keypad (slot %s)",
            address,
            count,
            index,
        )
        await self._send(
            build_configure_buttons(
                address, index, count, truncate_name(name) if name else None
            )
        )

    async def configure_dimmer(
        self,
        address: int,
        index: int,
        brightness_link: str,
        *,
        name: str | None = None,
        colour_link: str | None = None,
        require_known: bool = True,
    ) -> None:
        """Provision a component as a dimmer

        **The panel restarts** when its configuration changes, so it drops off
        the bus for a few seconds and anything sent in that window is lost. bound to a link.

        **Overwrites** whatever the component was doing before, exactly as
        making it a keypad does.
        """
        self._validate_address(address)
        self._validate_index(index)
        _validate_link(brightness_link)
        if colour_link:
            _validate_link(colour_link)
        if require_known:
            await self._await_known(address)

        _LOGGER.info(
            "Configuring component %s as a dimmer on link %s (slot %s)",
            address,
            brightness_link,
            index,
        )
        await self._send(
            build_configure_dimmer(
                address,
                index,
                brightness_link,
                name=truncate_name(name) if name else None,
                colour_link=colour_link,
            )
        )

    async def configure_shade(
        self,
        address: int,
        index: int,
        level_link: str,
        *,
        name: str | None = None,
        direction: int = SHADE_BOTTOM_TO_TOP,
        require_known: bool = True,
    ) -> None:
        """Provision a component as a shade controller

        **The panel restarts** when its configuration changes, so it drops off
        the bus for a few seconds and anything sent in that window is lost. bound to a link."""
        self._validate_address(address)
        self._validate_index(index)
        _validate_link(level_link)
        if direction not in (1, 2, 3, 4):
            raise InvalidRequest(f"Unknown shade orientation {direction}")
        if require_known:
            await self._await_known(address)

        _LOGGER.info(
            "Configuring component %s as a shade on link %s (slot %s)",
            address,
            level_link,
            index,
        )
        await self._send(
            build_configure_shade(
                address,
                index,
                level_link,
                name=truncate_name(name) if name else None,
                direction=direction,
            )
        )

    async def clear_configuration(
        self, address: int, index: int, mold: str, name: str | None = None
    ) -> None:
        """Clear a page's configuration for the given module type.

        Unlike writing one, this does **not** restart the panel: observed on
        firmware 0.10, a component kept polling straight through its
        neighbours being cleared. So callers need only a moment between
        clears, not a reboot.
        """
        self._validate_address(address)
        self._validate_index(index)
        if mold not in KNOWN_MOLDS:
            raise InvalidRequest(f"Unknown module type {mold!r}")

        _LOGGER.info("Clearing %s configuration on component %s", mold, address)
        await self._send(
            build_clear_config(
                address, index, mold, truncate_name(name) if name else None
            )
        )

    async def blank_configuration(
        self, address: int, index: int, mold: str, name: str | None = None
    ) -> None:
        """Overwrite a page with empty links, the nearest thing to deleting it.

        Restarts the panel, as every write does. Worth it: this is the only
        thing that actually stops a leftover page, since clearing one does
        nothing on this firmware.
        """
        self._validate_address(address)
        self._validate_index(index)

        _LOGGER.info(
            "Blanking %s page %s on component %s: the page stays on the panel "
            "but points at nothing",
            mold,
            index,
            address,
        )
        await self._send(
            build_blank_config(
                address, index, mold, truncate_name(name) if name else None
            )
        )

    async def set_address(
        self,
        old: int,
        new: int,
        *,
        verify: bool = True,
        timeout: float = VERIFY_TIMEOUT,
    ) -> None:
        """Move a component to a different address on the bus.

        Rejected when the target address is already taken, which would leave two
        components answering to the same address and need a site visit to sort
        out.
        """
        self._validate_address(old)
        self._validate_address(new)
        if old == new:
            raise InvalidRequest("Source and target addresses are the same")

        state = self._gateway.state
        if new in state.device_addresses:
            raise AddressInUse(f"Address {new} is already used by another component")
        if new == state.gateway_address:
            raise AddressInUse(f"Address {new} belongs to the gateway itself")
        await self._await_known(old)

        _LOGGER.info("Re-addressing component %s to %s", old, new)
        await self._send(build_set_address(old, new))

        if not verify:
            return
        try:
            await self.wait_for_address(new, timeout=timeout)
        except TimeoutError:
            raise VerificationFailed(
                f"Component {old} did not reappear at address {new} within "
                f"{timeout:.0f}s; it may need a power cycle before it reports in"
            ) from None

    async def start_update(self) -> None:
        """Begin a firmware update.

        Only valid while a heartbeat reports one waiting; the gateway ignores
        the command otherwise, which would look like a silent failure.
        """
        if not self._gateway.state.ota_pending:
            raise UpdateNotAvailable("The gateway is not reporting a pending update")
        _LOGGER.info("Starting BE3 firmware update")
        await self._send(build_ota_start())

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    async def wait_for_restart(
        self, address: int, *, timeout: float = RESTART_TIMEOUT
    ) -> bool:
        """Wait for a component to drop off the bus and come back.

        Writing a personality restarts the panel. That restart is the only
        evidence the write was accepted — the gateway acknowledges nothing — so
        watching the address disappear and return confirms it.

        Returns True when a full drop and return was seen. False means the
        restart was not observed, which is not a failure: heartbeats arrive
        every few seconds, and a panel that restarts between two of them never
        appears to have left.
        """
        try:
            async with asyncio.timeout(timeout):
                await self._wait_until(lambda state: address not in state.device_addresses)
        except TimeoutError:
            return False

        _LOGGER.debug("Component %s left the bus; waiting for it to return", address)
        try:
            await self.wait_for_address(address, timeout=timeout)
        except TimeoutError:
            raise VerificationFailed(
                f"Component {address} restarted but has not come back within "
                f"{timeout:.0f}s; it may need power cycling"
            ) from None
        return True

    async def _wait_until(self, predicate) -> None:
        """Wait for gateway state to satisfy a predicate."""
        if predicate(self._gateway.state):
            return
        done = asyncio.Event()

        def _watch(state: GatewayState) -> None:
            if predicate(state):
                done.set()

        unsubscribe = self._gateway.add_state_listener(_watch)
        try:
            if predicate(self._gateway.state):
                return
            await done.wait()
        finally:
            unsubscribe()

    async def wait_for_address(
        self, address: int, *, timeout: float = VERIFY_TIMEOUT
    ) -> None:
        """Wait until a heartbeat lists ``address`` among the components."""
        if address in self._gateway.state.device_addresses:
            return

        seen = asyncio.Event()

        def _watch(state: GatewayState) -> None:
            if address in state.device_addresses:
                seen.set()

        unsubscribe = self._gateway.add_state_listener(_watch)
        try:
            # Re-check: the address may have arrived while we were subscribing.
            if address in self._gateway.state.device_addresses:
                return
            async with asyncio.timeout(timeout):
                await seen.wait()
        finally:
            unsubscribe()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _send(self, payload: dict) -> None:
        if not await self._gateway.send(payload):
            raise NotConnected("No BE3 gateway is connected")

    def _validate_address(self, address: int) -> None:
        if not isinstance(address, int) or isinstance(address, bool):
            raise InvalidRequest(f"Address must be a whole number, got {address!r}")
        if not MIN_ADDRESS <= address <= MAX_ADDRESS:
            raise InvalidRequest(
                f"Address must be between {MIN_ADDRESS} and {MAX_ADDRESS}, "
                f"got {address}"
            )

    def _validate_index(self, index: int) -> None:
        if not 1 <= index <= MAX_DEVICE_INDEX:
            raise InvalidRequest(
                f"Device slot must be between 1 and {MAX_DEVICE_INDEX}, got {index}"
            )

    def _validate_button(self, button: int) -> None:
        if not 1 <= button <= MAX_BUTTONS:
            raise InvalidRequest(
                f"Button must be between 1 and {MAX_BUTTONS}, got {button}"
            )

    async def _await_known(self, address: int) -> None:
        """Wait for a component to be present, then allow the write.

        A panel that is restarting is missing from the heartbeat for the best
        part of a minute, and writing is what restarts it — so anyone changing
        two pages in a row asks about an address that is temporarily gone.
        Refusing outright turned that into "Could not configure component 24"
        on a panel that was simply on its way back.
        """
        state = self._gateway.state
        if not state.device_addresses:
            # Nothing has been reported yet; refusing would block a gateway
            # that simply has not sent its first heartbeat.
            return
        if address in state.device_addresses:
            return

        _LOGGER.info(
            "Component %s is not on the bus right now; waiting up to %.0fs for "
            "it, since a panel that was just written to is restarting",
            address,
            self._known_timeout,
        )
        try:
            await self.wait_for_address(address, timeout=self._known_timeout)
        except TimeoutError:
            raise UnknownComponent(
                f"The gateway has not reported a component at address "
                f"{address} within {self._known_timeout:.0f}s. Known "
                f"components: "
                f"{', '.join(str(a) for a in self._gateway.state.device_addresses) or 'none'}"
            ) from None
