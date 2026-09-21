"""Keypad backlight switches.

The gateway accepts LED commands but never reports LED state, so these are
assumed-state switches: they show what was last commanded, which may drift if
something else drives the same panel.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BE3ConfigEntry, ManagedComponent
from .const import CONF_MAC
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

    for managed in runtime.components:
        component = managed.component
        if component.kind is not ComponentKind.BUTTON:
            continue
        async_add_entities(
            [
                BE3LedSwitch(
                    runtime.gateway, runtime.provisioner, mac, component, button
                )
                for button in component.button_range
            ],
            config_subentry_id=managed.subentry_id,
        )


class BE3LedSwitch(BE3ComponentEntity, SwitchEntity):
    """Backlight for one button."""

    _attr_assumed_state = True
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        gateway: BE3Gateway,
        provisioner: Provisioner,
        mac: str,
        component: Component,
        button: int,
    ) -> None:
        super().__init__(gateway, mac, component)
        self._provisioner = provisioner
        self._button = button
        self._attr_is_on = False
        self._attr_unique_id = led_unique_id(mac, component.address, button)
        self._attr_translation_key = "led"
        self._attr_translation_placeholders = {"number": str(button)}

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
