"""Button events from SUBLIME keypads."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BE3ConfigEntry, ManagedComponent
from .const import CONF_MAC
from .devices import Component, ComponentKind, button_unique_id
from .entity import BE3ComponentEntity
from .gateway import BE3Gateway, ButtonEvent
from .protocol import ButtonAction

#: Every gesture the tracker can produce, in the order they occur.
EVENT_TYPES = [action.value for action in ButtonAction]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BE3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one event entity per configured button."""
    runtime = entry.runtime_data
    mac = entry.data[CONF_MAC]

    for managed in runtime.components:
        component = managed.component
        if component.kind is not ComponentKind.BUTTON:
            continue
        # Entities are added per subentry so each lands on its own device.
        async_add_entities(
            [
                BE3ButtonEvent(runtime.gateway, mac, component, button)
                for button in component.button_range
            ],
            config_subentry_id=managed.subentry_id,
        )


class BE3ButtonEvent(BE3ComponentEntity, EventEntity):
    """One button on a keypad.

    An event entity rather than a binary sensor: presses are momentary, and a
    long press is a distinct gesture rather than a longer state.
    """

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = EVENT_TYPES

    def __init__(
        self,
        gateway: BE3Gateway,
        mac: str,
        component: Component,
        button: int,
    ) -> None:
        super().__init__(gateway, mac, component)
        self._button = button
        self._attr_unique_id = button_unique_id(mac, component.address, button)
        self._attr_translation_key = "button"
        self._attr_translation_placeholders = {"number": str(button)}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._gateway.add_button_listener(self._handle_button))

    @callback
    def _handle_button(self, event: ButtonEvent) -> None:
        if event.address != self._component.address or event.button != self._button:
            return
        self._trigger_event(event.action.value)
        self.async_write_ha_state()
