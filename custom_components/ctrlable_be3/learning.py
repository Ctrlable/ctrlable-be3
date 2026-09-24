"""Working out what a component is by watching what it sends.

The gateway reports that an address exists and nothing more — no model, no
type, no button count. But components give themselves away in traffic:

* a component that sends button events **is** a keypad, and every event names
  the button, so the highest number seen is a floor on how many it has
* a component that writes brightness or on/off **is** a dimmer
* a component that writes shade level or stop **is** a shade controller

This turns commissioning into something an installer already does: walk the
panels and press the buttons. Nothing here guesses beyond the evidence — an
untouched component stays unknown, and a count only ever grows, because seeing
button 4 proves there are at least four, never that there are exactly four.

No Home Assistant imports, so the inference is testable on its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace

from .devices import Component, ComponentKind
from .protocol import (
    MAX_BUTTONS,
    TYPE_BRIGHTNESS,
    TYPE_OFF,
    TYPE_ON,
    TYPE_SHADE_LEVEL,
    TYPE_SHADE_STOP,
    ButtonReport,
    ValueRequest,
)

_DIMMER_TYPES = frozenset({TYPE_BRIGHTNESS, TYPE_ON, TYPE_OFF})
_SHADE_TYPES = frozenset({TYPE_SHADE_LEVEL, TYPE_SHADE_STOP})


@dataclass(frozen=True)
class Observation:
    """What a component's own traffic says about it."""

    kind: ComponentKind = ComponentKind.UNKNOWN
    #: Highest button number seen; 0 when none have been pressed.
    buttons: int = 0

    @property
    def identified(self) -> bool:
        return self.kind is not ComponentKind.UNKNOWN


class BusObserver:
    """Accumulates evidence about each component on the bus."""

    def __init__(self) -> None:
        self._seen: dict[int, Observation] = {}

    def observe_button(self, report: ButtonReport) -> bool:
        """Record a button event. Returns True when something new was learned."""
        if not 1 <= report.button <= MAX_BUTTONS:
            # A button number outside the panel's range is not evidence of a
            # bigger keypad; it is evidence of a frame we misread.
            return False
        return self._update(
            report.address,
            kind=ComponentKind.BUTTON,
            buttons=report.button,
        )

    def observe_value(self, request: ValueRequest) -> bool:
        """Record a value write. Returns True when something new was learned."""
        if not request.is_write:
            # A read tells us the panel is linked to something, not what it is.
            return False
        if request.type_code in _DIMMER_TYPES:
            return self._update(request.address, kind=ComponentKind.DIMMER)
        if request.type_code in _SHADE_TYPES:
            return self._update(request.address, kind=ComponentKind.SHADE)
        return False

    def get(self, address: int) -> Observation:
        return self._seen.get(address, Observation())

    def __contains__(self, address: int) -> bool:
        return address in self._seen

    def _update(
        self, address: int, *, kind: ComponentKind, buttons: int = 0
    ) -> bool:
        current = self._seen.get(address, Observation())
        updated = current

        if current.kind is ComponentKind.UNKNOWN:
            updated = replace(updated, kind=kind)
        elif current.kind is not kind:
            # A component that has already spoken as one type and now speaks as
            # another is a component someone has just reconfigured. The newest
            # evidence wins, and a type change drops the old button count.
            updated = Observation(kind=kind)

        if kind is ComponentKind.BUTTON:
            updated = replace(updated, buttons=max(updated.buttons, buttons))

        if updated == current and address in self._seen:
            return False
        self._seen[address] = updated
        return True


def apply_observation(component: Component, seen: Observation) -> Component | None:
    """Return the component updated by an observation, or None to leave it be.

    Three rules, in order:

    * A component someone configured by hand is left alone. Their settings are
      a decision; traffic is only evidence.
    * A component still unknown takes the observation whole — and the result is
      **not** marked manual, because inferring something is not the same as
      being told it. Marking it would freeze the very component that has the
      most still to learn.
    * A keypad already known widens to fit a higher button number.
    """
    if not seen.identified:
        return None

    if component.manual and component.kind.configured:
        return None

    if not component.kind.configured:
        return replace(
            component, kind=seen.kind, buttons=seen.buttons, manual=False
        )

    if component.kind is ComponentKind.BUTTON and seen.buttons > component.buttons:
        return replace(component, buttons=seen.buttons)

    return None


def contradicts(observed: Observation, chosen: ComponentKind) -> bool:
    """Whether a chosen type disagrees with how the component behaves.

    Components are not interchangeable. A SUBLIME Pro's button block reports
    button events; its screen slots report value writes. Nothing in the
    protocol says which a component is, but the component itself has been
    saying so all along — so evidence that flatly contradicts a choice is
    worth stopping on, because writing the wrong personality leaves the
    component with no working configuration at all.
    """
    if not observed.identified or chosen is ComponentKind.UNKNOWN:
        return False
    if observed.kind is chosen:
        return False
    # A dimmer and a shade are both screen slots, so swapping those is an
    # ordinary change of mind rather than a category error.
    screen = {ComponentKind.DIMMER, ComponentKind.SHADE}
    if observed.kind in screen and chosen in screen:
        return False
    return True


def group_from_restart(
    before: Iterable[int],
    during: Iterable[int],
    after: Iterable[int],
    provisioned: int,
) -> tuple[int, ...]:
    """Work out which components share a panel with the one we just wrote to.

    Writing a personality restarts the whole panel, so every component on it
    leaves the bus together and comes back together. Nothing in the protocol
    says which addresses belong to one panel — this is the only signal that
    does, and it costs nothing because we caused the restart.

    Only components that were present, went away **and** returned count. A
    component that left and stayed away is a coincidence — someone unplugging
    something, or a bus fault — not evidence of shared hardware.
    """
    before_set, during_set, after_set = set(before), set(during), set(after)
    if provisioned not in before_set or provisioned in during_set:
        # We never saw this panel leave, so we have learned nothing.
        return ()

    grouped = {
        address
        for address in before_set
        if address not in during_set and address in after_set
    }
    if provisioned not in grouped:
        return ()
    return tuple(sorted(grouped))


def possible_kinds(observed: Observation) -> tuple[ComponentKind, ...]:
    """Which page types the hardware at an address can actually be.

    Components are not interchangeable: a panel's button block reports button
    events, its screen reports value writes, and telling one it is the other
    writes a configuration it cannot use. Since pages cannot be removed once
    created, offering an impossible type is worse than useless — it is
    permanent.

    Nothing observed means nothing ruled out: an address that has never spoken
    keeps every option, because the alternative is blocking a panel that is
    simply idle.
    """
    if observed.kind is ComponentKind.BUTTON:
        return (ComponentKind.UNKNOWN, ComponentKind.BUTTON)
    if observed.kind in (ComponentKind.DIMMER, ComponentKind.SHADE):
        # Both are screen pages, so either is a legitimate choice.
        return (ComponentKind.UNKNOWN, ComponentKind.DIMMER, ComponentKind.SHADE)
    return tuple(ComponentKind)
