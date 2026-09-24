"""Identify buttons: make a panel flash so it can be found.

The protocol reports an address and nothing else — no name, model or serial —
so flashing a component is the only way to tell which physical panel an address
belongs to. It is also how an address conflict is confirmed: if two panels
flash at once, they share the address (see docs/address-conflicts.md).

One button per component rather than per panel: on a SUBLIME Pro the buttons
and the small screen answer to different addresses, and during commissioning
you want to know which is which.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BE3ConfigEntry, ManagedComponent
from .const import CONF_MAC
from .devices import Component, identify_unique_id
from .entity import BE3ComponentEntity
from .gateway import BE3Gateway
from .provisioning import Provisioner, ProvisioningError


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BE3ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create an identify button for every configured component."""
    runtime = entry.runtime_data
    mac = entry.data[CONF_MAC]
    via = runtime.gateway_device

    for managed in runtime.components:
        if managed.component.ignored:
            continue
        async_add_entities(
            [
                BE3IdentifyButton(
                    runtime.gateway,
                    runtime.provisioner,
                    mac,
                    managed.component,
                    via,
                )
            ],
            config_subentry_id=managed.subentry_id,
        )


class BE3IdentifyButton(BE3ComponentEntity, ButtonEntity):
    """Flashes one component."""

    _attr_device_class = ButtonDeviceClass.IDENTIFY

    def __init__(
        self,
        gateway: BE3Gateway,
        provisioner: Provisioner,
        mac: str,
        component: Component,
        via_device_id: str | None = None,
    ) -> None:
        super().__init__(gateway, mac, component, via_device_id)
        self._provisioner = provisioner
        self._attr_unique_id = identify_unique_id(mac, component.address, component.slot)
        self._attr_translation_key = "identify"
        self._attr_translation_placeholders = {"address": str(component.address)}

    async def async_press(self) -> None:
        try:
            await self._provisioner.identify(self._component.address)
        except ProvisioningError as err:
            raise HomeAssistantError(str(err)) from err
