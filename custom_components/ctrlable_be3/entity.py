"""Shared entity plumbing: device identity and gateway subscriptions."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, GATEWAY_MODEL, MANUFACTURER, PANEL_MODEL
from .devices import Component, ComponentKind, component_device_id, gateway_device_id
from .gateway import BE3Gateway, GatewayState


class BE3ComponentEntity(Entity):
    """An entity belonging to one component on the bus.

    Components hang off the gateway device, so a site reads as one gateway with
    its components beneath it.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, gateway: BE3Gateway, mac: str, component: Component
    ) -> None:
        self._gateway = gateway
        self._mac = mac
        self._component = component

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={
                (DOMAIN, component_device_id(self._mac, self._component.address))
            },
            name=component_name(self._component),
            manufacturer=MANUFACTURER,
            model=component_model(self._component),
            via_device=(DOMAIN, gateway_device_id(self._mac)),
            # Components of one physical panel share a label, which groups them
            # without pretending they are a single device.
            suggested_area=self._component.panel or None,
        )

    @property
    def available(self) -> bool:
        # Nothing can be sent or received while the gateway is away.
        return self._gateway.connected

    async def async_added_to_hass(self) -> None:
        """Follow the session so availability tracks the gateway."""
        self.async_on_remove(self._gateway.add_state_listener(self._handle_state))

    def _handle_state(self, state: GatewayState) -> None:
        self.async_write_ha_state()


def component_name(component: Component) -> str:
    """Label for a component's device.

    An unconfigured component is named after its address, because that is
    genuinely all anyone knows about it until it is identified.
    """
    if component.name:
        return component.name
    return f"BE3 component {component.address}"


def component_model(component: Component) -> str:
    if component.kind is ComponentKind.UNKNOWN:
        return "Unidentified component"
    return f"{PANEL_MODEL} ({component.kind.value})"


def gateway_device_info(mac: str, state: GatewayState, host: str | None) -> DeviceInfo:
    """Device entry for the gateway itself."""
    return DeviceInfo(
        identifiers={(DOMAIN, gateway_device_id(mac))},
        connections={(CONNECTION_NETWORK_MAC, mac)},
        name="BE3 gateway",
        manufacturer=MANUFACTURER,
        model=state.model or GATEWAY_MODEL,
        sw_version=state.firmware,
        configuration_url=f"http://{host}" if host else None,
    )
