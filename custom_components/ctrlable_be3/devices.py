"""How bus components map onto devices and entities.

One bus component is one device. A SUBLIME Pro 6 presents its buttons at one
address and its small screen at another, so it appears as two devices — which
is what its two addresses are. Components that belong to one physical panel can
share a panel label, which groups them by area without pretending they are a
single device.

Identity is derived from the gateway's MAC address, which is stable across
DHCP leases, plus the component address. Re-addressing a component therefore
creates new entities rather than silently rebinding the old ones, which is the
safer failure: a renamed entity is visible, a mis-bound one is not.

No Home Assistant imports, so the rules here are testable on their own.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from .protocol import (
    MAX_ADDRESS,
    MAX_BUTTONS,
    MAX_DEVICE_INDEX,
    MIN_ADDRESS,
    MOLD_BUTTON,
    MOLD_DIMMER,
    MOLD_SHADE,
)


class ComponentKind(str, Enum):
    """What a component has been configured to be.

    The gateway reports addresses but never says what lives at one, so a newly
    discovered component is ``UNKNOWN`` until someone says otherwise. That is a
    real state, not a placeholder: an unknown component still gets a device and
    an identify button, which is how an installer finds out what it is.
    """

    UNKNOWN = "unknown"
    BUTTON = "button"
    DIMMER = "dimmer"
    SHADE = "shade"

    @property
    def mold(self) -> str | None:
        """The identifier used on the wire, or None if not yet known."""
        return {
            ComponentKind.BUTTON: MOLD_BUTTON,
            ComponentKind.DIMMER: MOLD_DIMMER,
            ComponentKind.SHADE: MOLD_SHADE,
        }.get(self)

    @property
    def configured(self) -> bool:
        return self is not ComponentKind.UNKNOWN


class ConfigurationError(ValueError):
    """A component definition is not usable."""


@dataclass(frozen=True)
class Component:
    """One addressable component on the bus.

    Created either from a heartbeat (kind ``UNKNOWN``, nothing else known) or
    from what an installer has since told us about it.
    """

    address: int
    kind: ComponentKind = ComponentKind.UNKNOWN
    #: Logical slot, the gateway's devIdx. Unique per gateway, not per panel.
    slot: int = 1
    #: Button count, for keypads only.
    buttons: int = 0
    #: Installer's label for this component.
    name: str | None = None
    #: Groups components belonging to one physical panel, for area grouping.
    panel: str | None = None
    #: True when a person set these values, which makes them authoritative:
    #: traffic may then contradict them but never overrides them.
    manual: bool = False
    #: What this component controls: a Home Assistant entity id. A dimmer or
    #: shade with no target has nothing to drive, so it is not provisioned.
    target: str | None = None
    #: What the page's *leftover* control drives, if it has one. A page keeps
    #: the configuration of every personality it has ever had, so a dimmer
    #: written over a shade still shows a shade control and still asks about
    #: it. Pointing that at something is the only way to make it useful: the
    #: panel will not give the page up.
    leftover_target: str | None = None
    #: Whether to give a dimmer a colour-temperature page. Features exist on
    #: the panel because a link was written for them, so this is not decoration:
    #: it decides what the screen offers.
    colour_temperature: bool = False
    #: Brightness range written to the panel as "min,max,step".
    brightness_min: int = 0
    brightness_max: int = 100
    brightness_step: int = 5
    #: Shade orientation, as the vendor numbers them.
    direction: int = 4
    #: Not a controllable component. The gateway reports everything it sees on
    #: the bus, including parts of itself — its WiFi module appears as an
    #: ordinary address that answers nothing and configures nothing.
    ignored: bool = False

    def __post_init__(self) -> None:
        if not MIN_ADDRESS <= self.address <= MAX_ADDRESS:
            raise ConfigurationError(
                f"Address must be between {MIN_ADDRESS} and {MAX_ADDRESS}, "
                f"got {self.address}"
            )
        if not 1 <= self.slot <= MAX_DEVICE_INDEX:
            raise ConfigurationError(
                f"Device slot must be between 1 and {MAX_DEVICE_INDEX}, "
                f"got {self.slot}"
            )
        if self.kind is ComponentKind.BUTTON:
            if not 1 <= self.buttons <= MAX_BUTTONS:
                raise ConfigurationError(
                    f"A keypad must have between 1 and {MAX_BUTTONS} buttons, "
                    f"got {self.buttons}"
                )
        elif self.buttons:
            raise ConfigurationError(
                f"Only keypads have buttons; {self.kind.value} at address "
                f"{self.address} declares {self.buttons}"
            )

    @property
    def brightness_limits(self) -> str:
        """The range as the panel wants it: "min,max,step"."""
        return f"{self.brightness_min},{self.brightness_max},{self.brightness_step}"

    @property
    def controllable(self) -> bool:
        """Whether this component has something to drive."""
        if self.ignored:
            return False
        return self.kind in (ComponentKind.DIMMER, ComponentKind.SHADE) and bool(
            self.target
        )

    @property
    def button_range(self) -> range:
        return range(1, self.buttons + 1)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "address": self.address,
            "kind": self.kind.value,
            "slot": self.slot,
        }
        if self.buttons:
            data["buttons"] = self.buttons
        if self.name:
            data["name"] = self.name
        if self.panel:
            data["panel"] = self.panel
        if self.manual:
            data["manual"] = True
        if self.ignored:
            data["ignored"] = True
        if self.target:
            data["target"] = self.target
        if self.leftover_target:
            data["leftover_target"] = self.leftover_target
        if self.colour_temperature:
            data["colour_temperature"] = True
        if self.kind is ComponentKind.DIMMER:
            data["brightness_min"] = self.brightness_min
            data["brightness_max"] = self.brightness_max
            data["brightness_step"] = self.brightness_step
        if self.kind is ComponentKind.SHADE:
            data["direction"] = self.direction
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Component:
        try:
            kind = ComponentKind(data["kind"])
        except (KeyError, ValueError) as err:
            raise ConfigurationError(
                f"Unknown component type {data.get('kind')!r}"
            ) from err
        try:
            address = int(data["address"])
        except (KeyError, TypeError, ValueError) as err:
            raise ConfigurationError("Component is missing an address") from err
        return cls(
            address=address,
            kind=kind,
            slot=int(data.get("slot", 1)),
            buttons=int(data.get("buttons", 0)),
            name=data.get("name") or None,
            panel=data.get("panel") or None,
            manual=bool(data.get("manual", False)),
            ignored=bool(data.get("ignored", False)),
            target=data.get("target") or None,
            leftover_target=data.get("leftover_target") or None,
            colour_temperature=bool(data.get("colour_temperature", False)),
            brightness_min=int(data.get("brightness_min", 0)),
            brightness_max=int(data.get("brightness_max", 100)),
            brightness_step=int(data.get("brightness_step", 5)),
            direction=int(data.get("direction", 4)),
        )


def parse_components(raw: Iterable[dict[str, Any]]) -> tuple[Component, ...]:
    """Build components from stored configuration, rejecting duplicates."""
    components = tuple(Component.from_dict(item) for item in raw)

    # One page per slot per address. A screen holds many pages, and the panel
    # tells them apart by the slot it quotes back in every message.
    seen: set[tuple[int, int]] = set()
    for component in components:
        key = (component.address, component.slot)
        if key in seen:
            raise ConfigurationError(
                f"Address {component.address} already has a page in slot "
                f"{component.slot}"
            )
        seen.add(key)

    # Only configured components own a slot on the gateway; discovered ones
    # keep the default until someone says what they are.
    slots: dict[int, int] = {}
    for component in components:
        if not component.kind.configured:
            continue
        if component.slot in slots:
            raise ConfigurationError(
                f"Device slot {component.slot} is used by both address "
                f"{slots[component.slot]} and address {component.address}"
            )
        slots[component.slot] = component.address

    return components


def dump_components(components: Iterable[Component]) -> list[dict[str, Any]]:
    return [component.to_dict() for component in components]


def assign_slots(components: Iterable[Component]) -> tuple[Component, ...]:
    """Give every page on an address a distinct slot.

    Slots are per address, not global: the panel quotes the slot back with the
    address, so two panels may both use slot 1 without ambiguity. Pages on one
    address must differ, because the slot is how that panel tells its pages
    apart.
    """
    ordered = sorted(components, key=lambda item: (item.address, item.slot))
    taken: dict[int, set[int]] = {}
    result: list[Component | None] = []

    # First pass: keep slots that are already distinct for their address.
    for component in ordered:
        used = taken.setdefault(component.address, set())
        if component.slot not in used and 1 <= component.slot <= MAX_DEVICE_INDEX:
            used.add(component.slot)
            result.append(component)
        else:
            result.append(None)

    # Second pass: fill the gaps left by clashes.
    for index, component in enumerate(result):
        if component is not None:
            continue
        original = ordered[index]
        used = taken.setdefault(original.address, set())
        slot = next(
            (s for s in range(1, MAX_DEVICE_INDEX + 1) if s not in used), None
        )
        if slot is None:
            raise ConfigurationError(
                f"Address {original.address} cannot hold more than "
                f"{MAX_DEVICE_INDEX} pages"
            )
        used.add(slot)
        result[index] = replace(original, slot=slot)

    return tuple(component for component in result if component is not None)


def next_free_slot(components: Iterable[Component]) -> int:
    """Pick a slot not already used by another page on the same address."""
    used = {component.slot for component in components}
    for slot in range(1, MAX_DEVICE_INDEX + 1):
        if slot not in used:
            return slot
    raise ConfigurationError(
        f"All {MAX_DEVICE_INDEX} device slots are in use; remove one first"
    )


# ---------------------------------------------------------------------------
# Entity and device identity
# ---------------------------------------------------------------------------


def keypad_id(mac: str, address: int, slot: int) -> str:
    """Identity of one page, stable across restarts and DHCP leases.

    Three parts, each earning its place: the gateway's MAC, because two
    gateways can both have an address 16; the address, because a bus has many
    components; and the page, because one component can hold several.

    This is what the Buttons Machine backend stores as its ``device_serial``
    and compares against the events we publish, and it is the stem of every
    entity id below, so LEDs can be found from the serial alone.
    """
    return f"{_normalise_mac(mac)}-{address}-{slot}"


def gateway_device_id(mac: str) -> str:
    return f"be3-{_normalise_mac(mac)}"


def component_device_id(mac: str, address: int, slot: int) -> str:
    """Device identity for one page on the bus.

    Keyed by address *and* slot. A screen holds a page per slot it has been
    given — a light on one, a shade on another — and the panel quotes the slot
    back as ``idx`` in everything it sends, so that pair is what identifies a
    page. Keying by address alone would let one page overwrite another.
    """
    return f"be3-{keypad_id(mac, address, slot)}"


def button_unique_id(mac: str, address: int, slot: int, button: int) -> str:
    return f"be3-{keypad_id(mac, address, slot)}-button{button}"


def led_unique_id(mac: str, address: int, slot: int, button: int) -> str:
    return f"be3-{keypad_id(mac, address, slot)}-led{button}"


def led_unique_id_prefix(serial: str) -> str:
    """Prefix the backend scans for when mapping buttons to LED entities."""
    return f"be3-{serial}-led"


def value_unique_id(mac: str, address: int, slot: int) -> str:
    return f"be3-{keypad_id(mac, address, slot)}-value"


def identify_unique_id(mac: str, address: int, slot: int) -> str:
    return f"be3-{keypad_id(mac, address, slot)}-identify"


def _normalise_mac(mac: str) -> str:
    """Compare MACs without caring about separators or case."""
    return mac.replace(":", "").replace("-", "").lower()
