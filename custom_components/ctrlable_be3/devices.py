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
from dataclasses import dataclass
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
        )


def parse_components(raw: Iterable[dict[str, Any]]) -> tuple[Component, ...]:
    """Build components from stored configuration, rejecting duplicates."""
    components = tuple(Component.from_dict(item) for item in raw)

    seen: set[int] = set()
    for component in components:
        if component.address in seen:
            raise ConfigurationError(
                f"Address {component.address} is configured more than once"
            )
        seen.add(component.address)

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


def next_free_slot(components: Iterable[Component]) -> int:
    """Pick a device slot not already in use by a configured component."""
    used = {
        component.slot for component in components if component.kind.configured
    }
    for slot in range(1, MAX_DEVICE_INDEX + 1):
        if slot not in used:
            return slot
    raise ConfigurationError(
        f"All {MAX_DEVICE_INDEX} device slots are in use; remove one first"
    )


# ---------------------------------------------------------------------------
# Entity and device identity
# ---------------------------------------------------------------------------


def keypad_id(mac: str, address: int) -> str:
    """Identity of one component, stable across restarts and DHCP leases.

    A bus address alone is not unique — two gateways can both have an address
    16 — so identity is the gateway's MAC plus the address. This is what the
    Buttons Machine backend stores as its ``device_serial`` and compares against
    the events we publish, and it is the stem of every entity id below, so LEDs
    can be found from the serial alone.
    """
    return f"{_normalise_mac(mac)}-{address}"


def gateway_device_id(mac: str) -> str:
    return f"be3-{_normalise_mac(mac)}"


def component_device_id(mac: str, address: int) -> str:
    """Device identity for one bus component.

    One device per component, not per physical panel: the bus reports
    components, and identify and re-address both act on a single address. A
    SUBLIME Pro therefore appears as two devices, which is what its two
    addresses actually are.
    """
    return f"be3-{keypad_id(mac, address)}"


def button_unique_id(mac: str, address: int, button: int) -> str:
    return f"be3-{keypad_id(mac, address)}-button{button}"


def led_unique_id(mac: str, address: int, button: int) -> str:
    return f"be3-{keypad_id(mac, address)}-led{button}"


def led_unique_id_prefix(serial: str) -> str:
    """Prefix the backend scans for when mapping buttons to LED entities."""
    return f"be3-{serial}-led"


def identify_unique_id(mac: str, address: int) -> str:
    return f"be3-{keypad_id(mac, address)}-identify"


def _normalise_mac(mac: str) -> str:
    """Compare MACs without caring about separators or case."""
    return mac.replace(":", "").replace("-", "").lower()
