"""Keypad backlight switches.

The gateway accepts LED commands but never reports LED state, so these are
assumed-state switches: they show what was last commanded, which may drift if
something else drives the same panel.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BE3ConfigEntry, ManagedComponent
from .const import CONF_MAC, SIGNAL_COMPONENTS_LEARNED
from .devices import Component, ComponentKind, led_unique_id
from .entity import BE3ComponentEntity
from .gateway import BE3Gateway
from .provisioning import Provisioner, ProvisioningError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BE3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one switch per keypad backlight."""
    runtime = entry.runtime_data
    mac = entry.data[CONF_MAC]
    via = runtime.gateway_device

    #: Backlights created already, so learning a new button adds only that one.
    added: set[tuple[str, int]] = set()

    @callback
    def _async_add_missing() -> None:
        for managed in runtime.components:
            component = managed.component
            if component.ignored or component.kind is not ComponentKind.BUTTON:
                continue
            new = [
                button
                for button in component.button_range
                if (managed.subentry_id, button) not in added
            ]
            if not new:
                continue
            added.update((managed.subentry_id, button) for button in new)
            async_add_entities(
                [
                    BE3LedSwitch(
                        runtime.gateway,
                        runtime.provisioner,
                        mac,
                        component,
                        button,
                        via,
                    )
                    for button in new
                ],
                config_subentry_id=managed.subentry_id,
            )

    _async_add_missing()
    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_COMPONENTS_LEARNED.format(entry_id=entry.entry_id),
            _async_add_missing,
        )
    )


class BE3LedSwitch(BE3ComponentEntity, SwitchEntity):
    """Backlight for one button."""

    # Assumed, because the gateway accepts LED commands but never reports LED
    # state. Shown by default anyway: driving backlights is a large part of
    # what a keypad is for, and an entity nobody can find is not a feature.
    _attr_assumed_state = True

    def __init__(
        self,
        gateway: BE3Gateway,
        provisioner: Provisioner,
        mac: str,
        component: Component,
        button: int,
        via_device_id: str | None = None,
    ) -> None:
        super().__init__(gateway, mac, component, via_device_id)
        self._provisioner = provisioner
        self._button = button
        self._attr_is_on = False
        self._was_present = True
        self._attr_unique_id = led_unique_id(mac, component.address, component.slot, button)
        self._attr_translation_key = "led"
        self._attr_translation_placeholders = {"number": str(button)}

    def _handle_state(self, state) -> None:
        """Forget assumed LED state when the panel reboots.

        A panel restarts whenever its configuration changes, and comes back
        with its backlights off. Continuing to show them as on would be a
        confident lie — the one thing an assumed-state entity must avoid.
        """
        present = self._component.address in state.device_addresses
        if self._was_present and not present and self._attr_is_on:
            self._attr_is_on = False
        self._was_present = present
        super()._handle_state(state)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        try:
            await self._provisioner.set_led(self._component.address, self._button, on)
        except ProvisioningError as err:
            # Leave the previous assumed state alone: the panel did not change.
            raise HomeAssistantError(str(err)) from err
        self._attr_is_on = on
        self.async_write_ha_state()
