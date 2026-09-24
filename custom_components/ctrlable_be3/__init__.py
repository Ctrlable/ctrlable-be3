"""Ctrlable Pro integration for LifeSmart SUBLIME panels via the BE3 adaptor.

The protocol, connection, provisioning and device-model layers below this file
carry no Home Assistant imports and are unit tested on their own; this module
is the wiring.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from types import MappingProxyType

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .control import ControlDispatcher
from .const import (
    ATTR_ADDRESS,
    ATTR_COLOUR_TEMPERATURE,
    ATTR_DIRECTION,
    ATTR_BUTTONS,
    ATTR_ENTRY_ID,
    ATTR_KIND,
    ATTR_NAME,
    ATTR_NEW_ADDRESS,
    ATTR_PAGES,
    ATTR_SLOT,
    ATTR_SLOTS,
    CONF_HOST,
    CONF_MAC,
    CONF_PORT,
    CLEAR_INTERVAL,
    CONF_WRITTEN_PAGES,
    DOMAIN,
    EVENT_KEYPAD,
    EVENT_VALUE,
    GATEWAY_MODEL,
    MANUFACTURER,
    PLATFORMS,
    SIGNAL_COMPONENTS_LEARNED,
    SERVICE_BLANK_PAGE,
    SERVICE_CONFIGURE_PAGE,
    SERVICE_CLEAR_CONFIGURATION,
    SERVICE_CLEAR_PANEL,
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
    gateway_device_id,
    keypad_id,
)
from .gateway import BE3Gateway, ButtonEvent, GatewayState
from .keypads import KeypadRegistry
from .learning import BusObserver, apply_observation, group_from_restart
from .links import LinkRole, describe, link_for, role_of
from .protocol import (
    DEFAULT_TCP_PORT,
    SHADE_BOTTOM_TO_TOP,
    MOLD_DIMMER,
    MOLD_SHADE,
    MAX_ADDRESS,
    MAX_BUTTONS,
    MAX_DEVICE_INDEX,
    ButtonReport,
    Message,
    ValueRequest,
)
from .provisioning import Provisioner, ProvisioningError
from .records import WriteRecord

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
    observer: BusObserver = field(default_factory=BusObserver)
    control: ControlDispatcher | None = None
    #: What has already been written to each page, so an unchanged save does
    #: not restart a panel for nothing.
    writes: WriteRecord | None = None
    #: Set while persisting something the bus taught us, so the update does not
    #: reload the entry and drop the gateway's session.
    learning_update: bool = False
    #: Registry id of the gateway's own device, which components hang off.
    gateway_device: str | None = None


type BE3ConfigEntry = ConfigEntry[BE3Runtime]


async def async_setup_entry(hass: HomeAssistant, entry: BE3ConfigEntry) -> bool:
    """Start the gateway session for one BE3."""
    components = _managed_components(entry)
    _warn_duplicate_pages(components)

    gateway = BE3Gateway(
        port=entry.data.get(CONF_PORT, DEFAULT_TCP_PORT),
        expected_host=entry.data.get(CONF_HOST),
    )
    writes = WriteRecord(hass, entry.entry_id)
    await writes.async_load()
    entry.runtime_data = BE3Runtime(
        gateway=gateway,
        provisioner=Provisioner(gateway),
        components=components,
        writes=writes,
    )

    # Drive the targets that provisioned components point at, and answer the
    # polls their screens depend on.
    dispatcher = ControlDispatcher(hass, gateway)
    dispatcher.set_components([managed.component for managed in components])
    entry.runtime_data.control = dispatcher
    entry.async_on_unload(gateway.add_message_listener(dispatcher.handle_message))

    entry.runtime_data.gateway_device = _async_register_gateway_device(hass, entry)

    # Published for Buttons Machine, which reads this to offer the keypads on
    # this bus. Held per gateway: two BE3s can both have an address 16.
    registry = hass.data.setdefault(DOMAIN, {}).setdefault(
        "_keypad_registry", KeypadRegistry()
    )
    registry.refresh(
        entry.data[CONF_MAC],
        [managed.component for managed in components],
    )

    entry.async_on_unload(_async_publish_button_events(hass, entry, gateway))
    entry.async_on_unload(_async_publish_value_events(hass, entry, gateway))
    entry.async_on_unload(_async_record_discoveries(hass, entry, gateway))
    entry.async_create_background_task(
        hass, _async_reconcile_pages(hass, entry), "be3-reconcile-pages"
    )
    entry.async_on_unload(_async_learn_from_traffic(hass, entry, gateway))
    entry.async_on_unload(_async_adopt_pages(hass, entry, gateway))

    await gateway.start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _async_register_services(hass)
    return True


@callback
def _warn_duplicate_pages(components: tuple[ManagedComponent, ...]) -> None:
    """Report two components claiming the same page, and change nothing.

    This used to reassign one of them automatically. That meant rewriting
    settings nobody asked us to touch, and a keying mistake in it overwrote
    three pages with one page's configuration. Saying so and leaving the data
    alone is worth more than a repair that can silently destroy the thing it
    is repairing.
    """
    seen: dict[tuple[int, int], Component] = {}
    for managed in components:
        key = (managed.component.address, managed.component.slot)
        first = seen.get(key)
        if first is None:
            seen[key] = managed.component
            continue
        _LOGGER.warning(
            "Component %s page %s is configured twice — as %r and %r. Only one "
            "of them can be in use; delete whichever is wrong.",
            key[0],
            key[1],
            first.name or "unnamed",
            managed.component.name or "unnamed",
        )


@callback
def _async_register_gateway_device(hass: HomeAssistant, entry: BE3ConfigEntry) -> str:
    """Create the gateway's device up front and return its registry id.

    Components are children of the gateway, and the registry now wants the
    parent's id rather than its identifiers, so the parent has to exist first.
    """
    mac = entry.data[CONF_MAC]
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, gateway_device_id(mac))},
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        manufacturer=MANUFACTURER,
        model=GATEWAY_MODEL,
        name="BE3 gateway",
        configuration_url=f"http://{entry.data[CONF_HOST]}"
        if entry.data.get(CONF_HOST)
        else None,
    )
    return device.id


@callback
def _async_apply_learned(
    hass: HomeAssistant,
    entry: BE3ConfigEntry,
    subentry_id: str,
    updated: Component,
) -> None:
    """Rebuild in place what a reload would have rebuilt.

    The runtime's components, the controllers that answer the panel, the keypad
    registry other integrations read, and the platforms that own the entities —
    all refreshed without dropping the gateway's session.
    """
    runtime = entry.runtime_data
    runtime.components = tuple(
        ManagedComponent(managed.subentry_id, updated)
        if managed.subentry_id == subentry_id
        else managed
        for managed in runtime.components
    )
    if runtime.control is not None:
        runtime.control.set_components(
            [managed.component for managed in runtime.components]
        )
    registry = hass.data.get(DOMAIN, {}).get("_keypad_registry")
    if registry is not None:
        registry.refresh(
            entry.data[CONF_MAC],
            [managed.component for managed in runtime.components],
        )
    async_dispatcher_send(
        hass, SIGNAL_COMPONENTS_LEARNED.format(entry_id=entry.entry_id)
    )


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
            # The first page of a newly seen address; more are added by hand.
            slot = 1
            # Adding a subentry updates the entry, which reloads it, so the
            # new device and its entities appear without further prompting.
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(
                        Component(address=address, slot=slot).to_dict()
                    ),
                    subentry_type=SUBENTRY_COMPONENT,
                    title=f"Component {address}",
                    unique_id=f"{address}:{slot}",
                ),
            )

    return gateway.add_state_listener(_on_state)


def _async_learn_from_traffic(
    hass: HomeAssistant, entry: BE3ConfigEntry, gateway: BE3Gateway
) -> Callable[[], None]:
    """Fill in what a component is from what it sends.

    The gateway never says what lives at an address, but the components do:
    button events mean a keypad and name the button, value writes mean a dimmer
    or a shade. So walking the panels and pressing the buttons — which an
    installer does anyway — is enough to configure them.

    Only unknown components are filled in, and a keypad's button count only
    grows. An installer's explicit choice is never overwritten by traffic.
    """
    observer = entry.runtime_data.observer

    @callback
    def _on_message(message: Message) -> None:
        if isinstance(message, ButtonReport):
            learned = observer.observe_button(message)
            address = message.address
        elif isinstance(message, ValueRequest):
            learned = observer.observe_value(message)
            address = message.address
        else:
            return
        if learned:
            _apply(address)

    @callback
    def _apply(address: int) -> None:
        seen = observer.get(address)
        for managed in entry.runtime_data.components:
            if managed.component.address != address:
                continue
            updated = apply_observation(managed.component, seen)
            if updated is None:
                return

            _LOGGER.info(
                "Component %s identified itself as %s%s",
                address,
                updated.kind.value,
                f" with {updated.buttons} button(s)" if updated.buttons else "",
            )
            subentry = entry.subentries.get(managed.subentry_id)
            if subentry is not None:
                # Persisted so the count survives a restart, but *without* the
                # reload an entry update normally triggers. A reload closes the
                # listening socket, the gateway has to reconnect, and this
                # firmware stops relaying the bus when its session churns — so
                # discovering a sixth button used to take the whole bus down
                # mid-press. Everything a reload would rebuild is rebuilt here
                # instead.
                entry.runtime_data.learning_update = True
                hass.config_entries.async_update_subentry(
                    entry, subentry, data=MappingProxyType(updated.to_dict())
                )
                _async_apply_learned(hass, entry, managed.subentry_id, updated)
            return

    return gateway.add_message_listener(_on_message)


def _async_adopt_pages(
    hass: HomeAssistant, entry: BE3ConfigEntry, gateway: BE3Gateway
) -> Callable[[], None]:
    """Create a component for a page the panel has but we do not.

    Pages live on the panel until they are cleared, so one whose settings were
    deleted — or moved, which used to be possible — keeps asking and gets no
    answer. The panel names it every few seconds, so there is no reason to make
    an installer work out which page numbers are in use and recreate them by
    hand: adopt it, and let them configure or delete it like any other.
    """
    seen: set[tuple[int, int]] = set()

    @callback
    def _on_message(message: Message) -> None:
        if not isinstance(message, ValueRequest):
            return
        key = (message.address, message.index)
        if key in seen:
            return
        writes = getattr(entry.runtime_data, "writes", None)
        if writes is not None and writes.is_dismissed(message.address, message.index):
            # Deleted here on purpose. The panel keeps the page and goes on
            # polling it, so without this it would be adopted straight back and
            # deleting would look like it had failed.
            seen.add(key)
            return

        unique_id = f"{message.address}:{message.index}"
        for subentry in entry.subentries.values():
            if subentry.unique_id == unique_id:
                return
            data = subentry.data
            if (data.get("address"), data.get("slot")) == key:
                return
            if data.get("address") == message.address and data.get("ignored"):
                return
        seen.add(key)

        address, slot = key
        # The link it polls says what kind of page it is, which matters: a
        # page whose type is unknown cannot be cleared from the panel when
        # someone deletes it.
        role = role_of(message, slot)
        kind = {
            LinkRole.BRIGHTNESS: ComponentKind.DIMMER,
            LinkRole.COLOUR_TEMPERATURE: ComponentKind.DIMMER,
            LinkRole.LEVEL: ComponentKind.SHADE,
        }.get(role, ComponentKind.UNKNOWN)
        _LOGGER.info(
            "Adopting page %s on component %s, which the panel has but nothing "
            "here configured",
            slot,
            address,
        )
        # Defensive: two messages for one page can race, and an add that
        # loses is not worth an error in the log.
        with contextlib.suppress(AbortFlow):
            hass.config_entries.async_add_subentry(
                entry,
                ConfigSubentry(
                    data=MappingProxyType(
                        Component(address=address, slot=slot, kind=kind).to_dict()
                    ),
                    subentry_type=SUBENTRY_COMPONENT,
                    title=f"Component {address} page {slot}",
                    unique_id=unique_id,
                ),
            )

    return gateway.add_message_listener(_on_message)


@callback
def _slot_of(entry: BE3ConfigEntry, address: int) -> int:
    """The page a keypad's buttons belong to. Keypads have exactly one."""
    for managed in entry.runtime_data.components:
        if managed.component.address == address:
            return managed.component.slot
    return 1


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
                "keypad_id": keypad_id(mac, event.address, _slot_of(entry, event.address)),
                "gateway": mac,
                "address": event.address,
                "button": event.button,
                "action": event.action.value,
            },
        )

    return gateway.add_button_listener(_publish)


def _async_publish_value_events(
    hass: HomeAssistant, entry: BE3ConfigEntry, gateway: BE3Gateway
) -> Callable[[], None]:
    """Put dimmer and shade actions on the bus.

    A panel that is not a keypad never sends a button event, so without this
    its presses — including raise and lower — reach nothing.
    """
    mac = entry.data[CONF_MAC]

    slots = {
        managed.component.address: managed.component.slot
        for managed in entry.runtime_data.components
    }

    @callback
    def _publish(message: Message) -> None:
        if not isinstance(message, ValueRequest):
            return
        described = describe(message, slots.get(message.address, message.index))
        if described is None:
            return
        action, attributes = described
        hass.bus.async_fire(
            EVENT_VALUE,
            {
                "keypad_id": keypad_id(mac, message.address, message.index),
                "gateway": mac,
                "address": message.address,
                "action": action.value,
                **attributes,
            },
        )

    return gateway.add_message_listener(_publish)


async def _async_reconcile_pages(hass: HomeAssistant, entry: BE3ConfigEntry) -> None:
    """Clear pages from panels when their component has been deleted.

    A page lives on the panel until something clears it, and Home Assistant
    offers no callback when a subentry is removed — so deleting a component
    would otherwise leave the panel showing a page nothing answers. We keep a
    record of what we have written and compare it on every load.
    """
    runtime = entry.runtime_data
    configured = {
        (managed.component.address, managed.component.slot, managed.component.kind.mold)
        for managed in runtime.components
        if managed.component.kind.configured and not managed.component.ignored
    }
    recorded = {
        (int(item[0]), int(item[1]), str(item[2]))
        for item in entry.options.get(CONF_WRITTEN_PAGES, [])
        if len(item) == 3
    }

    for address, slot, mold in sorted(recorded - configured):
        # Reported, not cleared. A clear does nothing on this firmware, and
        # sending one anyway put two useless messages on the bus for every
        # deleted page — on a gateway that stops its bus loop if provoked.
        _LOGGER.info(
            "Page %s on component %s is no longer configured here, and the "
            "panel keeps it: it still shows on the screen as a %s page. Add a "
            "component for that page number to take it over.",
            slot,
            address,
            mold,
        )

    if configured != recorded:
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_WRITTEN_PAGES: sorted(
                    [address, slot, mold] for address, slot, mold in configured
                ),
            },
        )



async def async_watch_restart_grouping(
    hass: HomeAssistant, entry: BE3ConfigEntry, address: int
) -> None:
    """Group the components that restart alongside one we just wrote to.

    Nothing in the protocol says which addresses share a panel. This does,
    because writing a personality reboots the whole panel and its components
    leave and return together — a signal we get for free, since we caused it.

    Best effort by design: a panel that reboots between two heartbeats never
    appears to have left, and learning nothing is the right outcome there.
    """
    runtime = entry.runtime_data
    gateway = runtime.gateway
    before = set(gateway.state.device_addresses)
    missing: set[int] = set()

    @callback
    def _watch(state: GatewayState) -> None:
        missing.update(before - set(state.device_addresses))

    unsubscribe = gateway.add_state_listener(_watch)
    try:
        if not await runtime.provisioner.wait_for_restart(address):
            return
    except ProvisioningError:
        return
    finally:
        unsubscribe()

    during = before - missing
    grouped = group_from_restart(
        before, during, gateway.state.device_addresses, address
    )
    if len(grouped) < 2:
        return

    label = _panel_label(entry, grouped)
    _LOGGER.info(
        "Components %s restarted together, so they share a panel: labelling them %r",
        ", ".join(str(item) for item in grouped),
        label,
    )
    for managed in entry.runtime_data.components:
        component = managed.component
        if component.address not in grouped or component.panel:
            continue
        subentry = entry.subentries.get(managed.subentry_id)
        if subentry is None:
            continue
        hass.config_entries.async_update_subentry(
            entry,
            subentry,
            data=MappingProxyType(replace(component, panel=label).to_dict()),
        )


def _panel_label(entry: BE3ConfigEntry, grouped: tuple[int, ...]) -> str:
    """Name the panel after whatever the installer already called part of it."""
    for managed in entry.runtime_data.components:
        component = managed.component
        if component.address in grouped and component.panel:
            return component.panel
    return f"Panel {grouped[0]}"


async def async_unload_entry(hass: HomeAssistant, entry: BE3ConfigEntry) -> bool:
    """Release the port and stop background work."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.gateway.stop()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: BE3ConfigEntry) -> None:
    """Reload when the component list changes — but not for what we learned.

    A reload is the right answer to an installer changing a component, and the
    wrong answer to a panel telling us it has a sixth button: it closes the
    listening socket, and a gateway that has to re-establish its session stops
    relaying the bus often enough that pressing the buttons of a new keypad
    could take the whole installation down.
    """
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None and runtime.learning_update:
        runtime.learning_update = False
        _LOGGER.debug(
            "Applied what the bus told us without reloading, to keep the "
            "gateway's session"
        )
        return
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the provisioning services once per Home Assistant run."""
    if hass.services.has_service(DOMAIN, SERVICE_IDENTIFY):
        return

    def _runtime(call: ServiceCall) -> BE3Runtime:
        entry_id = call.data.get(ATTR_ENTRY_ID) or _only_gateway()
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise HomeAssistantError(f"No BE3 gateway with entry id {entry_id}")
        if not hasattr(entry, "runtime_data") or entry.runtime_data is None:
            raise HomeAssistantError("That BE3 gateway is not loaded")
        return entry.runtime_data

    def _only_gateway() -> str:
        """The gateway to act on when a call does not name one.

        Most installations have exactly one, and making someone paste an
        internal id to identify it is how a working call comes back as
        "required key not provided".
        """
        loaded = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if getattr(entry, "runtime_data", None) is not None
        ]
        if len(loaded) == 1:
            return loaded[0].entry_id
        if not loaded:
            raise ServiceValidationError("No BE3 gateway is loaded")
        raise ServiceValidationError(
            "There is more than one BE3 gateway, so this action needs to be "
            "told which: " + ", ".join(entry.title for entry in loaded)
        )

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

    async def _blank_page(call: ServiceCall) -> None:
        """Point a leftover page at nothing.

        The honest version of deleting a page: the panel keeps it, but it stops
        asking about a link nobody owns and stops driving anything when it is
        touched. Clearing a page does nothing on this firmware; writing one
        works, so this writes.
        """
        runtime = _runtime(call)
        kind = ComponentKind(call.data[ATTR_KIND])
        if kind.mold is None:
            raise ServiceValidationError(
                f"A {kind.value} page has no configuration to blank"
            )
        await _guard(
            runtime.provisioner.blank_configuration(
                call.data[ATTR_ADDRESS],
                call.data[ATTR_SLOT],
                kind.mold,
                call.data.get(ATTR_NAME),
            )
        )

    async def _configure_page(call: ServiceCall) -> None:
        """Write a real configuration to one or more pages of a panel.

        The only operation a panel honours. It is how a page left behind by
        another controller is taken over: the page cannot be removed, but it
        can be made ours, named, and pointed at a link we answer.

        Each write restarts the panel, and anything sent while it is away is
        lost, so the pages go one at a time and each one waits for the panel to
        come back. A restart that is not observed is not an error — heartbeats
        are seconds apart and a quick panel can be back before the next one.
        """
        runtime = _runtime(call)
        address = call.data[ATTR_ADDRESS]
        kind = ComponentKind(call.data[ATTR_KIND])
        slots: list[int] = list(call.data[ATTR_SLOTS])
        names: list[str] = list(call.data.get(ATTR_NAME) or [])
        if names and len(names) != len(slots):
            raise ServiceValidationError(
                f"Got {len(names)} names for {len(slots)} pages: give one name "
                "per page, or none at all"
            )

        for position, slot in enumerate(slots):
            name = names[position] if names else f"Page {slot}"
            if kind is ComponentKind.DIMMER:
                await _guard(
                    runtime.provisioner.configure_dimmer(
                        address,
                        slot,
                        link_for(slot, LinkRole.BRIGHTNESS),
                        name=name,
                        colour_link=(
                            link_for(slot, LinkRole.COLOUR_TEMPERATURE)
                            if call.data.get(ATTR_COLOUR_TEMPERATURE)
                            else None
                        ),
                    )
                )
            else:
                await _guard(
                    runtime.provisioner.configure_shade(
                        address,
                        slot,
                        link_for(slot, LinkRole.LEVEL),
                        name=name,
                        direction=int(call.data.get(ATTR_DIRECTION, SHADE_BOTTOM_TO_TOP)),
                    )
                )

            if slot != slots[-1]:
                restarted = await runtime.provisioner.wait_for_restart(address)
                _LOGGER.info(
                    "Component %s: page %s written%s",
                    address,
                    slot,
                    "" if restarted else " (no restart seen, which is normal)",
                )

    async def _clear_panel(call: ServiceCall) -> None:
        """Ask a panel to drop its pages, for what little that is worth.

        On firmware 0.10 this does nothing observable: the pages stay on the
        screen with their names, and a cleared page goes on polling, before
        and after a power cycle. Kept because it costs nothing and may mean
        something on another firmware — but a page is only ever really
        changed by writing a new configuration to its number.

        Normally clears the pages we know about. A panel can also hold pages
        we cannot see — a page with no link never asks us for anything, so it
        is invisible on the wire while still occupying the screen — so an
        address can be swept instead, clearing every page number whether or
        not anything is configured there.
        """
        runtime = _runtime(call)
        address = call.data.get(ATTR_ADDRESS)
        sweep = int(call.data.get(ATTR_PAGES, 0))

        work: list[tuple[int, int, str]] = []
        if sweep and address is not None:
            # Both screen types per page: we cannot know which a leftover is.
            work = [
                (address, page, mold)
                for page in range(1, sweep + 1)
                for mold in (MOLD_DIMMER, MOLD_SHADE)
            ]
        else:
            work = [
                (
                    managed.component.address,
                    managed.component.slot,
                    managed.component.kind.mold or "",
                )
                for managed in runtime.components
                if managed.component.kind.mold is not None
                and not managed.component.ignored
                and (address is None or managed.component.address == address)
            ]

        if not work:
            _LOGGER.info("Nothing to clear")
            return

        _LOGGER.info("Clearing %d configuration(s), one panel restart each", len(work))
        for target_address, page, mold in work:
            await _guard(
                runtime.provisioner.clear_configuration(target_address, page, mold)
            )
            # Clearing does not restart the panel, so this is a breath rather
            # than a reboot: a sweep of eight took six minutes waiting for
            # restarts that never came.
            await asyncio.sleep(CLEAR_INTERVAL)
        _LOGGER.info("Cleared %d configuration(s)", len(work))

    async def _start_update(call: ServiceCall) -> None:
        runtime = _runtime(call)
        await _guard(runtime.provisioner.start_update())

    address = vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_ADDRESS))
    slot = vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_DEVICE_INDEX))
    # Optional: a call that names no gateway means the only one there is.
    entry_id = vol.Schema({vol.Optional(ATTR_ENTRY_ID): cv.string})

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
        DOMAIN,
        SERVICE_BLANK_PAGE,
        _blank_page,
        schema=entry_id.extend(
            {
                vol.Required(ATTR_ADDRESS): address,
                vol.Required(ATTR_SLOT): slot,
                vol.Required(ATTR_KIND): vol.In(
                    [kind.value for kind in ComponentKind if kind.mold is not None]
                ),
                vol.Optional(ATTR_NAME): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CONFIGURE_PAGE,
        _configure_page,
        schema=entry_id.extend(
            {
                vol.Required(ATTR_ADDRESS): address,
                vol.Required(ATTR_SLOTS): vol.All(cv.ensure_list, [slot], vol.Length(min=1)),
                vol.Required(ATTR_KIND): vol.In(
                    [ComponentKind.DIMMER.value, ComponentKind.SHADE.value]
                ),
                vol.Optional(ATTR_NAME): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional(ATTR_COLOUR_TEMPERATURE): cv.boolean,
                vol.Optional(ATTR_DIRECTION): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=4)
                ),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_START_UPDATE, _start_update, schema=entry_id
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CLEAR_PANEL,
        _clear_panel,
        schema=entry_id.extend(
            {
                vol.Optional(ATTR_ADDRESS): address,
                vol.Optional(ATTR_PAGES): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=MAX_DEVICE_INDEX)
                ),
            }
        ),
    )


async def _guard(awaitable) -> None:
    """Surface provisioning refusals as Home Assistant errors."""
    try:
        await awaitable
    except ProvisioningError as err:
        raise HomeAssistantError(str(err)) from err
