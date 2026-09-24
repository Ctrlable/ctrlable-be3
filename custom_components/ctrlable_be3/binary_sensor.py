"""Gateway connectivity.

Without this the gateway's device page is empty until a component is found,
which is exactly backwards: the moment you most need to know what is happening
is when nothing has appeared yet. A gateway that has not connected is the
normal first state, not a fault — most often another controller still holds it.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BE3ConfigEntry
from .const import CONF_HOST, CONF_MAC
from .entity import gateway_device_info
from .gateway import BE3Gateway, GatewayState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BE3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([BE3GatewayConnectivity(entry)])


class BE3GatewayConnectivity(BinarySensorEntity):
    """Whether the gateway currently has a session with us."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "gateway_connection"

    def __init__(self, entry: BE3ConfigEntry) -> None:
        self._entry = entry
        self._gateway: BE3Gateway = entry.runtime_data.gateway
        self._mac: str = entry.data[CONF_MAC]
        self._attr_unique_id = f"be3-{self._mac.replace(':', '').lower()}-connected"

    @property
    def is_on(self) -> bool:
        return self._gateway.connected

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Enough context to explain an empty gateway without reading logs."""
        state = self._gateway.state
        return {
            "components_found": len(state.device_addresses),
            "addresses": ", ".join(str(a) for a in state.device_addresses) or None,
            "gateway_address": state.gateway_address,
            "firmware": state.firmware,
            "listening_port": self._gateway.port,
        }

    @property
    def device_info(self):
        return gateway_device_info(
            self._mac, self._gateway.state, self._entry.data.get(CONF_HOST)
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._gateway.add_state_listener(self._handle_state))

    def _handle_state(self, state: GatewayState) -> None:
        self.async_write_ha_state()
