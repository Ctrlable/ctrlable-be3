"""Config and subentry flows.

Setup discovers gateways with the legacy probe, which is safe to run against a
site still owned by another controller: it asks a gateway to describe itself
rather than to connect.

Each bus component is a **subentry**, so it gets its own device and its own
Configure button on that device's page. Components are created automatically
from what the gateway reports, so these flows are for saying what a component
*is* — the gateway can only say that it exists.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

import asyncio

from .const import (
    CLEAR_INTERVAL,
    CONF_HOST,
    CONF_MAC,
    CONF_MODEL,
    CONF_PORT,
    DOMAIN,
    SUBENTRY_COMPONENT,
)
from . import async_watch_restart_grouping
from .devices import Component, ComponentKind, ConfigurationError, next_free_slot
from .discovery import async_discover, async_probe
from .learning import Observation, contradicts, possible_kinds
from .links import LinkRole, link_for
from .payloads import payload_for, signature
from .provisioning import NotConnected, ProvisioningError

#: How long to wait for a panel to come back after a clear before writing
#: its new configuration. Long enough for a reboot, short enough that an
#: installer is not left staring at a spinner.
RESTART_SETTLE = 20.0
from .protocol import (
    DEFAULT_BUTTONS,
    DEFAULT_TCP_PORT,
    MAX_ADDRESS,
    MAX_BUTTONS,
    MAX_DEVICE_INDEX,
)

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"

CONF_ADDRESS = "address"
CONF_KIND = "kind"
CONF_BUTTONS = "buttons"
CONF_SLOT = "slot"
CONF_NAME = "name"
CONF_PANEL = "panel"
CONF_TARGET = "target"
CONF_LEFTOVER = "leftover_target"
CONF_COLOUR = "colour_temperature"
CONF_DIRECTION = "direction"
CONF_APPLY = "apply"
CONF_IGNORED = "ignored"


class BE3ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a gateway."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered: dict[str, Any] = {}

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Each bus component is configured as a subentry."""
        return {SUBENTRY_COMPONENT: ComponentSubentryFlow}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Offer the gateways found on the network."""
        if user_input is not None:
            if user_input[CONF_HOST] == MANUAL:
                return await self.async_step_manual()
            gateway = self._discovered[user_input[CONF_HOST]]
            return await self._async_create(
                host=gateway.host,
                mac=gateway.mac,
                model=gateway.model,
                port=user_input[CONF_PORT],
            )

        found = await async_discover()
        self._discovered = {gateway.host: gateway for gateway in found}
        if not found:
            return await self.async_step_manual()

        choices = {gateway.host: gateway.title for gateway in found}
        choices[MANUAL] = "Enter an address manually"
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): vol.In(choices),
                    vol.Required(CONF_PORT, default=DEFAULT_TCP_PORT): cv.port,
                }
            ),
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept an address typed by hand."""
        errors: dict[str, str] = {}
        if user_input is not None:
            gateway = await async_probe(user_input[CONF_HOST])
            if gateway is None:
                errors["base"] = "cannot_connect"
            else:
                return await self._async_create(
                    host=gateway.host,
                    mac=gateway.mac,
                    model=gateway.model,
                    port=user_input[CONF_PORT],
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): cv.string,
                    vol.Required(CONF_PORT, default=DEFAULT_TCP_PORT): cv.port,
                }
            ),
            errors=errors,
        )

    async def _async_create(
        self, *, host: str, mac: str, model: str | None, port: int
    ) -> ConfigFlowResult:
        # Identity is the MAC, so a gateway that changes address is recognised
        # and updated rather than added twice.
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})
        return self.async_create_entry(
            title=f"BE3 gateway ({host})",
            data={
                CONF_HOST: host,
                CONF_MAC: mac,
                CONF_MODEL: model,
                CONF_PORT: port,
            },
        )


class ComponentSubentryFlow(ConfigSubentryFlow):
    """Say what a component is, and how it should behave.

    Two steps rather than one form: a keypad has nothing to say about shade
    orientation, and showing every field for every type is how an installer
    ends up rejected by a setting they never touched.
    """

    def __init__(self) -> None:
        self._existing: dict[str, Any] | None = None
        self._kind: ComponentKind = ComponentKind.UNKNOWN
        self._address: int | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a component by hand.

        Rarely needed: components appear on their own from the gateway's
        heartbeat. This covers one that is not reporting yet — a panel waiting
        to be wired, or one being configured before it is installed.
        """
        return await self._async_choose_kind(user_input, existing=None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Ask what to do with this page before asking what it should be.

        Editing and deleting are different intentions, and a form that opens
        straight into "what type is this?" reads like an add — which is how a
        page gets overwritten by someone who meant to look at it.
        """
        self._existing = dict(self._get_reconfigure_subentry().data)
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["edit", "delete"],
            description_placeholders=self._page_labels(),
        )

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change this page's settings."""
        return await self._async_choose_kind(user_input, existing=self._existing)

    async def async_step_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Stop managing this page. The panel keeps it either way."""
        if user_input is None:
            return self.async_show_form(
                step_id="delete",
                data_schema=vol.Schema({}),
                description_placeholders=self._page_labels(),
            )

        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        try:
            component = Component.from_dict(dict(subentry.data))
        except ConfigurationError:
            component = None

        runtime = getattr(entry, "runtime_data", None)
        if component is not None:
            # No clear is sent. It does nothing on this firmware — the page
            # stays, keeps its name and goes on polling, before and after a
            # power cycle — and every message is a chance to upset a gateway
            # that has stopped its bus loop five times in a day. Saying so is
            # more use than sending it.
            _LOGGER.info(
                "No longer managing page %s on component %s. The panel keeps "
                "the page: nothing in this protocol removes one. It will not "
                "be listed again until it is added back.",
                component.slot,
                component.address,
            )

        if runtime is not None and runtime.writes is not None and component is not None:
            # Forget what was written, so whoever takes the page over next has
            # their configuration actually sent; and stay out of its way until
            # then, or it would be adopted straight back from its own polling.
            runtime.writes.forget(component.address, component.slot)
            runtime.writes.dismiss(component.address, component.slot)

        self.hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        return self.async_abort(reason="deleted")

    async def _async_choose_kind(
        self, user_input: dict[str, Any] | None, existing: dict[str, Any] | None
    ) -> SubentryFlowResult:
        self._existing = existing
        step = "edit" if existing is not None else "user"

        if user_input is not None:
            self._kind = ComponentKind(user_input[CONF_KIND])
            if existing is None:
                self._address = int(user_input[CONF_ADDRESS])
            if self._contradicts_evidence():
                return await self.async_step_confirm_type()
            return await self._async_continue_to_settings()

        current = existing or {}
        fields: dict[Any, Any] = {}
        if existing is None:
            fields[vol.Required(CONF_ADDRESS)] = vol.All(
                vol.Coerce(int), vol.Range(min=0, max=MAX_ADDRESS)
            )
        fields[vol.Required(CONF_KIND, default=current.get(CONF_KIND, "unknown"))] = (
            SelectSelector(
                SelectSelectorConfig(
                    options=[
                        SelectOptionDict(value=kind.value, label=kind.value)
                        for kind in self._offered_kinds(current)
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="component_kind",
                )
            )
        )
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema(fields),
            description_placeholders={
                **self._page_labels(),
                "hardware": self._hardware_note(),
            },
        )

    def _offered_kinds(self, current: dict[str, Any]) -> tuple[ComponentKind, ...]:
        """The types this component could actually be.

        A page cannot be removed once the panel has it, so a type the hardware
        cannot use is not a mistake an installer gets to undo — it is better
        not offered. Only the address being edited can be filtered: when one is
        being added the address has not been entered yet, and the contradiction
        is caught on the way to the settings instead.
        """
        if self._existing is None and self._address is None:
            return tuple(ComponentKind)

        offered = possible_kinds(self._observed())
        stored = current.get(CONF_KIND)
        if stored and stored not in {kind.value for kind in offered}:
            # Whatever is already saved has to stay selectable, or the form
            # opens with a default it refuses to accept.
            offered = (*offered, ComponentKind(stored))
        return offered

    def _hardware_note(self) -> str:
        """Say what the component's own traffic proves it is."""
        observed = self._observed().kind
        if observed is ComponentKind.BUTTON:
            return (
                "This address reports button presses, so it is a button block: "
                "only a keypad can be written to it."
            )
        if observed in (ComponentKind.DIMMER, ComponentKind.SHADE):
            return (
                "This address asks for values to show, so it is a screen: it "
                "takes dimmer and shade pages, not keypads."
            )
        return (
            "This address has not said what it is yet. Press its buttons or "
            "give it a page to watch, and the choice narrows on its own."
        )

    async def async_step_confirm_type(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Confirm a type the component's own traffic contradicts.

        Writing the wrong personality is not a setting to undo later: the
        component is left with no working configuration, which is what happens
        when a panel's screen slot is told it is a keypad.
        """
        if user_input is not None:
            return await self._async_continue_to_settings()

        return self.async_show_form(
            step_id="confirm_type",
            data_schema=vol.Schema({}),
            description_placeholders={
                "observed": self._observed().kind.value,
                "chosen": self._kind.value,
            },
        )

    async def _async_continue_to_settings(self) -> SubentryFlowResult:
        """Show the settings that belong to the chosen type."""
        if self._kind is ComponentKind.BUTTON:
            return await self.async_step_keypad()
        if self._kind is ComponentKind.DIMMER:
            return await self.async_step_dimmer()
        if self._kind is ComponentKind.SHADE:
            return await self.async_step_shade()
        # Unknown: nothing more to ask, and saving it hands the component back
        # to identifying itself.
        return await self._async_save({})

    def _observed(self) -> Observation:
        """What this component's own traffic says it is."""
        runtime = getattr(self._get_entry(), "runtime_data", None)
        address = self._address
        if address is None and self._existing:
            address = int(self._existing.get(CONF_ADDRESS, -1))
        if runtime is None or address is None:
            return Observation()
        return runtime.observer.get(address)

    def _contradicts_evidence(self) -> bool:
        return contradicts(self._observed(), self._kind)

    async def async_step_keypad(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Settings that only a keypad has."""
        if user_input is not None:
            return await self._async_save(user_input)

        current = self._existing or {}
        return self.async_show_form(
            step_id="keypad",
            description_placeholders=self._page_labels(),
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default=current.get(CONF_NAME, "")): cv.string,
                    vol.Optional(
                        CONF_BUTTONS,
                        default=int(current.get(CONF_BUTTONS, DEFAULT_BUTTONS)),
                    ): _number(1, MAX_BUTTONS),
                    **_shared_fields(
                        current,
                        self._default_slot(self._existing),
                        editable_slot=self._existing is None,
                    ),
                }
            ),
        )

    async def async_step_dimmer(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Settings that only a dimmer has."""
        if user_input is not None:
            return await self._async_save(user_input)

        current = self._existing or {}
        return self.async_show_form(
            step_id="dimmer",
            description_placeholders=self._page_labels(),
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default=current.get(CONF_NAME, "")): cv.string,
                    vol.Optional(
                        CONF_TARGET,
                        description={
                            "suggested_value": current.get(CONF_TARGET) or None
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(domain=["light", "switch"])
                    ),
                    vol.Optional(
                        CONF_LEFTOVER,
                        description={
                            "suggested_value": current.get(CONF_LEFTOVER) or None
                        },
                    ): EntitySelector(EntitySelectorConfig(domain=["cover"])),
                    vol.Optional(
                        CONF_COLOUR, default=bool(current.get(CONF_COLOUR, False))
                    ): BooleanSelector(),
                    **_shared_fields(
                        current,
                        self._default_slot(self._existing),
                        editable_slot=self._existing is None,
                    ),
                }
            ),
        )

    async def async_step_shade(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Settings that only a shade controller has."""
        if user_input is not None:
            return await self._async_save(user_input)

        current = self._existing or {}
        return self.async_show_form(
            step_id="shade",
            description_placeholders=self._page_labels(),
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_NAME, default=current.get(CONF_NAME, "")): cv.string,
                    vol.Optional(
                        CONF_TARGET,
                        description={
                            "suggested_value": current.get(CONF_TARGET) or None
                        },
                    ): EntitySelector(EntitySelectorConfig(domain=["cover"])),
                    vol.Optional(
                        CONF_LEFTOVER,
                        description={
                            "suggested_value": current.get(CONF_LEFTOVER) or None
                        },
                    ): EntitySelector(
                        EntitySelectorConfig(domain=["light", "switch"])
                    ),
                    vol.Optional(
                        CONF_DIRECTION, default=str(current.get(CONF_DIRECTION, 4))
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value="1", label="separate"),
                                SelectOptionDict(value="2", label="left_to_right"),
                                SelectOptionDict(value="3", label="right_to_left"),
                                SelectOptionDict(value="4", label="bottom_to_top"),
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="shade_direction",
                        )
                    ),
                    **_shared_fields(
                        current,
                        self._default_slot(self._existing),
                        editable_slot=self._existing is None,
                    ),
                }
            ),
        )

    async def _async_write_to_panel(self, component: Component) -> str | None:
        """Push this personality to the panel. Returns an error key or None.

        This is the Control4 driver's SAVE: the panel stores the configuration
        and **restarts**. Skipped for a dimmer or shade with no target, since
        the panel would be given a page controlling nothing.
        """
        if component.ignored:
            return None

        runtime = getattr(self._get_entry(), "runtime_data", None)
        if runtime is None:
            return "not_loaded"

        provisioner = runtime.provisioner
        slot = component.slot

        # Writing restarts the panel, and twice today it has left the gateway's
        # bus loop stopped until someone power cycled it. So a save that would
        # send the same bytes as last time sends nothing: the panel already
        # holds exactly this configuration.
        writes = runtime.writes
        wanted = signature(payload_for(component))
        if (
            wanted is not None
            and writes is not None
            and writes.last_written(component.address, slot) == wanted
        ):
            _LOGGER.info(
                "Component %s page %s already holds this configuration; not "
                "writing it again",
                component.address,
                slot,
            )
            return None
        previous = self._existing or {}
        previous_kind = ComponentKind(previous.get(CONF_KIND, "unknown"))
        previous_slot = int(previous.get(CONF_SLOT, slot))
        try:
            # Clearing restarts the panel just as writing does, so anything
            # sent straight after a clear is lost while it reboots. Clear only
            # what genuinely has to go, then wait for the panel to come back.
            stale = (
                previous_kind.mold
                if previous_kind.configured and previous_kind is not component.kind
                else None
            )
            if stale is not None:
                _LOGGER.info(
                    "Component %s: clearing its %s page before writing %s",
                    component.address,
                    previous_kind.value,
                    component.kind.value,
                )
                await provisioner.clear_configuration(
                    component.address, previous_slot, stale
                )
                # Clearing does not restart the panel — only writing does — so
                # the write follows immediately.
                await asyncio.sleep(CLEAR_INTERVAL)

            if component.kind is ComponentKind.BUTTON:
                await provisioner.configure_buttons(
                    component.address, slot, component.buttons, component.name
                )
            elif component.kind is ComponentKind.DIMMER:
                if not component.target:
                    return None
                await provisioner.configure_dimmer(
                    component.address,
                    slot,
                    link_for(slot, LinkRole.BRIGHTNESS),
                    name=component.name,
                    colour_link=(
                        link_for(slot, LinkRole.COLOUR_TEMPERATURE)
                        if component.colour_temperature
                        else None
                    ),
                )
            elif component.kind is ComponentKind.SHADE:
                if not component.target:
                    return None
                await provisioner.configure_shade(
                    component.address,
                    slot,
                    link_for(slot, LinkRole.LEVEL),
                    name=component.name,
                    direction=component.direction,
                )
            else:
                return None

            if wanted is not None and writes is not None:
                writes.record(component.address, slot, wanted)
            # The panel reboots now, and everything else on it reboots with
            # it — which is the only way to learn what shares a panel.
            entry = self._get_entry()
            entry.async_create_background_task(
                self.hass,
                async_watch_restart_grouping(self.hass, entry, component.address),
                f"be3-grouping-{component.address}",
            )
        except NotConnected:
            return "not_connected"
        except ProvisioningError as err:
            _LOGGER.warning(
                "Could not configure component %s: %s", component.address, err
            )
            return "write_failed"
        return None

    def _siblings(self) -> list[Component]:
        """The other pages already configured on this component's address."""
        current = None
        if self.source == "reconfigure":
            current = self._get_reconfigure_subentry().subentry_id
        others: list[Component] = []
        for subentry in self._get_entry().subentries.values():
            if subentry.subentry_type != SUBENTRY_COMPONENT:
                continue
            if subentry.subentry_id == current:
                continue
            try:
                other = Component.from_dict(dict(subentry.data))
            except ConfigurationError:
                continue
            # Slots identify a page within one address, so only pages on the
            # same address compete for them.
            if other.address == self._page_address():
                others.append(other)
        return others

    def _page_labels(self) -> dict[str, str]:
        """Name the page being edited, so it is never mistaken for another."""
        address = self._page_address()
        slot = int((self._existing or {}).get(CONF_SLOT, 0)) or None
        name = (self._existing or {}).get(CONF_NAME) or ""
        return {
            "address": str(address) if address is not None else "new",
            "page": str(slot) if slot else "new",
            "name": name,
        }

    def _page_address(self) -> int | None:
        if self._address is not None:
            return self._address
        if self._existing:
            return int(self._existing.get(CONF_ADDRESS, -1))
        return None

    def _slot_owner(self, component: Component) -> str | None:
        """Name of another page on this address already using the slot."""
        for other in self._siblings():
            if other.slot == component.slot:
                return other.name or f"page {other.slot}"
        return None

    def _default_slot(self, existing: dict[str, Any] | None) -> int:
        if existing and existing.get(CONF_SLOT):
            return int(existing[CONF_SLOT])
        try:
            return next_free_slot(self._siblings())
        except ConfigurationError:
            return 1

    async def _async_save(self, user_input: dict[str, Any]) -> SubentryFlowResult:
        """Validate, optionally write to the panel, and store."""
        existing = self._existing
        payload = dict(user_input)
        payload[CONF_KIND] = self._kind.value
        if existing is None and self._address is not None:
            payload[CONF_ADDRESS] = self._address

        data = _component_data(payload, existing)
        try:
            component = Component.from_dict(data)
        except ConfigurationError as err:
            _LOGGER.debug("Rejected component configuration: %s", err)
            return self._retry("invalid_component")

        clash = self._slot_owner(component)
        if clash is not None:
            return self._retry("slot_in_use", {"slot_owner": clash})

        if user_input.get(CONF_APPLY, True):
            error = await self._async_write_to_panel(component)
            if error is not None:
                return self._retry(error)

        # Named for a person, addressed for the hardware: an installer picks
        # by name and every log line and service call uses the address.
        title = (
            f"{component.name} · {component.address}"
            if component.name
            else f"Component {component.address}"
        )
        if existing is None:
            runtime = getattr(self._get_entry(), "runtime_data", None)
            writes = getattr(runtime, "writes", None)
            if writes is not None:
                # Adding a page back is the way to undo dismissing it.
                writes.restore(component.address, component.slot)
            # A subentry flow takes its unique id here: async_set_unique_id is
            # a config flow's method and calling it on this one raises, which
            # reaches the installer as "Unknown error occurred" after the page
            # has already been written to the panel.
            return self.async_create_entry(
                title=title,
                data=data,
                unique_id=f"{component.address}:{component.slot}",
            )
        return self.async_update_and_abort(
            self._get_entry(),
            self._get_reconfigure_subentry(),
            data=data,
            title=title,
        )

    def _retry(
        self, error: str, placeholders: dict[str, str] | None = None
    ) -> SubentryFlowResult:
        """Show the type's own form again with an error on it."""
        step = {
            ComponentKind.BUTTON: "keypad",
            ComponentKind.DIMMER: "dimmer",
            ComponentKind.SHADE: "shade",
        }.get(self._kind, "user")
        return self.async_show_form(
            step_id=step,
            data_schema=vol.Schema({}),
            errors={"base": error},
            description_placeholders=placeholders,
        )

def _number(minimum: int, maximum: int) -> NumberSelector:
    """A box, not a slider: these are facts read off a wall, not explorations."""
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum, max=maximum, step=1, mode=NumberSelectorMode.BOX
        )
    )


def _shared_fields(
    current: dict[str, Any], default_slot: int, *, editable_slot: bool = True
) -> dict[Any, Any]:
    """Settings every component has, whatever it is.

    The page number is fixed once a page exists: it is half of that page's
    identity, and changing it here would leave the panel showing a page
    nothing listens to any more. Adding a page is a separate act.
    """
    fields: dict[Any, Any] = {}
    if editable_slot:
        fields[
            vol.Optional(CONF_SLOT, default=int(current.get(CONF_SLOT, default_slot)))
        ] = _number(1, MAX_DEVICE_INDEX)
    return fields | {
        vol.Optional(
            CONF_IGNORED, default=bool(current.get(CONF_IGNORED, False))
        ): BooleanSelector(),
        vol.Optional(CONF_PANEL, default=current.get(CONF_PANEL, "")): cv.string,
        # Writing the configuration restarts the panel, so it is deliberate.
        vol.Optional(CONF_APPLY, default=True): BooleanSelector(),
    }


def _component_data(
    user_input: dict[str, Any], existing: dict[str, Any] | None
) -> dict[str, Any]:
    """Build stored component data from a submitted form."""
    kind = ComponentKind(user_input[CONF_KIND])
    address = (
        int(user_input[CONF_ADDRESS])
        if CONF_ADDRESS in user_input
        else int((existing or {})[CONF_ADDRESS])
    )
    data: dict[str, Any] = {
        CONF_ADDRESS: address,
        CONF_KIND: kind.value,
        # A chosen type is a decision to protect from later traffic. Leaving it
        # unknown is the absence of one — and saving the form that way is how
        # you hand a component back to self-identification.
        "manual": kind.configured,
        CONF_SLOT: int(user_input.get(CONF_SLOT, (existing or {}).get(CONF_SLOT, 1))),
    }
    # Only keypads carry a button count; keeping a stale one would fail
    # validation the next time the form is opened.
    if kind is ComponentKind.BUTTON:
        data[CONF_BUTTONS] = int(user_input.get(CONF_BUTTONS, DEFAULT_BUTTONS))
    if user_input.get(CONF_NAME):
        data[CONF_NAME] = user_input[CONF_NAME]
    if user_input.get(CONF_PANEL):
        data[CONF_PANEL] = user_input[CONF_PANEL]
    if user_input.get(CONF_IGNORED):
        data[CONF_IGNORED] = True

    if kind in (ComponentKind.DIMMER, ComponentKind.SHADE):
        if user_input.get(CONF_TARGET):
            data[CONF_TARGET] = user_input[CONF_TARGET]
        if user_input.get(CONF_LEFTOVER):
            data[CONF_LEFTOVER] = user_input[CONF_LEFTOVER]
        if kind is ComponentKind.DIMMER:
            # A feature exists on the panel because a link was written for it,
            # so this choice decides what the screen offers.
            data[CONF_COLOUR] = bool(user_input.get(CONF_COLOUR))
        else:
            data[CONF_DIRECTION] = int(user_input.get(CONF_DIRECTION, 4))
    return data


