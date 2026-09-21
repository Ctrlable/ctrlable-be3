"""Ctrlable Pro integration for LifeSmart SUBLIME panels via the BE3 adaptor.

The protocol, connection, provisioning and device-model layers below this file
carry no Home Assistant imports and are unit tested on their own; this module
is the wiring.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_ADDRESS,
    ATTR_BUTTONS,
    ATTR_ENTRY_ID,
    ATTR_KIND,
    ATTR_NAME,
    ATTR_NEW_ADDRESS,
    ATTR_SLOT,
    CONF_HOST,
    CONF_MAC,
    CONF_PORT,
    DOMAIN,
    EVENT_KEYPAD,
    PLATFORMS,
    SERVICE_CLEAR_CONFIGURATION,
    SERVICE_CONFIGURE_KEYPAD,
    SERVICE_IDENTIFY,
    SERVICE_SET_ADDRESS,
    SERVICE_START_UPDATE,
    SUBENTRY_COMPONENT,
)
from .devices import (
    Component,
    ComponentKind,
    ConfigurationError,
    keypad_id,
)
from .gateway import BE3Gateway, ButtonEvent, GatewayState
from .protocol import DEFAULT_TCP_PORT, MAX_ADDRESS, MAX_BUTTONS, MAX_DEVICE_INDEX
from .provisioning import Provisioner, ProvisioningError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ManagedComponent:
    """A component together with the subentry that configures it.

    Each component is a subentry, which is what gives it its own device and a
    Configure button on that device's page.
    """

    subentry_id: str
    component: Component


@dataclass
class BE3Runtime:
    """Everything a config entry owns while it is loaded."""

    gateway: BE3Gateway
    provisioner: Provisioner
    components: tuple[ManagedComponent, ...] = ()


type BE3ConfigEntry = ConfigEntry[BE3Runtime]


async def async_setup_entry(hass: HomeAssistant, entry: BE3ConfigEntry) -> bool:
    """Start the gateway session for one BE3."""
    components = _managed_components(entry)

    gateway = BE3Gateway(
        port=entry.data.get(CONF_PORT, DEFAULT_TCP_PORT),
        expected_host=entry.data.get(CONF_HOST),
    )
    entry.runtime_data = BE3Runtime(
        gateway=gateway,
        provisioner=Provisioner(gateway),
        components=components,
    )

    entry.async_on_unload(_async_publish_button_events(hass, entry, gateway))
    entry.async_on_unload(_async_record_discoveries(hass, entry, gateway))

    await gateway.start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _async_register_services(hass)
    return True


def _managed_components(entry: BE3ConfigEntry) -> tuple[ManagedComponent, ...]:
    """Read the configured components out of the entry's subentries."""
    managed: list[ManagedComponent] = []
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_COMPONENT:
            continue
        try:
            component = Component.from_dict(dict(subentry.data))
        except ConfigurationError as err:
            # One bad subentry must not take the whole gateway down with it.
            _LOGGER.warning(
                "Ignoring invalid BE3 component %s: %s", subentry.title, err
            )
            continue
        managed.append(ManagedComponent(subentry.subentry_id, component))
    return tuple(sorted(managed, key=lambda item: item.component.address))


def _async_record_discoveries(
    hass: HomeAssistant, entry: BE3ConfigEntry, gateway: BE3Gateway
) -> Callable[[], None]:
    """Persist components the gateway reports but we have never seen.

    The bus is authoritative about what exists, so a component appears as a
    device without anyone declaring it. Writing the discovery into the entry's
    options rather than holding it in memory means the device survives a
    restart, and the resulting update reloads the entry so its entities appear.
    """

    @callback
    def _on_state(state: GatewayState) -> None:
        if not state.device_addresses:
            return
        known = {
            managed.component.address for managed in entry.runtime_data.components
        }
        new = [
            address for address in state.device_addresses if address not in known
        ]
        if not new:
            return

        _LOGGER.info(
            "BE3 reported %d new component(s): %s",
            len(new),
            ", ".join(str(address) for address in new),
        )
        for address in new:
            # Adding a subentry updates the entry, which reloads it, so the
            # new device and its entities appear without further prompting.
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(Component(address=address).to_dict()),
                    subentry_type=SUBENTRY_COMPONENT,
                    title=f"Component {address}",
                    unique_id=str(address),
                ),
            )

    return gateway.add_state_listener(_on_state)


def _async_publish_button_events(
    hass: HomeAssistant, entry: BE3ConfigEntry, gateway: BE3Gateway
) -> Callable[[], None]:
    """Put every button action on the event bus.

    Entities cover what a person sees in the UI; the bus is what other code
    consumes. The Buttons Machine backend listens here, and so can any
    automation that wants a button before it has been configured as anything.
    """
    mac = entry.data[CONF_MAC]

    @callback
    def _publish(event: ButtonEvent) -> None:
        hass.bus.async_fire(
            EVENT_KEYPAD,
            {
                "keypad_id": keypad_id(mac, event.address),
                "gateway": mac,
                "address": event.address,
                "button": event.button,
                "action": event.action.value,
            },
        )

    return gateway.add_button_listener(_publish)


async def async_unload_entry(hass: HomeAssistant, entry: BE3ConfigEntry) -> bool:
    """Release the port and stop background work."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.gateway.stop()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: BE3ConfigEntry) -> None:
    """Reload when the component list changes."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the provisioning services once per Home Assistant run."""
    if hass.services.has_service(DOMAIN, SERVICE_IDENTIFY):
        return

    def _runtime(call: ServiceCall) -> BE3Runtime:
        entry_id = call.data[ATTR_ENTRY_ID]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise HomeAssistantError(f"No BE3 gateway with entry id {entry_id}")
        if not hasattr(entry, "runtime_data") or entry.runtime_data is None:
            raise HomeAssistantError("That BE3 gateway is not loaded")
        return entry.runtime_data

    async def _identify(call: ServiceCall) -> None:
        runtime = _runtime(call)
        await _guard(runtime.provisioner.identify(call.data[ATTR_ADDRESS]))

    async def _set_address(call: ServiceCall) -> None:
        runtime = _runtime(call)
        await _guard(
            runtime.provisioner.set_address(
                call.data[ATTR_ADDRESS], call.data[ATTR_NEW_ADDRESS]
            )
        )

    async def _configure_keypad(call: ServiceCall) -> None:
        runtime = _runtime(call)
        await _guard(
            runtime.provisioner.configure_buttons(
                call.data[ATTR_ADDRESS],
                call.data[ATTR_SLOT],
                call.data[ATTR_BUTTONS],
                call.data.get(ATTR_NAME),
            )
        )

    async def _clear_configuration(call: ServiceCall) -> None:
        runtime = _runtime(call)
        kind = ComponentKind(call.data[ATTR_KIND])
        await _guard(
            runtime.provisioner.clear_configuration(
                call.data[ATTR_ADDRESS], call.data[ATTR_SLOT], kind.mold
            )
        )

    async def _start_update(call: ServiceCall) -> None:
        runtime = _runtime(call)
        await _guard(runtime.provisioner.start_update())

    address = vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_ADDRESS))
    slot = vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_DEVICE_INDEX))
    entry_id = vol.Schema({vol.Required(ATTR_ENTRY_ID): cv.string})

    hass.services.async_register(
        DOMAIN,
        SERVICE_IDENTIFY,
        _identify,
        schema=entry_id.extend({vol.Required(ATTR_ADDRESS): address}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_ADDRESS,
        _set_address,
        schema=entry_id.extend(
            {
                vol.Required(ATTR_ADDRESS): address,
                vol.Required(ATTR_NEW_ADDRESS): address,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIGURE_KEYPAD,
        _configure_keypad,
        schema=entry_id.extend(
            {
                vol.Required(ATTR_ADDRESS): address,
                vol.Required(ATTR_SLOT): slot,
                vol.Required(ATTR_BUTTONS): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=MAX_BUTTONS)
                ),
                vol.Optional(ATTR_NAME): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_CONFIGURATION,
        _clear_configuration,
        schema=entry_id.extend(
            {
                vol.Required(ATTR_ADDRESS): address,
                vol.Required(ATTR_SLOT): slot,
                vol.Required(ATTR_KIND): vol.In([kind.value for kind in ComponentKind]),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START_UPDATE, _start_update, schema=entry_id
    )


async def _guard(awaitable) -> None:
    """Surface provisioning refusals as Home Assistant errors."""
    try:
        await awaitable
    except ProvisioningError as err:
        raise HomeAssistantError(str(err)) from err
