"""What a component's settings become on the wire.

Kept in one place because two callers need the same answer to different
questions: the provisioner asks what to send, and the flow asks whether it
differs from what was sent last time. Writing is the only thing a panel
honours, but it is also what restarts it and what has twice left the gateway's
bus loop stopped — so a write that changes nothing is worth not making.
"""

from __future__ import annotations

import json
from typing import Any

from .devices import Component, ComponentKind
from .links import LinkRole, link_for
from .protocol import (
    build_configure_buttons,
    build_configure_dimmer,
    build_configure_shade,
)
from .provisioning import truncate_name


def payload_for(component: Component) -> dict[str, Any] | None:
    """The message that writes this component's settings, or None.

    None means there is nothing to write: a component whose type is unknown, a
    dimmer or shade with nothing to control, or one marked as not a device.
    Those are not errors — a page with no target would leave the panel showing
    a control for nothing.
    """
    if component.ignored:
        return None

    slot = component.slot
    name = truncate_name(component.name) if component.name else None

    if component.kind is ComponentKind.BUTTON:
        return build_configure_buttons(
            component.address, slot, component.buttons, name=name
        )

    if component.kind is ComponentKind.DIMMER:
        if not component.target:
            return None
        return build_configure_dimmer(
            component.address,
            slot,
            link_for(slot, LinkRole.BRIGHTNESS),
            name=name,
            colour_link=(
                link_for(slot, LinkRole.COLOUR_TEMPERATURE)
                if component.colour_temperature
                else None
            ),
        )

    if component.kind is ComponentKind.SHADE:
        if not component.target:
            return None
        return build_configure_shade(
            component.address,
            slot,
            link_for(slot, LinkRole.LEVEL),
            name=name,
            direction=component.direction,
        )

    return None


def signature(payload: dict[str, Any] | None) -> str | None:
    """A stable string for comparing one write against the last one.

    Key order is not stable across Python runs of a dict built by different
    code paths, so compare the sorted form rather than the dict itself.
    """
    if payload is None:
        return None
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))
