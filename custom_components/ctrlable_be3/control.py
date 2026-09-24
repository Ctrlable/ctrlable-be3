"""Acting on what a panel asks for, and telling it what is true.

A provisioned dimmer or shade does two things forever: it writes when someone
touches it, and it reads so its screen can show the real value. Both arrive as
``setVal`` messages quoting the link they concern, and both have to be handled
or the panel is decorative — it will happily display a level nobody is
maintaining.

Answering reads matters as much as acting on writes. The panel polls every few
seconds; a controller that stays silent leaves the screen showing whatever it
last assumed, which is usually zero.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_CLOSE_COVER,
    SERVICE_OPEN_COVER,
    SERVICE_STOP_COVER,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant, callback

from .devices import Component
from .gateway import BE3Gateway
from .links import (
    Intent,
    LinkRole,
    device_id_for,
    interpret,
    leftover_role,
    link_for,
    role_matches,
)
from .protocol import Message, ValueRequest, build_value_response
from .values import (
    brightness_to_percent,
    kelvin_to_percent,
    percent_to_brightness,
    percent_to_kelvin,
    position_to_percent,
)

_LOGGER = logging.getLogger(__name__)


class ComponentController:
    """Drives one component's target and answers its polls."""

    def __init__(
        self, hass: HomeAssistant, gateway: BE3Gateway, component: Component
    ) -> None:
        self.hass = hass
        self._gateway = gateway
        self._component = component
        self._warned: set[str] = set()

    @property
    def component(self) -> Component:
        return self._component

    async def async_handle(self, request: ValueRequest) -> None:
        """Act on one request from the panel."""
        decoded = interpret(request, self._component.slot)
        if decoded is None:
            self._warn_foreign_link(request)
            return

        target = self._target_for(decoded.role)
        if target is None:
            # A page left over from what this component used to be, with
            # nothing assigned to it. The panel keeps such pages forever, and
            # obeying one blindly means driving a cover with a light's
            # commands.
            self._warn_stale_page(decoded.role)
            return
        if not target:
            return

        _LOGGER.debug(
            "Component %s page %s: %s %s value=%s",
            self._component.address,
            self._component.slot,
            decoded.intent.name,
            decoded.role.value if decoded.role else "unknown",
            decoded.value,
        )

        if decoded.intent is Intent.REPORT:
            await self._async_report(request, decoded.role, target)
            return

        try:
            await self._async_act(target, decoded.intent, decoded.value)
        except Exception:  # noqa: BLE001 - a bad target must not kill the session
            _LOGGER.exception(
                "Component %s could not drive %s", self._component.address, target
            )

    def _target_for(self, role: LinkRole | None) -> str | None:
        """What a request about this role should drive, if anything.

        A page answers for two things: the personality it has now, and the one
        it used to have, because nothing removes a configuration from a panel.
        The second only counts once someone has said what it should drive —
        until then the control exists on the wall but means nothing, and
        guessing would be worse than ignoring it.
        """
        if role_matches(self._component.kind, role):
            return self._component.target or ""
        if role is not None and role is leftover_role(self._component.kind):
            return self._component.leftover_target or None
        return None

    def _warn_stale_page(self, role: LinkRole | None) -> None:
        """Say once that the panel still has a page from a previous type."""
        key = f"stale:{role}"
        if key in self._warned:
            return
        self._warned.add(key)
        _LOGGER.warning(
            "Component %s page %s still shows a %s control from before it "
            "became a %s, and nothing removes a page from a panel. Open its "
            "settings and set 'Leftover control' to whatever it should drive, "
            "or leave it: it does nothing until you do.",
            self._component.address,
            self._component.slot,
            role.value if role else "unknown",
            self._component.kind.value,
        )

    def _warn_foreign_link(self, request: ValueRequest) -> None:
        """Say when a panel is quoting a configuration we did not write.

        A panel keeps whatever it was last told, so one migrated from another
        system asks about that system's links and silently does nothing here.
        Said once per link, because the panel repeats itself every few seconds.
        """
        link = f"{request.device_id},{request.attribute_id}"
        if link in self._warned:
            return
        self._warned.add(link)
        _LOGGER.warning(
            "Component %s is asking about link %s, which this integration did "
            "not write — the panel still holds another controller's "
            "configuration. Open its settings and save with 'Write this to the "
            "panel' to take it over. Expected %s.",
            self._component.address,
            link,
            link_for(self._component.slot, LinkRole.BRIGHTNESS),
        )

    async def _async_act(self, target: str, intent: Intent, value: int | None) -> None:
        domain = target.split(".", 1)[0]

        if intent is Intent.TURN_ON:
            service = SERVICE_OPEN_COVER if domain == "cover" else SERVICE_TURN_ON
            await self._call(domain, service, {ATTR_ENTITY_ID: target})
        elif intent is Intent.TURN_OFF:
            service = SERVICE_CLOSE_COVER if domain == "cover" else SERVICE_TURN_OFF
            await self._call(domain, service, {ATTR_ENTITY_ID: target})
        elif intent is Intent.SET_BRIGHTNESS and value is not None:
            if value <= 0:
                # A panel dimmed to nothing means off, not a light at zero
                # brightness, which some drivers treat as "on but invisible".
                await self._call(domain, SERVICE_TURN_OFF, {ATTR_ENTITY_ID: target})
            else:
                await self._call(
                    domain,
                    SERVICE_TURN_ON,
                    {
                        ATTR_ENTITY_ID: target,
                        "brightness": percent_to_brightness(value),
                    },
                )
        elif intent is Intent.SET_COLOUR_TEMPERATURE and value is not None:
            low, high = self._kelvin_range(target)
            await self._call(
                domain,
                SERVICE_TURN_ON,
                {
                    ATTR_ENTITY_ID: target,
                    "color_temp_kelvin": percent_to_kelvin(value, low, high),
                },
            )
        elif intent is Intent.OPEN:
            await self._call(domain, SERVICE_OPEN_COVER, {ATTR_ENTITY_ID: target})
        elif intent is Intent.CLOSE:
            await self._call(domain, SERVICE_CLOSE_COVER, {ATTR_ENTITY_ID: target})
        elif intent is Intent.STOP:
            await self._call(domain, SERVICE_STOP_COVER, {ATTR_ENTITY_ID: target})

    async def _call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        _LOGGER.debug("Component %s: %s.%s %s", self._component.address, domain, service, data)
        await self.hass.services.async_call(domain, service, data, blocking=False)

    async def _async_report(
        self, request: ValueRequest, role: LinkRole | None, target: str
    ) -> None:
        """Answer a poll with the current value of what this role drives."""
        value = self._current_value(role, target)
        if value is None:
            # Nothing to say: the target is unavailable, or a cover that does
            # not report its position. The panel keeps its last reading.
            _LOGGER.debug(
                "Component %s page %s: no value to report for %s (%s is %s)",
                self._component.address,
                self._component.slot,
                role.value if role else "unknown",
                target,
                getattr(self.hass.states.get(target), "state", None),
            )
            return
        await self._gateway.send(
            build_value_response(
                self._component.address,
                self._component.slot,
                device_id_for(self._component.slot),
                request.attribute_id,
                value,
            )
        )

    def _current_value(self, role: LinkRole | None, target: str) -> int | None:
        state = self.hass.states.get(target)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None

        if role is LinkRole.COLOUR_TEMPERATURE:
            low, high = self._kelvin_range(target)
            return kelvin_to_percent(
                state.attributes.get("color_temp_kelvin"), low, high
            )

        if role is LinkRole.LEVEL:
            return position_to_percent(state.attributes.get("current_position"))

        # Brightness, and anything else that means "how much is it on".
        if state.state == STATE_OFF:
            return 0
        brightness = state.attributes.get("brightness")
        if brightness is None:
            # A switch, or a light without brightness support: on is 100%.
            return 100
        return brightness_to_percent(brightness)

    def _kelvin_range(self, target: str) -> tuple[int | None, int | None]:
        state = self.hass.states.get(target)
        if state is None:
            return None, None
        return (
            state.attributes.get("min_color_temp_kelvin"),
            state.attributes.get("max_color_temp_kelvin"),
        )


class ControlDispatcher:
    """Routes panel requests to the controller that owns the address."""

    def __init__(self, hass: HomeAssistant, gateway: BE3Gateway) -> None:
        self.hass = hass
        self._gateway = gateway
        self._controllers: dict[tuple[int, int], ComponentController] = {}
        self._targetless: dict[tuple[int, int], Component] = {}
        self._orphans: set[tuple[int, int]] = set()

    def set_components(self, components: list[Component]) -> None:
        # Keyed by address and type: one screen can hold a dimmer page and a
        # shade page at once, and only the link says which one a message is
        # about.
        self._controllers = {
            (component.address, component.slot): ComponentController(
                self.hass, self._gateway, component
            )
            for component in components
            if component.controllable
        }
        # Kept apart from the controllers: a page configured here but with no
        # target looks exactly like an unknown page on the wire, and the two
        # need different advice.
        self._targetless = {
            (component.address, component.slot): component
            for component in components
            if not component.controllable and not component.ignored
        }
        self._orphans.clear()

    @callback
    def handle_message(self, message: Message) -> None:
        if not isinstance(message, ValueRequest):
            return
        # The panel quotes the slot it was configured with as idx, so a
        # screen's pages are told apart exactly rather than by guesswork.
        key = (message.address, message.index)
        controller = self._controllers.get(key)
        if controller is None:
            self._warn_orphan(key)
            return
        self.hass.async_create_task(controller.async_handle(message))

    def _warn_orphan(self, key: tuple[int, int]) -> None:
        """Say why a page is asking and getting no answer.

        A page keeps asking whatever happens to its settings here, and a page
        that gets no answer looks dead on the wall with nothing explaining
        why. There are two reasons for it and they need different advice: the
        page exists here but controls nothing, or the page is not configured
        here at all.
        """
        if key in self._orphans:
            return
        self._orphans.add(key)
        address, slot = key

        known = self._targetless.get(key)
        if known is not None:
            _LOGGER.warning(
                "Component %s page %s (%s) is configured here as a %s but has "
                "nothing to control, so its polls go unanswered and its screen "
                "shows nothing. Open its settings and choose what it controls.",
                address,
                slot,
                known.name or "unnamed",
                known.kind.value,
            )
            return

        _LOGGER.warning(
            "Component %s is showing page %s, which nothing here configures. "
            "A page cannot be removed from a panel, so take it over: add a "
            "component for address %s page %s and give it something to "
            "control.",
            address,
            slot,
            address,
            slot,
        )
