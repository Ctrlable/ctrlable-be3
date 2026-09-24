"""Button events from SUBLIME keypads."""

from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BE3ConfigEntry, ManagedComponent
from .const import CONF_MAC, SIGNAL_COMPONENTS_LEARNED
from .devices import Component, ComponentKind, button_unique_id, value_unique_id
from .entity import BE3ComponentEntity
from .gateway import BE3Gateway, ButtonEvent
from .links import describe
from .protocol import ButtonAction, Message, ValueAction, ValueRequest

#: Every gesture the tracker can produce, in the order they occur.
EVENT_TYPES = [action.value for action in ButtonAction]

#: What a dimmer or shade component can report. A keypad reports none of
#: these, and a dimmer reports none of the button gestures.
DIMMER_EVENT_TYPES = [
    ValueAction.ON.value,
    ValueAction.OFF.value,
    ValueAction.BRIGHTNESS.value,
    ValueAction.COLOUR_TEMPERATURE.value,
]
SHADE_EVENT_TYPES = [
    ValueAction.OPEN.value,
    ValueAction.CLOSE.value,
    ValueAction.STOP.value,
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BE3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one event entity per configured button."""
    runtime = entry.runtime_data
    mac = entry.data[CONF_MAC]
    via = runtime.gateway_device

    #: What has been created already, so a component that learns a new button
    #: gains an entity for it without the others being added twice.
    added: set[tuple[str, str]] = set()

    @callback
    def _async_add_missing() -> None:
        for managed in runtime.components:
            component = managed.component
            if component.ignored:
                continue
            # Entities are added per subentry so each lands on its own device.
            if component.kind is ComponentKind.BUTTON:
                new = [
                    button
                    for button in component.button_range
                    if (managed.subentry_id, f"button{button}") not in added
                ]
                if not new:
                    continue
                added.update((managed.subentry_id, f"button{b}") for b in new)
                async_add_entities(
                    [
                        BE3ButtonEvent(runtime.gateway, mac, component, button, via)
                        for button in new
                    ],
                    config_subentry_id=managed.subentry_id,
                )
            elif component.kind in (ComponentKind.DIMMER, ComponentKind.SHADE):
                # A panel that is not a keypad reports no button events at all:
                # its presses arrive as value writes, so this is where they
                # surface.
                if (managed.subentry_id, "value") in added:
                    continue
                added.add((managed.subentry_id, "value"))
                async_add_entities(
                    [BE3ValueEvent(runtime.gateway, mac, component, via)],
                    config_subentry_id=managed.subentry_id,
                )

    _async_add_missing()
    # A component that identifies itself later — a keypad whose buttons are
    # being pressed for the first time — gets its entities now rather than at
    # the next restart, because reloading the entry to create them is what used
    # to stop the gateway relaying the bus.
    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_COMPONENTS_LEARNED.format(entry_id=entry.entry_id),
            _async_add_missing,
        )
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
        via_device_id: str | None = None,
    ) -> None:
        super().__init__(gateway, mac, component, via_device_id)
        self._button = button
        self._attr_unique_id = button_unique_id(mac, component.address, component.slot, button)
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


class BE3ValueEvent(BE3ComponentEntity, EventEntity):
    """What a dimmer or shade component was asked to do.

    Raise and lower arrive here rather than as button gestures: the panel
    reports the brightness it wants, not the press that caused it.
    """

    def __init__(
        self,
        gateway: BE3Gateway,
        mac: str,
        component: Component,
        via_device_id: str | None = None,
    ) -> None:
        super().__init__(gateway, mac, component, via_device_id)
        self._attr_unique_id = value_unique_id(mac, component.address, component.slot)
        self._attr_translation_key = "value"
        self._attr_event_types = (
            DIMMER_EVENT_TYPES
            if component.kind is ComponentKind.DIMMER
            else SHADE_EVENT_TYPES
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._gateway.add_message_listener(self._handle_message))

    @callback
    def _handle_message(self, message: Message) -> None:
        if not isinstance(message, ValueRequest):
            return
        if message.address != self._component.address:
            return
        described = describe(message, self._component.slot)
        if described is None:
            return
        action, attributes = described
        if action.value not in self._attr_event_types:
            return
        self._trigger_event(action.value, attributes)
        self.async_write_ha_state()
