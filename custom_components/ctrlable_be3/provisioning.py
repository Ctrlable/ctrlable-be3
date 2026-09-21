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
    MAX_ADDRESS,
    MAX_BUTTONS,
    MAX_DEVICE_INDEX,
    MAX_NAME_BYTES,
    MIN_ADDRESS,
    MOLD_BUTTON,
    MOLD_DIMMER,
    MOLD_SHADE,
    build_clear_config,
    build_configure_buttons,
    build_identify,
    build_led,
    build_ota_start,
    build_set_address,
)

_LOGGER = logging.getLogger(__name__)

#: How long to wait for a heartbeat to confirm a re-address.
VERIFY_TIMEOUT = 20.0

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

    def __init__(self, gateway: BE3Gateway) -> None:
        self._gateway = gateway

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
        """Provision a component as a keypad.

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
        if require_known:
            self._require_known(address)

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

    async def clear_configuration(
        self, address: int, index: int, mold: str, name: str | None = None
    ) -> None:
        """Clear a component's configuration for the given module type."""
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
        self._require_known(old)

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

    def _require_known(self, address: int) -> None:
        state = self._gateway.state
        if not state.device_addresses:
            # Nothing has been reported yet; refusing would block a gateway
            # that simply has not sent its first heartbeat.
            return
        if address in state.device_addresses:
            return
        raise UnknownComponent(
            f"The gateway has not reported a component at address {address}. "
            f"Known components: "
            f"{', '.join(str(a) for a in state.device_addresses) or 'none'}"
        )
