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
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_HOST,
    CONF_MAC,
    CONF_MODEL,
    CONF_PORT,
    DOMAIN,
    SUBENTRY_COMPONENT,
)
from .devices import Component, ComponentKind, ConfigurationError, next_free_slot
from .discovery import async_discover, async_probe
from .protocol import DEFAULT_TCP_PORT, MAX_ADDRESS, MAX_BUTTONS, MAX_DEVICE_INDEX

_LOGGER = logging.getLogger(__name__)

MANUAL = "manual"

CONF_ADDRESS = "address"
CONF_KIND = "kind"
CONF_BUTTONS = "buttons"
CONF_SLOT = "slot"
CONF_NAME = "name"
CONF_PANEL = "panel"


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
    """Say what a component is, from its own device page."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Add a component by hand.

        Rarely needed: components appear on their own from the gateway's
        heartbeat. This covers one that is not reporting yet — a panel waiting
        to be wired, or one being configured before it is installed.
        """
        return await self._async_form(user_input, existing=None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change an existing component's settings."""
        subentry = self._get_reconfigure_subentry()
        return await self._async_form(user_input, existing=dict(subentry.data))

    async def _async_form(
        self, user_input: dict[str, Any] | None, existing: dict[str, Any] | None
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            data = _component_data(user_input, existing)
            try:
                component = Component.from_dict(data)
            except ConfigurationError as err:
                _LOGGER.debug("Rejected component configuration: %s", err)
                errors["base"] = "invalid_component"
            else:
                # Two components in one gateway slot overwrite each other's
                # configuration on the bus, so refuse it here where it is still
                # a typo rather than a site visit.
                clash = self._slot_owner(component)
                if clash is not None:
                    _LOGGER.debug("Slot %s already used by %s", component.slot, clash)
                    errors["base"] = "slot_in_use"
                    return self.async_show_form(
                        step_id="reconfigure" if existing else "user",
                        data_schema=_component_schema(
                            existing, editable_address=existing is None
                        ),
                        errors=errors,
                        description_placeholders={"slot_owner": clash},
                    )
                title = component.name or f"Component {component.address}"
                if existing is None:
                    await self.async_set_unique_id(str(component.address))
                    return self.async_create_entry(title=title, data=data)
                return self.async_update_and_abort(
                    self._get_entry(),
                    self._get_reconfigure_subentry(),
                    data=data,
                    title=title,
                )

        return self.async_show_form(
            step_id="reconfigure" if existing else "user",
            data_schema=_component_schema(
                existing,
                editable_address=existing is None,
                default_slot=self._default_slot(existing),
            ),
            errors=errors,
        )

    def _siblings(self) -> list[Component]:
        """The other components already configured on this gateway."""
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
                others.append(Component.from_dict(dict(subentry.data)))
            except ConfigurationError:
                continue
        return others

    def _slot_owner(self, component: Component) -> str | None:
        """Name of a configured sibling already using this component's slot."""
        if not component.kind.configured:
            return None
        for other in self._siblings():
            if other.kind.configured and other.slot == component.slot:
                return other.name or f"address {other.address}"
        return None

    def _default_slot(self, existing: dict[str, Any] | None) -> int:
        if existing and existing.get(CONF_SLOT):
            return int(existing[CONF_SLOT])
        try:
            return next_free_slot(self._siblings())
        except ConfigurationError:
            return 1


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
        CONF_SLOT: int(user_input.get(CONF_SLOT, (existing or {}).get(CONF_SLOT, 1))),
    }
    # Only keypads carry a button count; keeping a stale one would fail
    # validation the next time the form is opened.
    if kind is ComponentKind.BUTTON:
        data[CONF_BUTTONS] = int(user_input.get(CONF_BUTTONS, MAX_BUTTONS))
    if user_input.get(CONF_NAME):
        data[CONF_NAME] = user_input[CONF_NAME]
    if user_input.get(CONF_PANEL):
        data[CONF_PANEL] = user_input[CONF_PANEL]
    return data


def _component_schema(
    existing: dict[str, Any] | None,
    *,
    editable_address: bool,
    default_slot: int = 1,
) -> vol.Schema:
    current = existing or {}
    fields: dict[Any, Any] = {}

    if editable_address:
        fields[vol.Required(CONF_ADDRESS)] = vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_ADDRESS)
        )

    fields[vol.Required(CONF_KIND, default=current.get(CONF_KIND, "unknown"))] = (
        SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=kind.value, label=kind.value)
                    for kind in ComponentKind
                ],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="component_kind",
            )
        )
    )
    fields[
        vol.Optional(CONF_BUTTONS, default=current.get(CONF_BUTTONS, MAX_BUTTONS))
    ] = vol.All(vol.Coerce(int), vol.Range(min=1, max=MAX_BUTTONS))
    fields[vol.Optional(CONF_SLOT, default=current.get(CONF_SLOT, default_slot))] = vol.All(
        vol.Coerce(int), vol.Range(min=1, max=MAX_DEVICE_INDEX)
    )
    fields[vol.Optional(CONF_NAME, default=current.get(CONF_NAME, ""))] = cv.string
    fields[vol.Optional(CONF_PANEL, default=current.get(CONF_PANEL, ""))] = cv.string

    return vol.Schema(fields)
