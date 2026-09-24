"""Links: how a panel addresses a property of the thing it controls.

A link is the pair ``<devId>,<devAtrId>`` written into a component's
configuration. The panel stores it, shows a property page for it, and quotes it
back in every read and write it makes. The numbers mean nothing to the panel —
Control4 fills them with its own device ids — so they are ours to assign, and
what we assign them to is a *property*.

That is the whole trick. A dimmer panel reports brightness and colour
temperature with the same type code (``0xE3``); only the link tells them apart.
Give a panel one link and its screen offers one property; give it two and it
offers both.

No Home Assistant imports, so the mapping is testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .protocol import (
    TYPE_BRIGHTNESS,
    TYPE_OFF,
    TYPE_ON,
    TYPE_SHADE_LEVEL,
    TYPE_SHADE_STOP,
    ValueAction,
    ValueRequest,
    decode_brightness,
    describe_value,
)

#: Base of the device ids we hand out. Arbitrary, like Control4's own: it only
#: has to be stable, and distinct from the attribute ids so a swapped pair is
#: obvious in a log.
DEVICE_ID_BASE = 900

#: Attribute ids, one per property a panel can be given a page for.
ATTR_BRIGHTNESS = 1001
ATTR_COLOUR_TEMPERATURE = 1002
ATTR_LEVEL = 1003


class LinkRole(str, Enum):
    """Which property of the target a link addresses."""

    BRIGHTNESS = "brightness"
    COLOUR_TEMPERATURE = "colour_temperature"
    LEVEL = "level"

    @property
    def attribute_id(self) -> int:
        return {
            LinkRole.BRIGHTNESS: ATTR_BRIGHTNESS,
            LinkRole.COLOUR_TEMPERATURE: ATTR_COLOUR_TEMPERATURE,
            LinkRole.LEVEL: ATTR_LEVEL,
        }[self]


class Intent(str, Enum):
    """What the panel is asking the controller to do."""

    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"
    SET_BRIGHTNESS = "set_brightness"
    SET_COLOUR_TEMPERATURE = "set_colour_temperature"
    OPEN = "open"
    CLOSE = "close"
    STOP = "stop"
    REPORT = "report"


@dataclass(frozen=True)
class Request:
    """A decoded panel request: what to do, to which property, with what."""

    intent: Intent
    role: LinkRole | None = None
    value: int | None = None


def device_id_for(slot: int) -> int:
    """The device id a component's links share."""
    return DEVICE_ID_BASE + slot


def link_for(slot: int, role: LinkRole) -> str:
    """The link string written into a component's configuration."""
    return f"{device_id_for(slot)},{role.attribute_id}"


def role_of(request: ValueRequest, slot: int) -> LinkRole | None:
    """Which property a request addresses, or None if it is not ours.

    A panel still holding another controller's configuration quotes that
    controller's ids, which is how we tell a stale panel from a provisioned
    one instead of acting on a link we never wrote.
    """
    if request.device_id != device_id_for(slot):
        return None
    for role in LinkRole:
        if request.attribute_id == role.attribute_id:
            return role
    return None


def interpret(request: ValueRequest, slot: int) -> Request | None:
    """Decode a panel request into what it wants done.

    Returns None when the request belongs to someone else's configuration, or
    is a write we do not model.
    """
    role = role_of(request, slot)
    if role is None:
        return None

    if request.is_read:
        # The panel polls each link so its screen shows the real value.
        return Request(Intent.REPORT, role)

    if request.type_code == TYPE_ON:
        return Request(Intent.TURN_ON, role)
    if request.type_code == TYPE_OFF:
        return Request(Intent.TURN_OFF, role)

    if request.type_code == TYPE_BRIGHTNESS:
        value = decode_brightness(request.value)
        # Brightness and colour temperature share a type code; only the link
        # says which of them this is.
        if role is LinkRole.COLOUR_TEMPERATURE:
            return Request(Intent.SET_COLOUR_TEMPERATURE, role, value)
        return Request(Intent.SET_BRIGHTNESS, role, value)

    if request.type_code == TYPE_SHADE_LEVEL:
        if request.value == 100:
            return Request(Intent.OPEN, role)
        if request.value == 0:
            return Request(Intent.CLOSE, role)
        return Request(Intent.OPEN, role, request.value)
    if request.type_code == TYPE_SHADE_STOP:
        return Request(Intent.STOP, role)

    return None


def role_matches(kind, role: LinkRole | None) -> bool:
    """Whether a link belongs to what the component currently is.

    A panel keeps every configuration it has ever been given, so one changed
    from a dimmer to a shade still polls its old brightness link. Acting on
    that means driving a cover with a light's commands, so leftovers are
    ignored rather than obeyed.
    """
    if role is None:
        return False
    if kind.value == "dimmer":
        return role in (LinkRole.BRIGHTNESS, LinkRole.COLOUR_TEMPERATURE)
    if kind.value == "shade":
        return role is LinkRole.LEVEL
    return False


def leftover_role(kind) -> LinkRole | None:
    """The role a page of this type can still be asked about, but is not.

    A page keeps every configuration it has ever been given, and nothing
    removes one, so a page written as a dimmer after being a shade goes on
    polling its old level link for as long as the panel lives. There is
    exactly one such role per type, because a screen page has exactly two
    personalities available to it.

    Naming it is what lets an installer put it to work instead of leaving a
    control on the wall that does nothing.
    """
    if kind.value == "dimmer":
        return LinkRole.LEVEL
    if kind.value == "shade":
        return LinkRole.BRIGHTNESS
    return None


def describe(request: ValueRequest, slot: int) -> tuple[ValueAction, dict] | None:
    """Name a request, using the links to resolve what it addresses.

    Falls back to the link-agnostic reading for a panel still holding another
    controller's configuration: we cannot tell its brightness from its colour
    temperature, but reporting the press is better than reporting nothing
    while an installer is midway through migrating a site.
    """
    decoded = interpret(request, slot)
    if decoded is None:
        return describe_value(request)

    if decoded.intent is Intent.REPORT:
        # A poll is the panel asking, not a person acting.
        return None
    if decoded.intent is Intent.TURN_ON:
        return ValueAction.ON, {}
    if decoded.intent is Intent.TURN_OFF:
        return ValueAction.OFF, {}
    if decoded.intent is Intent.SET_BRIGHTNESS:
        return ValueAction.BRIGHTNESS, {"brightness": decoded.value}
    if decoded.intent is Intent.SET_COLOUR_TEMPERATURE:
        return ValueAction.COLOUR_TEMPERATURE, {"colour_temperature": decoded.value}
    if decoded.intent is Intent.OPEN:
        return ValueAction.OPEN, ({"level": decoded.value} if decoded.value else {})
    if decoded.intent is Intent.CLOSE:
        return ValueAction.CLOSE, {}
    if decoded.intent is Intent.STOP:
        return ValueAction.STOP, {}
    return None
